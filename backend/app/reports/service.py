"""Build an MBR package and store its files; called by the ``mbr_report`` job."""
from __future__ import annotations

import json
import logging
import re

from .. import store
from ..context import PipelineCancelled
from . import markdown, mbr_engine, narrate, pptx_writer, xlsx_writer

log = logging.getLogger(__name__)

MIME_MD = "text/markdown; charset=utf-8"
MIME_JSON = "application/json"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _stem(brand: str, period_label: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", f"{brand}_{period_label}").strip("_")


def _query_progress(report):
    """Progress callback for the template queries (40 at most; queries are the slow part)."""
    done = {"n": 0}

    def progress(key, rec):
        done["n"] += 1
        if report:
            outcome = "ok" if rec["ok"] else rec.get("code")
            report(min(5 + done["n"] * 2, 85), "querying", f"{key}: {outcome}")

    return progress


def _narration(run, model: dict, report, job_id: str) -> dict:
    """The story: headlines, takeaways, synthesis written by the tenant's model, every
    figure traced; any failure leaves the rule-written text and the package ships."""
    try:
        return narrate.narrate(run, model, report)
    except PipelineCancelled:
        raise
    except Exception as exc:
        log.exception("mbr narration failed job=%s", job_id)
        return {"status": "failed", "errors": [f"{type(exc).__name__}: {exc}"[:300]]}


def _workbook(model: dict, job_id: str) -> bytes | None:
    try:
        return xlsx_writer.build(model)
    except Exception:                       # a writer bug never costs the evidence package
        log.exception("mbr workbook failed job=%s", job_id)
        return None


def _deck(model: dict, job_id: str) -> bytes | None:
    try:
        deck = pptx_writer.build(model)
        for issue in pptx_writer.qc(deck):
            log.warning("mbr deck qc job=%s: %s", job_id, issue)
        return deck
    except Exception:                       # same rule as the workbook
        log.exception("mbr deck failed job=%s", job_id)
        return None


def generate_mbr(tenant_id: str, role: str, brand: str, period: str, business: str | None,
                 job_id: str, report=None, user_id: str = "", app_key: str = "",
                 targets: dict | None = None, inputs: str | None = None) -> dict:
    """Run the engine, write the package, return the manifest the job stores as its result."""
    if report:
        report(2, "preparing", f"MBR {brand} {period}")
        if inputs:                           # a big zip takes a minute or two to read
            report(3, "reading_files", "Reading the attached files")
    run = mbr_engine.prepare(tenant_id, role, brand, period, business,
                             progress=_query_progress(report), targets=targets, inputs=inputs)
    try:
        model = mbr_engine.build(run)
        model["narration"] = _narration(run, model, report, job_id)
    finally:
        run.close()                          # the attached files' working copy goes now
    return _package(tenant_id, role, job_id, model, report, user_id, app_key)


def _package(tenant_id, role, job_id, model, report, user_id, app_key) -> dict:
    """Write the files and return the job result."""
    if report:
        report(98, "writing", "Writing the report files")

    stem = _stem(model["brand"], model["period_label"])
    data_json = json.dumps(model, ensure_ascii=False, indent=1, default=str).encode("utf-8")
    evidence = markdown.evidence(model).encode("utf-8")
    checksums = {f"{stem}_report_data.json": markdown.sha256(data_json),
                 "EVIDENCE-AND-REASONING.md": markdown.sha256(evidence)}
    workbook = _workbook(model, job_id)
    if workbook:
        checksums[f"{stem}_Review.xlsx"] = markdown.sha256(workbook)
    deck = _deck(model, job_id)
    if deck:
        checksums[f"{stem}_Business_Review.pptx"] = markdown.sha256(deck)
    open_items = markdown.open_items(model, checksums).encode("utf-8")
    files = [
        store.save_report_file(tenant_id, job_id, "EVIDENCE-AND-REASONING.md", MIME_MD, evidence),
        store.save_report_file(tenant_id, job_id, "OPEN-ITEMS-AND-REVIEW.md", MIME_MD, open_items),
        store.save_report_file(tenant_id, job_id, f"{stem}_report_data.json", MIME_JSON, data_json),
    ]
    if workbook:
        files.append(store.save_report_file(tenant_id, job_id, f"{stem}_Review.xlsx", MIME_XLSX,
                                            workbook))
    if deck:
        files.append(store.save_report_file(tenant_id, job_id, f"{stem}_Business_Review.pptx",
                                            MIME_PPTX, deck))
    stats = model["stats"]
    told = model.get("narration") or {}
    store.log_usage(tenant_id, app_key, role, "mbr_report", stats["queries_ok"],
                    "OK", model=told.get("model") or None,
                    prompt_tokens=told.get("prompt_tokens") or 0,
                    completion_tokens=told.get("completion_tokens") or 0,
                    cost=told.get("cost") or 0.0, user_id=user_id, trace_id=job_id)
    if report:
        report(100, "done", markdown.delivery_status(model))
    return {
        "report_id": job_id,
        "kind": "mbr",
        "brand": model["brand"],
        "period": model["period"],
        "business": model["business"],
        "data_as_of": model["data_as_of"],
        "delivery_status": markdown.delivery_status(model),
        "stats": stats,
        "narration": {k: told.get(k) for k in ("status", "model", "pages_written",
                                               "pages_total", "synthesis", "cost")
                      if k in told},
        "sections": [{"id": s["id"], "group": s["group"], "title": s["title"],
                      "status": s["status"], "headline": s["headline"]}
                     for s in model["sections"]],
        "files": files,
        "uploads": _uploads_summary(model.get("uploads")),
    }


def _uploads_summary(uploads: dict | None) -> dict | None:
    """What the attached files gave this report (counts only, no file content)."""
    if not uploads:
        return None
    files = uploads.get("files") or []
    return {"files_used": sum(1 for f in files if f.get("used")),
            "files_ignored": sum(1 for f in files if not f.get("used")),
            "tables": uploads.get("tables") or {}, "replaced": uploads.get("replaced") or [],
            "error": uploads.get("error")}
