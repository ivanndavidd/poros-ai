"""Auto-enrich an MDL's CURATION layer (descriptions / metrics / reference_values /
examples) using the tenant's configured LLM, grounded in the REAL schema + PII-masked
sample data. Triggered by a button (POST /admin/datasources/{id}/auto-enrich), NOT on
every connect (sampling costs money on BigQuery; the admin decides when).

Hard rules honored:
  - PII NEVER reaches the LLM: sample values of mdl['pii_columns'] are redacted before
    the prompt, and reference_values are computed ONLY for non-PII columns.
  - Read-only: sampling uses SELECT ... LIMIT / GROUP BY only; every generated example
    SQL is re-validated through governance.govern() (read-only + allowlist + CLAC) and
    dropped if it fails — so a hallucinated table/PII column can't slip into the catalog.
  - Non-destructive MERGE: only fills EMPTY fields / NEW description keys; an admin's
    existing curation is never overwritten (they can still edit afterwards).
  - Names are grounded: descriptions/examples referencing tables/columns that don't
    exist are dropped; description keys are normalised to the canonical table/column name.
"""
import json
import re

from . import store
from .llm import gateway
from .governance.engine import govern

# Column-name hints for "dimension" columns worth listing valid values for (reference_values).
_DIM_HINTS = (
    "status", "type", "tipe", "channel", "kanal", "category", "kategori", "uom", "satuan",
    "state", "stage", "level", "grade", "class", "group", "segment", "segmen", "region",
    "wilayah", "area", "site", "method", "metode", "reason", "alasan", "flag", "kind",
    "movement", "gr_type", "stock_type", "stock_owner", "delivery_type", "doc_type",
    "document_type", "sales_channel",
)

# Bounds — keep latency + BigQuery scan cost predictable.
_MAX_TABLES_SAMPLED = 12
_SAMPLE_ROWS = 12
_MAX_SAMPLE_COLS = 24
_MAX_DIM_COLS = 6
_MAX_DIM_VALUES = 25
_MAX_EXAMPLES = 8
_DRYRUN_MAX_BYTES = 300 * 1024 ** 2   # skip a sample query estimated to scan > 300 MB (BigQuery)


# ---------- sampling (read-only, PII-safe) ----------

def _qi(name: str, dialect: str) -> str:
    """Quote an identifier for the target dialect (BigQuery backticks, etc.)."""
    from sqlglot import exp
    return exp.to_identifier(name, quoted=True).sql(dialect=dialect)


def _safe_run(conn_obj, sql: str):
    """Run a bounded read-only query; return (cols, rows) or None on any error / too-costly.
    Uses the connector's dry_run() cost estimate when available (BigQuery) to skip big scans."""
    try:
        est = conn_obj.dry_run(sql)          # None for engines without cost estimation
        if est is not None and est > _DRYRUN_MAX_BYTES:
            return None
    except Exception:
        pass
    try:
        return conn_obj.run(sql)
    except Exception:
        return None


def _mask_row(row, pii_idx):
    return ["[PII]" if i in pii_idx else v for i, v in enumerate(row)]


def _gather_samples(conn_obj, dialect, allowed, columns_by_table, pii_lower):
    """Return (samples, refs, notes).
      samples : {table: {"columns": [...], "rows": [[...]]}}  — PII columns excluded entirely
      refs    : {column: [values]}  — real DISTINCT values of non-PII dimension-like columns
      notes   : human notes (e.g. sampling truncated)."""
    samples, refs, notes = {}, {}, []
    for t in allowed[:_MAX_TABLES_SAMPLED]:
        cols = columns_by_table.get(t, [])
        # (1) sample rows — only NON-PII columns are ever selected, trimmed to bound tokens.
        pick = [c for c in cols if c.lower() not in pii_lower][:_MAX_SAMPLE_COLS]
        if pick:
            sel = ", ".join(_qi(c, dialect) for c in pick)
            res = _safe_run(conn_obj, f"SELECT {sel} FROM {_qi(t, dialect)} LIMIT {_SAMPLE_ROWS}")
            if res and res[1]:
                samples[t] = {"columns": list(res[0]), "rows": [list(r) for r in res[1]]}
        # (2) reference values — real, from the DB (not invented), non-PII dimension columns only.
        dims = [c for c in cols
                if c.lower() not in pii_lower and any(h in c.lower() for h in _DIM_HINTS)][:_MAX_DIM_COLS]
        for c in dims:
            if c in refs:
                continue
            res = _safe_run(
                conn_obj,
                f"SELECT {_qi(c, dialect)} AS v, COUNT(*) AS n FROM {_qi(t, dialect)} "
                f"GROUP BY {_qi(c, dialect)} ORDER BY n DESC LIMIT {_MAX_DIM_VALUES}")
            if res and res[1]:
                vals = [str(r[0]) for r in res[1] if r[0] is not None and str(r[0]).strip() != ""]
                if vals:
                    refs[c] = vals[:_MAX_DIM_VALUES]
    if len(allowed) > _MAX_TABLES_SAMPLED:
        notes.append(f"sampel dibatasi ke {_MAX_TABLES_SAMPLED} dari {len(allowed)} tabel (hemat biaya)")
    return samples, refs, notes


# ---------- prompt + parse ----------

def _build_prompt(ds_name, dialect, allowed, columns_by_table, pii_cols, samples, refs) -> str:
    ctx = ["SKEMA (tabel di allowlist -> kolom):"]
    for t in allowed:
        ctx.append(f"- {t}: " + ", ".join(columns_by_table.get(t, [])))
    if pii_cols:
        ctx.append("\nKOLOM PII (nilainya disensor; JANGAN buat contoh SQL yang menampilkan kolom ini): "
                   + ", ".join(pii_cols))
    if samples:
        ctx.append("\nCONTOH DATA (kolom PII sudah dikeluarkan):")
        for t, s in samples.items():
            ctx.append(f"[{t}] columns={s['columns']}")
            for r in s["rows"][:6]:
                ctx.append("  " + json.dumps(r, ensure_ascii=False, default=str))
    if refs:
        ctx.append("\nNILAI DIMENSI NYATA (dari database — pakai ini kalau relevan):")
        for c, vals in refs.items():
            ctx.append(f"- {c}: " + ", ".join(vals))

    return (
        f"Kamu menyiapkan katalog NL->SQL untuk sumber data '{ds_name}' (SQL dialect: {dialect}). "
        "Berdasarkan skema + contoh data di bawah, keluarkan HANYA SATU objek JSON valid, bentuknya:\n"
        '{\n'
        '  "descriptions": {"<tabel>": "...", "<tabel>.<kolom>": "..."},\n'
        '  "metrics": [{"name": "...", "definition": "<ekspresi SQL agregat, mis. SUM(quantity)>", "note": "..."}],\n'
        '  "examples": [{"q": "<pertanyaan bisnis>", "sql": "<SELECT ... valid untuk dialect ini>"}]\n'
        "}\n"
        "ATURAN:\n"
        "- Tulis semua deskripsi & pertanyaan contoh dalam Bahasa Indonesia yang ringkas & jelas.\n"
        "- Deskripsikan setiap tabel (satu baris mewakili apa) lalu kolom-kolom penting. "
        "JANGAN mengarang tabel/kolom yang tidak ada di skema.\n"
        "- metrics: hanya agregasi masuk akal dari kolom numerik nyata; 'definition' berupa EKSPRESI SQL, bukan kalimat.\n"
        f"- examples: {_MAX_EXAMPLES} pertanyaan umum; SQL HARUS read-only SELECT, hanya memakai tabel di allowlist, "
        "dan JANGAN memilih kolom PII.\n"
        "- Kalau ragu, lebih sedikit lebih baik daripada mengarang.\n"
        "- Keluarkan JSON saja: tanpa penjelasan, tanpa blok ``` .\n\n"
        + "\n".join(ctx))


def _parse_json(text: str) -> dict:
    """Defensive JSON parse (models sometimes wrap in prose / code fences)."""
    if not text:
        return {}
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    s, e = t.find("{"), t.rfind("}")
    if s >= 0 and e > s:
        t = t[s:e + 1]
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


# ---------- validate + non-destructive merge ----------

def _validate_and_merge(mdl: dict, gen: dict, refs: dict, dialect: str, namespace: dict | None = None):
    """Return (proposal_mdl, added_counts). Only fills EMPTY fields / NEW description keys."""
    allowed = list(mdl.get("allowed_tables") or list((mdl.get("tables") or {}).keys()))
    tables = mdl.get("tables") or {}
    tbl_by_lower = {t.lower(): t for t in allowed}
    cols_by_lower = {t: {c.lower(): c for c in tables.get(t, [])} for t in allowed}

    proposal = dict(mdl)
    added = {"descriptions": 0, "metrics": 0, "examples": 0, "reference_values": 0}

    # descriptions — MERGE (keep existing keys), normalise to canonical names, drop unknowns.
    desc = dict(mdl.get("descriptions") or {})
    gd = gen.get("descriptions")
    if isinstance(gd, dict):
        for k, v in gd.items():
            if not isinstance(v, str) or not v.strip():
                continue
            key = str(k).strip()
            if "." in key:
                base, col = key.split(".", 1)
                tb = tbl_by_lower.get(base.strip().lower())
                real_col = cols_by_lower.get(tb, {}).get(col.strip().lower()) if tb else None
                real_key = f"{tb}.{real_col}" if (tb and real_col) else None
            else:
                tb = tbl_by_lower.get(key.lower())
                real_key = tb
            if real_key and real_key not in desc:
                desc[real_key] = v.strip()
                added["descriptions"] += 1
    if desc:
        proposal["descriptions"] = desc

    # metrics — only when currently empty (avoid dupes); shape-validate.
    if not mdl.get("metrics"):
        ms = []
        for m in (gen.get("metrics") or []):
            if isinstance(m, dict) and str(m.get("name", "")).strip() and str(m.get("definition", "")).strip():
                item = {"name": str(m["name"]).strip()[:80], "definition": str(m["definition"]).strip()[:200]}
                if str(m.get("note", "")).strip():
                    item["note"] = str(m["note"]).strip()[:200]
                ms.append(item)
        if ms:
            proposal["metrics"] = ms[:20]
            added["metrics"] = len(proposal["metrics"])

    # reference_values — deterministic, from the DB DISTINCT (non-PII), only when empty.
    if not mdl.get("reference_values") and refs:
        proposal["reference_values"] = refs
        added["reference_values"] = len(refs)

    # examples — only when empty; EACH SQL re-validated through govern() with a restrictive
    # role_policy (read-only + Wren allowlist + CLAC blocks PII columns). Invalid ones dropped.
    if not mdl.get("examples"):
        exs = []
        for e in (gen.get("examples") or []):
            if not (isinstance(e, dict) and str(e.get("q", "")).strip() and str(e.get("sql", "")).strip()):
                continue
            sql = str(e["sql"]).strip().rstrip(";")
            try:
                # {} => can_see_pii False, no full_access. The namespace keeps a legitimate
                # `project.dataset.table` from the model valid while still rejecting a
                # qualifier that points outside this datasource.
                govern(sql, dialect, mdl, {}, namespace=namespace)
            except Exception:
                continue                          # not read-only / outside allowlist / selects PII / unparsable
            exs.append({"q": str(e["q"]).strip()[:300], "sql": sql})
            if len(exs) >= _MAX_EXAMPLES:
                break
        if exs:
            proposal["examples"] = exs
            added["examples"] = len(exs)

    return proposal, added


# ---------- entry point ----------

def enrich(tenant_id: str, ds: dict, use_samples: bool = True) -> dict:
    """Generate a curation-layer draft for one datasource. Returns:
      {mdl, added, sampled, notes, model, tokens, cost}
    Raises gateway.LLMUnavailable when the tenant has no usable model configured."""
    if ds.get("type") == "docs":
        raise ValueError("documentation source has no SQL schema to enrich")

    # Fail FAST when no model is set — BEFORE any (paid) DB sampling, so clicking the
    # button without a configured model costs nothing (BigQuery scan / latency).
    if not gateway.is_configured(tenant_id, "answer_complex"):
        raise gateway.LLMUnavailable("no model configured for this tenant — set one in the Model tab")

    mdl = ds.get("mdl") or {}
    if isinstance(mdl, str):
        mdl = json.loads(mdl)
    tables = mdl.get("tables") or {}
    allowed = list(mdl.get("allowed_tables") or list(tables.keys()))
    columns_by_table = {t: tables.get(t, []) for t in allowed}
    pii_cols = list(mdl.get("pii_columns") or [])
    pii_lower = {p.lower() for p in pii_cols}

    from .connectors.factory import make_connector
    conn_obj = make_connector(ds["type"], ds["conn"])
    dialect = getattr(conn_obj, "dialect", "duckdb")

    samples, refs, notes = {}, {}, []
    if use_samples:
        try:
            samples, refs, notes = _gather_samples(conn_obj, dialect, allowed, columns_by_table, pii_lower)
        except Exception as e:  # sampling is best-effort — degrade to names-only
            notes.append(f"sampling dilewati ({e}); memakai nama kolom saja")

    prompt = _build_prompt(ds.get("name", ""), dialect, allowed, columns_by_table, pii_cols, samples, refs)
    res = gateway.complete(tenant_id, "answer_complex", [{"role": "user", "content": prompt}], temperature=0)

    gen = _parse_json(res.text)
    proposal, added = _validate_and_merge(
        mdl, gen, refs, dialect, getattr(conn_obj, "namespace", lambda: None)())

    # best-effort metering (visible in Monitoring) — must never break the enrich.
    try:
        store.log_usage(tenant_id, "admin", "admin", "mdl_enrich", 0, "ok",
                        model=res.model, prompt_tokens=res.prompt_tokens,
                        completion_tokens=res.completion_tokens, cost=res.cost)
    except Exception:
        pass

    return {
        "mdl": proposal,
        "added": added,
        "sampled": bool(samples or refs),
        "notes": notes,
        "model": res.model,
        "tokens": res.prompt_tokens + res.completion_tokens,
        "cost": res.cost,
    }
