"""Handlers for Poros persistent background jobs.

The handler layer deliberately calls the same connectors, governance helpers, RAG
pipeline and agent pipeline as the synchronous APIs. Moving execution behind a queue
changes transport/lifecycle only; it does not reduce scan depth or answer quality.
"""
from __future__ import annotations

import os
import re
import time

from . import jobs, mdl_merge, rag, store
from .agent import run_agent
from .connectors.factory import make_connector
from .context import PipelineCancelled, set_budget_reservation
from .schema_scan import scan_connector

_DS_NOT_FOUND = "Datasource tidak ditemukan."

# Filename noise stripped before comparing two uploads ("report final.pdf" ==
# "report v2.pdf"). Split in two so neither alternation gets hard to read; the
# two sets are disjoint, so applying them in sequence matches the same tokens.
_NOISE_WORDS = re.compile(r"\b(?:copy|final|draft|new|baru|lama|old|updated?)\b")
_NOISE_VERSIONS = re.compile(r"\b(?:rev(?:isi)?\d*|versi?\s*\d*|v\d+)\b")


def _datasource_test(_job, payload, report, _emit):
    ds_type, conn = payload["type"], payload.get("conn") or {}
    if ds_type == "docs":
        return {"ok": True, "tables": [], "table_count": 0, "note": "documentation source"}
    report(10, "connecting", "Menghubungkan ke database")
    try:
        intro = make_connector(ds_type, conn).introspect()
    except Exception as exc:
        raise jobs.JobFailure(
            "CONNECT_FAILED",
            "Koneksi atau scan database gagal. Periksa konfigurasi dan izin service account.",
        ) from exc
    report(90, "schema_ready", f"{len(intro)} tabel ditemukan")
    return {"ok": True, "tables": sorted(intro), "table_count": len(intro)}


def _datasource_create(job, payload, report, _emit):
    ds_type = payload["type"]
    if ds_type == "docs":
        mdl = {
            "kind": "docs", "tables": {}, "allowed_tables": [], "pii_columns": [],
            "denied_functions": [], "allowed_functions": [],
        }
    else:
        report(10, "connecting", "Menghubungkan ke database")
        try:
            connector = make_connector(ds_type, payload.get("conn") or {})
            intro = connector.introspect()
        except Exception as exc:
            raise jobs.JobFailure(
                "CONNECT_FAILED",
                "Koneksi atau scan database gagal. Periksa konfigurasi dan izin service account.",
            ) from exc
        report(70, "building_catalog", f"Menyusun katalog {len(intro)} tabel")
        mdl = scan_connector(connector)
    did = store.create_datasource(
        job["tenant_id"], ds_type, payload["name"], payload.get("conn") or {}, mdl
    )
    return {"id": did, "mdl": mdl}


def _datasource_update(_job, payload, report, _emit):
    ds = store.get_datasource(payload["datasource_id"])
    if not ds:
        raise jobs.JobFailure("NOT_FOUND", _DS_NOT_FOUND)
    if ds["type"] == "docs":
        raise jobs.JobFailure("NOT_SQL", "Sumber dokumentasi tidak memiliki koneksi SQL.")
    report(10, "validating_connection", "Memvalidasi koneksi baru")
    try:
        intro = make_connector(ds["type"], payload.get("conn") or {}).introspect()
    except Exception as exc:
        raise jobs.JobFailure(
            "CONNECT_FAILED",
            "Koneksi baru gagal divalidasi; konfigurasi lama tetap digunakan.",
        ) from exc
    report(85, "saving_connection", "Koneksi valid; menyimpan konfigurasi")
    store.update_datasource_conn(ds["id"], payload.get("conn") or {})
    return {"ok": True, "datasource_id": ds["id"], "table_count": len(intro)}


def _merge_message(pruned: dict) -> str:
    parts = []
    if pruned["dropped_total"]:
        parts.append(f"{pruned['dropped_total']} kurasi diparkir (tabel/kolom hilang)")
    if pruned["restored_total"]:
        parts.append(f"{pruned['restored_total']} kurasi dipulihkan dari karantina")
    return "; ".join(parts) or "Kurasi lama tetap utuh"


def _datasource_rescan(_job, payload, report, _emit):
    ds = store.get_datasource(payload["datasource_id"])
    if not ds:
        raise jobs.JobFailure("NOT_FOUND", _DS_NOT_FOUND)
    if ds["type"] == "docs":
        return ds["mdl"]
    report(10, "scanning_schema", "Memindai skema database")
    try:
        connector = make_connector(ds["type"], ds["conn"])
        scanned = scan_connector(connector)
    except Exception as exc:
        raise jobs.JobFailure(
            "SCAN_FAILED", "Scan schema gagal; katalog lama tetap digunakan."
        ) from exc
    report(70, "merging_catalog", "Menggabungkan scan dengan kurasi yang sudah ada")
    try:
        fresh, pruned = mdl_merge.merge_scan(ds["mdl"], scanned)
    except mdl_merge.ScanLostAllTables as exc:
        raise jobs.JobFailure(
            "SCAN_EMPTY",
            "Scan tidak menemukan tabel apa pun; katalog lama tetap digunakan.",
        ) from exc
    report(85, "pruning_curation", _merge_message(pruned))
    store.update_mdl(ds["id"], fresh)
    # Businesses curate the same tables. A name the scan no longer returns must not survive
    # in a business allowlist, or questions keep routing to a table govern() then refuses.
    scoped = store.prune_business_tables(ds["id"], list((fresh.get("tables") or {}).keys()))
    # `pruned` rides along with the job result only — it is a report, not catalog state.
    return {**fresh, "pruned": pruned, "business_pruned": scoped}


def _datasource_auto_enrich(job, payload, report, _emit):
    ds = store.get_datasource(payload["datasource_id"])
    if not ds:
        raise jobs.JobFailure("NOT_FOUND", _DS_NOT_FOUND)
    if ds["type"] == "docs":
        raise jobs.JobFailure("NOT_SQL", "Sumber dokumentasi tidak memiliki skema.")
    from . import mdl_enrich
    from .llm.gateway import LLMUnavailable
    report(5, "sampling", "Menyiapkan skema dan sample data yang aman")
    try:
        out = mdl_enrich.enrich(
            job["tenant_id"], ds, use_samples=bool(payload.get("use_samples", True))
        )
    except LLMUnavailable as exc:
        raise jobs.JobFailure(
            "NO_MODEL", f"Belum ada model aktif untuk tenant ini: {exc}"
        ) from exc
    except Exception as exc:
        raise jobs.JobFailure(
            "ENRICH_FAILED",
            "Auto-enrich gagal. Gunakan request id untuk memeriksa log backend.",
        ) from exc
    report(90, "saving_catalog", "Menyimpan draft semantic catalog")
    store.update_mdl(ds["id"], out["mdl"])
    return out


def _business_auto_enrich(job, payload, report, _emit):
    """Draft the curation for ONE (business, datasource) pair.

    Same model pass as the datasource-level enrich, but it reads and writes the BUSINESS's
    curation: mdl_enrich only ever looks at `mdl`, so it is handed a view of the datasource
    whose curation layer IS the business's. `datasources.mdl` is not written at all, which
    is what keeps 1P's drafts out of 3P's catalog.
    """
    from . import business_scope, mdl_enrich
    from .llm.gateway import LLMUnavailable
    bid, did = payload["business_id"], payload["datasource_id"]
    ds = store.get_datasource(did)
    if not ds:
        raise jobs.JobFailure("NOT_FOUND", _DS_NOT_FOUND)
    if ds["type"] == "docs":
        raise jobs.JobFailure("NOT_SQL", "Sumber dokumentasi tidak memiliki skema.")
    knowledge = store.get_business_knowledge(bid, did)
    if knowledge is None:
        raise jobs.JobFailure("NOT_IN_BUSINESS",
                              "Datasource ini belum masuk ke business tersebut.")
    if not knowledge["allowed_tables"]:
        # Without a slice mdl_enrich would fall back to every table in the schema and draft
        # for tables this business cannot query — then the save would drop all of it.
        raise jobs.JobFailure("NO_TABLES",
                              "Pilih dulu authorized tables untuk business ini, "
                              "supaya AI hanya membaca tabel yang memang dipakai.")
    report(5, "sampling", "Menyiapkan skema dan sample data yang aman")
    try:
        out = mdl_enrich.enrich(job["tenant_id"], business_scope.enrich_view(ds, knowledge),
                                use_samples=bool(payload.get("use_samples", True)))
    except LLMUnavailable as exc:
        raise jobs.JobFailure(
            "NO_MODEL", f"Belum ada model aktif untuk tenant ini: {exc}"
        ) from exc
    except Exception as exc:
        raise jobs.JobFailure(
            "ENRICH_FAILED",
            "Auto-enrich gagal. Gunakan request id untuk memeriksa log backend.",
        ) from exc
    report(90, "saving_catalog", "Menyimpan draft semantic catalog business")
    drafted = business_scope.curation_of(out["mdl"])
    # The draft is still subject to the datasource ceiling and the contract checks: a model
    # suggestion is never a reason to widen a slice or to store a contract govern() rejects.
    drafted["allowed_tables"], _ = business_scope.narrow_tables(ds["mdl"],
                                                               drafted["allowed_tables"])
    try:
        drafted = business_scope.validate(ds["mdl"], drafted)
    except ValueError as exc:
        raise jobs.JobFailure("SEMANTIC_CONTRACT_INVALID", str(exc)) from exc
    store.set_business_knowledge(bid, did, drafted)
    return {**out, "knowledge": store.get_business_knowledge(bid, did)}


def _norm_name(filename: str) -> str:
    base = os.path.splitext(filename or "")[0].lower()
    base = re.sub(r"\(\d+\)", " ", base)
    base = _NOISE_WORDS.sub(" ", base)
    base = _NOISE_VERSIONS.sub(" ", base)
    return re.sub(r"[^a-z0-9]+", "", base)


def _flag_document_versions(tenant_id, doc, result) -> None:
    """Same-filename older versions are auto-archived (re-upload is the normal flow, not
    an edge case); similar filenames (v2/(1)/final) are reported so the UI can warn."""
    existing = [
        item for item in store.list_documents(tenant_id, doc["datasource_id"])
        if item["id"] != doc["id"] and not item.get("archived")
    ]
    same = [item for item in existing if item["filename"] == doc["filename"]]
    for item in same:
        store.set_document_archived(item["id"], True)
    if same:
        result["replaced"] = [
            {"id": item["id"], "filename": item["filename"]} for item in same
        ]
    normalized = _norm_name(doc["filename"])
    similar = [
        item for item in existing
        if item["filename"] != doc["filename"]
        and normalized
        and _norm_name(item["filename"]) == normalized
    ]
    if similar:
        result["possible_duplicates"] = [
            {"id": item["id"], "filename": item["filename"]} for item in similar
        ]


def _auto_reindex_stale(tenant_id, doc_id, report, result) -> None:
    """Document-config auto_reindex: re-embed older docs left on a previous embedder."""
    doc_cfg = store.get_tenant_config(tenant_id, "document") or {}
    if not doc_cfg.get("auto_reindex"):
        return
    current = rag.embedder_name(tenant_id)
    stale = [
        item for item in store.list_documents(tenant_id)
        if item["id"] != doc_id and not item.get("archived")
        and item.get("embedder") and item.get("embedder") != current
    ]
    if stale:
        report(92, "reindexing", "Menyelaraskan embedding dokumen lama")
        result["auto_reindexed"] = {
            "reindexed": True, **rag.reindex_tenant(tenant_id)
        }


def _document_ingest(job, payload, report, _emit):
    doc_id = payload["document_id"]
    doc = store.get_document(doc_id)
    if not doc or doc["tenant_id"] != job["tenant_id"]:
        raise jobs.JobFailure("NOT_FOUND", "Dokumen tidak ditemukan.")
    try:
        report(5, "extracting", "Membaca dan mengekstrak isi dokumen")
        data = store.read_document_file(job["tenant_id"], doc_id)
        result = rag.ingest_existing_document(
            job["tenant_id"],
            doc_id,
            data,
            config=payload.get("config") or None,
            on_progress=report,
        )
        _flag_document_versions(job["tenant_id"], doc, result)
        _auto_reindex_stale(job["tenant_id"], doc_id, report, result)
        result["file_url"] = f"/admin/documents/{doc_id}/file"
        result["acl_roles"] = doc.get("acl_roles") or []
        store.set_document_job_status(doc_id, "ready", "")
        return result
    except PipelineCancelled:
        store.set_document_job_status(doc_id, "failed", "CANCELLED")
        raise
    except jobs.JobFailure:
        raise
    except Exception as exc:
        store.set_document_job_status(doc_id, "failed", "INGEST_FAILED")
        raise jobs.JobFailure(
            "INGEST_FAILED",
            "Dokumen tidak dapat diproses. Periksa format/isinya lalu coba lagi.",
        ) from exc


def _document_reingest(job, payload, report, _emit):
    doc = store.get_document(payload["document_id"])
    if not doc or doc["tenant_id"] != job["tenant_id"]:
        raise jobs.JobFailure("NOT_FOUND", "Dokumen tidak ditemukan.")
    report(10, "extracting", "Memproses ulang dokumen")
    result = rag.reingest_document(
        job["tenant_id"], doc["id"], payload.get("config") or {}
    )
    store.set_document_job_status(doc["id"], "ready", "")
    return {"ok": True, **result}


def _documents_reindex(job, _payload, report, _emit):
    report(5, "reindexing", "Menghitung ulang embedding seluruh dokumen")
    return {"ok": True, **rag.reindex_tenant(job["tenant_id"])}


def _buffered_publisher(emit):
    """Coalesce streaming deltas so SSE stays live but not per-token: flush at 240 chars
    or 0.6s, and ALWAYS flush before a non-delta event so ordering holds."""
    pending = {"answer_delta": "", "reasoning_delta": ""}
    last_flush = [time.monotonic()]

    def flush():
        for event_type in ("reasoning_delta", "answer_delta"):
            if pending[event_type]:
                emit({"type": event_type, "text": pending[event_type]})
                pending[event_type] = ""
        last_flush[0] = time.monotonic()

    def publish(event):
        event_type = (event or {}).get("type")
        if event_type in pending:
            pending[event_type] += str(event.get("text") or "")
            if len(pending[event_type]) >= 240 or time.monotonic() - last_flush[0] >= 0.6:
                flush()
            return
        flush()
        emit(event)

    return publish, flush


def _log_chat_usage(job, principal, user_id, out) -> None:
    usage = out.get("usage", {})
    sql = out.get("sql") or {}
    rows = len(sql.get("rows", [])) if sql.get("ok") else 0
    status = "BLOCKED" if out.get("blocked") else "OK"
    store.log_usage(
        job["tenant_id"], principal["key"], principal["role"], "chat", rows, status,
        usage.get("model"), usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0), usage.get("cost", 0.0),
        user_id=user_id, trace_id=out.get("trace_id", job["id"]),
    )


def _chat(job, payload, report, emit):
    principal = {
        "tenant_id": job["tenant_id"],
        "key": job.get("app_key_id") or "",
        "role": payload["role"],
    }
    user_id = job.get("actor_id") or ""
    try:
        reservation = store.reserve_budget(job["tenant_id"])
    except store.BudgetExceeded as exc:
        raise jobs.JobFailure(
            "BUDGET_EXCEEDED",
            f"Monthly AI budget exhausted (limit ${exc.limit:.2f}).",
        ) from exc
    set_budget_reservation(reservation["id"] if reservation else "")
    report(2, "starting", "Menyiapkan analisis")
    publish, flush = _buffered_publisher(emit)

    try:
        out = run_agent(
            principal,
            payload["message"],
            payload.get("datasource_id"),
            user_id,
            emit=publish,
            history=payload.get("history"),
            analysis_mode=payload.get("analysis_mode") or "auto",
            trace_id=job["id"],
            business=payload.get("business"),
            confirmation=payload.get("confirmation"),
            session_id=payload.get("session_id"),
        )
        flush()
        _log_chat_usage(job, principal, user_id, out)
        return out
    except PipelineCancelled:
        store.log_usage(
            job["tenant_id"], principal["key"], principal["role"], "chat", 0,
            "CANCELLED", user_id=user_id, trace_id=job["id"],
        )
        raise
    except BaseException:
        if reservation:
            store.log_usage(
                job["tenant_id"], principal["key"], principal["role"], "chat", 0,
                "ERROR", cost=reservation["amount"], user_id=user_id,
                trace_id=job["id"],
            )
        raise
    finally:
        store.release_budget(reservation["id"] if reservation else None)
        set_budget_reservation("")


def _mbr_report(job, payload, report, _emit):
    """Monthly Business Review package for one brand and month. Figures come from
    governed queries; the tenant model only writes the narration (reports/narrate.py)."""
    from . import business_scope
    from .reports import service as report_service
    from .reports.sqlkit import ReportInputError
    try:
        return report_service.generate_mbr(
            job["tenant_id"], payload["role"], payload["brand"], payload["period"],
            payload.get("business"), job["id"], report,
            user_id=job.get("actor_id") or "", app_key=job.get("app_key_id") or "",
            targets=payload.get("targets"), inputs=payload.get("inputs"))
    except ReportInputError as exc:
        raise jobs.JobFailure(exc.code, exc.message) from exc
    except business_scope.BusinessError as exc:
        raise jobs.JobFailure(exc.code, str(exc)) from exc
    finally:
        # attached files stay until they expire (the person may generate again with them);
        # whatever else of this tenant's has expired goes now
        if payload.get("inputs"):
            store.purge_expired_report_inputs(job["tenant_id"])


def register_all() -> None:
    jobs.register("datasource_test", _datasource_test)
    jobs.register("datasource_create", _datasource_create)
    jobs.register("datasource_update", _datasource_update)
    jobs.register("datasource_rescan", _datasource_rescan)
    jobs.register("datasource_auto_enrich", _datasource_auto_enrich)
    jobs.register("business_auto_enrich", _business_auto_enrich)
    jobs.register("document_ingest", _document_ingest)
    jobs.register("document_reingest", _document_reingest)
    jobs.register("documents_reindex", _documents_reindex)
    jobs.register("chat", _chat)
    jobs.register("mbr_report", _mbr_report)
