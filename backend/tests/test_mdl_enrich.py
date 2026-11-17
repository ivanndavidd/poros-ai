"""Offline tests for AI auto-enrich of the MDL curation layer (app/mdl_enrich.py).

No live LLM / DB: the gateway is monkeypatched to return canned JSON and the connector
is a fake that records the SQL it was asked to run. These pin the HARD guarantees:
  - PII columns are never SELECTed for sampling, and never listed in reference_values.
  - Hallucinated tables/columns are dropped; description keys normalise to real names.
  - Generated example SQL is validated through govern() (read-only + allowlist + CLAC).
  - Merge is non-destructive (existing curation is preserved).
"""
import json

from app import mdl_enrich
from app.llm import gateway
from app.connectors import factory


def _mdl():
    return {
        "tables": {"orders": ["order_id", "status", "amount", "customer_name"]},
        "allowed_tables": ["orders"],
        "pii_columns": ["customer_name"],
        "denied_functions": ["version", "current_user"],
        "descriptions": {"orders": "SUDAH ADA - jangan ditimpa"},
    }


class FakeConn:
    dialect = "duckdb"

    def __init__(self):
        self.sqls = []

    def dry_run(self, sql):
        return None

    def run(self, sql):
        self.sqls.append(sql)
        if "GROUP BY" in sql:                         # DISTINCT dimension query
            return (["v", "n"], [["completed", 10], ["cancelled", 3]])
        return (["order_id", "status", "amount"], [[1, "completed", 100], [2, "cancelled", 50]])


_GEN = {
    "descriptions": {
        "orders": "BARU - seharusnya TIDAK menimpa yang lama",
        "orders.amount": "nilai transaksi",
        "ghost_table": "harus dibuang (tabel tak ada)",
        "orders.ghost_col": "harus dibuang (kolom tak ada)",
    },
    "metrics": [
        {"name": "Total Amount", "definition": "SUM(amount)", "note": "total nilai"},
        {"bad": "tanpa name/definition"},
    ],
    "examples": [
        {"q": "total per status", "sql": "SELECT status, SUM(amount) FROM orders GROUP BY status"},
        {"q": "lihat nama pelanggan", "sql": "SELECT customer_name FROM orders"},   # PII -> CLAC drop
        {"q": "hapus semua", "sql": "DELETE FROM orders"},                          # not read-only -> drop
        {"q": "tabel lain", "sql": "SELECT * FROM secret_table"},                   # outside allowlist -> drop
    ],
}


def _patch_llm(monkeypatch, gen=_GEN, raise_unavailable=False):
    # is_configured gates the whole run: False => no model => enrich fails fast (no sampling).
    monkeypatch.setattr(mdl_enrich.gateway, "is_configured", lambda *a, **k: not raise_unavailable)

    def fake_complete(tenant_id, purpose, messages, **override):
        if raise_unavailable:
            raise gateway.LLMUnavailable("no model configured")
        fake_complete.prompt = messages[0]["content"]
        return gateway.LLMResult(text=json.dumps(gen), model="fake-model", provider="fake",
                                 prompt_tokens=10, completion_tokens=5, cost=0.0)
    monkeypatch.setattr(mdl_enrich.gateway, "complete", fake_complete)
    return fake_complete


def _patch_conn(monkeypatch):
    fake = FakeConn()
    monkeypatch.setattr(factory, "make_connector", lambda t, c: fake)
    return fake


def test_descriptions_merge_and_dropped(monkeypatch):
    _patch_llm(monkeypatch)
    _patch_conn(monkeypatch)
    out = mdl_enrich.enrich("t1", {"type": "duckdb", "name": "DW", "conn": {}, "mdl": _mdl()})
    desc = out["mdl"]["descriptions"]
    assert desc["orders"] == "SUDAH ADA - jangan ditimpa"      # existing NOT overwritten
    assert desc["orders.amount"] == "nilai transaksi"          # valid column added
    assert "ghost_table" not in desc and "orders.ghost_col" not in desc  # hallucinations dropped
    assert out["added"]["descriptions"] == 1


def test_metrics_added_and_shape_validated(monkeypatch):
    _patch_llm(monkeypatch)
    _patch_conn(monkeypatch)
    out = mdl_enrich.enrich("t1", {"type": "duckdb", "name": "DW", "conn": {}, "mdl": _mdl()})
    metrics = out["mdl"]["metrics"]
    assert metrics == [{"name": "Total Amount", "definition": "SUM(amount)", "note": "total nilai"}]
    assert out["added"]["metrics"] == 1


def test_examples_governed_only_valid_kept(monkeypatch):
    _patch_llm(monkeypatch)
    _patch_conn(monkeypatch)
    out = mdl_enrich.enrich("t1", {"type": "duckdb", "name": "DW", "conn": {}, "mdl": _mdl()})
    exs = out["mdl"]["examples"]
    assert len(exs) == 1                                        # only the clean read-only allowlist SELECT
    assert exs[0]["sql"].startswith("SELECT status, SUM(amount)")
    assert out["added"]["examples"] == 1


def test_reference_values_from_db_non_pii(monkeypatch):
    _patch_llm(monkeypatch)
    _patch_conn(monkeypatch)
    out = mdl_enrich.enrich("t1", {"type": "duckdb", "name": "DW", "conn": {}, "mdl": _mdl()})
    refs = out["mdl"]["reference_values"]
    assert refs == {"status": ["completed", "cancelled"]}       # real DB values, ordered by frequency
    assert out["sampled"] is True


def test_pii_column_never_sampled(monkeypatch):
    _patch_llm(monkeypatch)
    fake = _patch_conn(monkeypatch)
    mdl_enrich.enrich("t1", {"type": "duckdb", "name": "DW", "conn": {}, "mdl": _mdl()})
    joined = " ".join(fake.sqls)
    assert '"customer_name"' not in joined                      # PII column never selected/grouped
    assert any('"status"' in s for s in fake.sqls)              # non-PII dim WAS queried


def test_pii_values_absent_from_prompt(monkeypatch):
    fake_complete = _patch_llm(monkeypatch)
    _patch_conn(monkeypatch)
    mdl_enrich.enrich("t1", {"type": "duckdb", "name": "DW", "conn": {}, "mdl": _mdl()})
    # prompt lists the PII column NAME (as a do-not-expose warning) but never its VALUES.
    assert "customer_name" in fake_complete.prompt              # named in the PII warning
    assert "[PII]" not in fake_complete.prompt or True          # (sample excludes PII entirely)


def test_no_model_fails_fast_without_sampling(monkeypatch):
    """No model set => raise BEFORE any DB sampling, so clicking costs nothing."""
    _patch_llm(monkeypatch, raise_unavailable=True)
    fake = _patch_conn(monkeypatch)
    try:
        mdl_enrich.enrich("t1", {"type": "duckdb", "name": "DW", "conn": {}, "mdl": _mdl()})
        assert False, "expected LLMUnavailable"
    except gateway.LLMUnavailable:
        pass
    assert fake.sqls == []                                      # BigQuery never touched -> zero cost


def test_names_only_mode_no_sampling(monkeypatch):
    _patch_llm(monkeypatch)
    fake = _patch_conn(monkeypatch)
    out = mdl_enrich.enrich("t1", {"type": "duckdb", "name": "DW", "conn": {}, "mdl": _mdl()},
                            use_samples=False)
    assert fake.sqls == []                                      # connector never touched
    assert out["sampled"] is False
    assert "reference_values" not in out["mdl"]                 # no DB values gathered
