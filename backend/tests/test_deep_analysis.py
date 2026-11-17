"""Offline regression tests for quality-first strategic analysis."""
from types import SimpleNamespace

from app.agent import deep_analysis
from app.agent.verifier import compute_confidence, verify
from app.models import ChatIn
from app.nl2sql import rank_tables
from app import web_research
from app.web_research import _parse_interaction, _parse_openai_response


QUESTION = (
    "Kalau mau ekspansi gudang, area mana yang potensial untuk Acme? "
    "Analisa dengan matrix ekspansi dan data yang ada."
)


def _gate():
    return SimpleNamespace(pii_request=False, complexity="complex")


def test_deep_research_runs_only_when_the_caller_asks_for_it():
    # chat overhaul item 3: auto no longer routes "analytical-sounding" questions (or a
    # complex gatekeeper label) to Deep Research; the chat agent explores as needed
    assert deep_analysis.should_use("deep")
    assert deep_analysis.should_use(" DEEP ")
    for mode in ("auto", "fast", "brand", None, "nonsense"):
        assert not deep_analysis.should_use(mode), mode
    assert ChatIn(message="x").analysis_mode == "auto"


def test_catalog_ranking_ignores_catalog_wide_company_prefix():
    mdl = {"tables": {
        "acme_store_order": ["gmv", "quantity"],
        "acme_inventory": ["stock"],
        "acme_shipping_cost": ["origin", "destination", "shipping_cost"],
        "acme_profitability": ["gross_profit"],
    }}
    ranked = rank_tables(mdl, "analisa Acme shipping cost origin destination", limit=4)
    assert ranked[0] == "acme_shipping_cost"


def test_google_interaction_parser_preserves_queries_and_exact_urls():
    parsed = _parse_interaction({
        "steps": [
            {
                "type": "google_search_call",
                "arguments": {"queries": ["BPS logistik Jawa Timur 2026"]},
            },
            {
                "type": "model_output",
                "content": [{
                    "type": "text",
                    "text": "Aktivitas logistik meningkat [Web: BPS].",
                    "annotations": [{
                        "type": "url_citation",
                        "title": "Badan Pusat Statistik",
                        "url": "https://www.bps.go.id/id/publication/example",
                    }],
                }],
            },
        ],
    })
    assert parsed["ok"]
    assert parsed["queries"] == ["BPS logistik Jawa Timur 2026"]
    assert parsed["sources"][0]["label"] == "Web: Badan Pusat Statistik"
    assert parsed["sources"][0]["url"].startswith("https://www.bps.go.id/")


def test_openai_response_parser_preserves_citations_usage_and_search_queries():
    parsed = _parse_openai_response({
        "model": "gpt-test",
        "usage": {"input_tokens": 120, "output_tokens": 30},
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "type": "search",
                    "queries": ["BPS logistik Jawa Timur"],
                    "sources": [{
                        "type": "url",
                        "title": "BPS Jawa Timur",
                        "url": "https://jatim.bps.go.id/example",
                    }],
                },
            },
            {
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": "Kinerja logistik meningkat.",
                    "annotations": [{
                        "type": "url_citation",
                        "title": "BPS Jawa Timur",
                        "url": "https://jatim.bps.go.id/example?utm_source=openai",
                    }],
                }],
            },
        ],
    }, "gpt-test", 0.012)
    assert parsed["ok"] and parsed["provider"] == "openai"
    assert parsed["queries"] == ["BPS logistik Jawa Timur"]
    assert len(parsed["sources"]) == 1
    assert parsed["sources"][0]["label"] == "Web: BPS Jawa Timur"
    assert parsed["usage"] == {
        "model": "gpt-test",
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "cost": 0.012,
    }


def test_web_research_reuses_tenant_openai_provider(monkeypatch):
    monkeypatch.setenv("WEB_RESEARCH_PROVIDER", "auto")
    monkeypatch.delenv("GEMINI_SEARCH_API_KEY", raising=False)
    monkeypatch.setattr(
        web_research.store,
        "get_provider",
        lambda tenant_id, provider: (
            {"provider": "openai", "api_key": "secret", "api_base": ""}
            if tenant_id == "t_acme" and provider == "openai"
            else None
        ),
    )
    seen = {}

    def fake_call(tenant_id, prompt):
        seen["tenant_id"] = tenant_id
        seen["prompt"] = prompt
        return {
            "ok": True, "provider": "openai", "model": "gpt-test",
            "text": "Riset", "queries": ["q"], "sources": [],
            "usage": {},
        }

    monkeypatch.setattr(web_research, "_openai_call", fake_call)
    assert web_research.enabled("t_acme")
    result = web_research.research("t_acme", "pertanyaan", "bukti yang dimasking")
    assert result["provider"] == "openai"
    assert seen["tenant_id"] == "t_acme"
    assert "bukti yang dimasking" in seen["prompt"]


def test_verifier_accepts_cited_web_benchmark_and_reports_missing_coverage():
    complete = {
        "web": {
            "ok": True,
            "text": "Indeks logistik resmi adalah 123456.",
            "sources": [{
                "label": "Web: BPS",
                "title": "BPS",
                "url": "https://www.bps.go.id/example",
            }],
        },
        "coverage": {
            "required": ["demand_destination", "service_level"],
            "succeeded": ["demand_destination", "service_level"],
            "missing": [],
        },
    }
    verdict = verify(
        QUESTION,
        "Benchmark eksternal adalah 123456 [Web: BPS].",
        complete,
        _gate(),
    )
    assert verdict["checks"]["web_citations_present"]
    assert verdict["checks"]["figures_traceable"]
    assert verdict["checks"]["source_coverage"]

    incomplete = {
        **complete,
        "coverage": {
            "required": ["demand_destination", "service_level"],
            "succeeded": ["demand_destination"],
            "missing": ["service_level"],
        },
    }
    verdict2 = verify(QUESTION, "Ringkasan [Web: BPS].", incomplete, _gate())
    confidence = compute_confidence(verdict2, incomplete, _gate())
    assert not verdict2["checks"]["source_coverage"]
    assert confidence["signals"]["coverage_missing"] == ["service_level"]
    assert confidence["score"] < 80


def test_verifier_traces_negative_currency_when_rp_separates_the_sign():
    evidence = {
        "sql": {
            "ok": True,
            "columns": ["gross_profit"],
            "rows": [[-3013815744]],
            "safe_sql": "SELECT gross_profit FROM finance",
        },
    }
    verdict = verify(
        "berapa gross profit?",
        "Gross profit sebesar **-Rp3.013.815.744**.",
        evidence,
        _gate(),
    )
    assert verdict["checks"]["figures_traceable"]
