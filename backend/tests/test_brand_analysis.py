"""Brand analysis in chat: an analysis of a brand stands on the brand's fixed numbers.

Pinned here, with the model faked and the warehouse a real DuckDB (the MBR test world):
  * only the brand mode detects a brand up front (a brand that exists, named whole; no
    brand named = an open reading the agent settles, chat overhaul item 5); elsewhere the
    chat agent calls brand_baseline itself (item 3, pinned in test_chat_overhaul.py);
  * the baseline is the review's core template queries, cached per brand and month,
    encrypted at rest, holding masked cards and never query rows;
  * every figure the model writes traces to the data it was shown (Indonesian number
    style read right); a sentence with an untraced one is sent back once, then taken out;
  * the answer is the model's own markdown (chat overhaul item 2): no headings or tables of
    ours, only the basis line;
  * PII values never reach the prompt, even for a role allowed to see them;
  * through /v1/chat in the brand mode the same question gets the same analysis from the
    cache, and the brand mode without a brand answers with the brand as an open reading.
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store                                            # noqa: E402
from app.agent import brand_analysis as ba                       # noqa: E402
from app.agent import orchestrator as orch                       # noqa: E402
from app.llm import gateway                                      # noqa: E402
from app.llm.gatekeeper import GateDecision                      # noqa: E402
from app.llm.gateway import LLMResult                            # noqa: E402
from app.reports import mbr_engine, narrate                      # noqa: E402
from app.reports.sqlkit import Month                             # noqa: E402
from app.skills.registry import Ctx                              # noqa: E402
from test_mbr_report import _hdr, client, world                  # noqa: E402,F401

TODAY = date(2026, 10, 14)
BAD = "Rp 987.6 miliar"                # in no data the model is shown
QUESTION = "kenapa NMV Acmesmile naik di September 2026?"


# ---------------------------------------------------------------- the fake analyst
def _between(text, start, end):
    i = text.index(start) + len(start)
    return text[i:text.index(end, i)]


def _is_figure(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and abs(v) > 1000 and not (1990 <= abs(v) <= 2100))


def _a_number(cards) -> str:
    cells = (v for b in cards for page in b["pages"] for t in page.get("tables") or []
             for row in t["rows"] for v in row)
    for v in cells:
        if _is_figure(v):
            return f"{v:,}"
    raise AssertionError("the baseline shows no number")


class FakeAnalyst:
    """Answers the brand analysis prompt; every other model call is 'unavailable', so the
    rest of the turn runs its no-model fallbacks (rule-based SQL)."""

    def __init__(self, bad_first=False, bad_always=False):
        self.bad_first, self.bad_always = bad_first, bad_always
        self.prompts, self.calls = [], 0

    def __call__(self, tenant_id, purpose, messages, **kw):
        if not messages[0]["content"].startswith("You are the business analyst of Acme"):
            raise gateway.LLMUnavailable("only the brand analysis is faked")
        assert purpose == "answer_complex"
        self.calls += 1
        self.prompts.append("\n".join(m["content"] for m in messages))
        repair = len(messages) > 2
        user = messages[1]["content"]
        cards = json.loads(_between(user, "BASELINE:\n", "\n\nQUESTION EVIDENCE:"))
        n = _a_number(cards)
        bad = self.bad_always or (self.bad_first and not repair)
        reply = (
            f"NMV Acmesmile naik karena pembeli bertambah; nilainya **{n}**.\n\n"
            "Yang paling mungkin menjelaskan kenaikan ini:\n"
            "- Kampanye 9.9 menarik pembeli baru. Lonjakan terjadi di hari kampanye. "
            + (f"Harga turun senilai {BAD}. " if bad else "")
            + "Cara memastikan: bandingkan pembeli baru di hari kampanye dan hari biasa.\n"
            "- Pengambilalihan toko oleh Acme menaikkan volume.\n"
            + (f"\nBulan depan diperkirakan {BAD}.\n" if bad else "")
            + f"\n| Angka | Nilai |\n|---|---|\n| NMV | {n} |\n"
            + (f"| Target | {BAD} |\n" if bad else ""))
        return LLMResult(text=reply, model="fake-analyst", provider="fake",
                         prompt_tokens=200, completion_tokens=80, cost=0.02)


@pytest.fixture
def analyst(monkeypatch):
    def install(**kw):
        m = FakeAnalyst(**kw)
        monkeypatch.setattr(ba.llm, "available", lambda: True)
        monkeypatch.setattr(ba.llm, "supports_tools", lambda *a, **k: False)
        monkeypatch.setattr(ba.llm, "complete", m)
        return m
    return install


def _biz(world):
    return {"id": world["biz"], "name": "MBR"}


def _target(world, message=QUESTION, role="analyst", mode="brand"):
    mbr_engine._brand_cache.clear()
    return ba.detect(world["tid"], role, _biz(world), message, mode, today=TODAY)


def _ctx(world, role="analyst"):
    return Ctx(tenant_id=world["tid"], role=role, role_policy={}, question=QUESTION)


# ---------------------------------------------------------------- detection
def test_brand_names_are_read_whole_longest_first_and_never_ordinary_words():
    names = ["ACMESMILE", "ACMESMILE EXPERT", "OTHER", "SHOPEE", "PHILIPS"]
    assert ba.brands_named("kenapa nmv Acmesmile Expert turun", names) == ["ACMESMILE EXPERT"]
    assert ba.brands_named("bandingkan Acmesmile dan Philips", names) == ["ACMESMILE", "PHILIPS"]
    assert ba.brands_named("why did Acmesmile drop against other brands", names) == ["ACMESMILE"]
    assert ba.brands_named("analisa penjualan shopee", names) == []      # a marketplace word
    assert ba.brands_named("analisa acmesmileplus", names) == []            # not a whole name
    assert len(ba.brands_named("acmesmile philips acmesmile expert", names)) == ba.MAX_BRANDS


def test_the_period_is_read_in_both_languages_and_the_baseline_is_a_full_month():
    def p(text):
        month, note, asked = ba.period_of(text, TODAY)
        return month.key, note, asked.key if asked else None
    assert p("analisa Acmesmile September 2026") == ("2026-09", "", "2026-09")
    assert p("analisa Acmesmile 2026-07") == ("2026-07", "", "2026-07")
    assert p("kenapa Acmesmile turun di bulan mei") == ("2026-05", "", "2026-05")
    assert p("why did Acmesmile drop in December") == ("2025-12", "", "2025-12")  # not ahead
    assert p("kenapa Acmesmile turun bulan lalu") == ("2026-09", "", "2026-09")
    assert p("analisa Acmesmile bulan ini") == ("2026-09", "current", "2026-10")
    assert p("may I see why Acmesmile dropped") == ("2026-09", "", None)   # "may" is a word
    assert p("analisa Acmesmile") == ("2026-09", "", None)


def test_only_the_brand_mode_detects_and_no_brand_named_is_an_open_reading(world, monkeypatch):
    t = _target(world)
    assert isinstance(t, ba.Target)
    assert t.brands == ["ACMESMILE"] and t.month == Month(2026, 9)
    assert _target(world, "berapa nmv Acmesmile", mode="client").brands == ["ACMESMILE"]
    assert _target(world, "kenapa nmv Wardah turun")["kind"] == "brand"  # no such brand: open
    # outside the brand mode nothing is detected: the chat agent decides (brand_baseline)
    for mode in ("auto", "fast", "deep", None):
        assert _target(world, QUESTION, mode=mode) is None, mode
    reading = _target(world, "bagaimana penjualannya?")
    assert reading["kind"] == "brand" and reading["term"] is None
    assert "question" not in reading                 # nothing is asked of the person
    assert "busiest first: ACMESMILE" in reading["_context"]
    assert "brand_baseline" in reading["_context"]
    assert reading["options"][0]["id"] == "ACMESMILE"
    assert reading["options"][0]["question"] == "bagaimana penjualannya? (brand ACMESMILE)"
    monkeypatch.setenv("BRAND_ANALYSIS", "0")
    assert _target(world) is None


def test_a_question_about_every_brand_is_not_asked_to_pick_one(world):
    """Live loop: "Analyze GMV and GMV split for all clients in 1P between August and
    September" in the brand mode was asked which brand, and "all brands" was asked again."""
    live = "Analyze GMV and GMV split for all clients in 1P between August and September"
    assert _target(world, live, mode="brand") is None
    for text in ("analisa gmv semua brand bulan lalu", "kenapa gmv per client turun",
                 "evaluate each brand in September", "analisa masing-masing klien"):
        assert _target(world, text, mode="brand") is None, text
    # a named brand still wins, and one brand without "all" is still an open reading
    assert _target(world, "analisa Acmesmile vs semua brand", mode="brand").brands == ["ACMESMILE"]
    ask = _target(world, "analyze GMV for 1P between August and September", mode="brand")
    assert ask["kind"] == "brand"
    every = ask["options"][-1]
    assert every["id"] == ba.brand_confirm.ALL and every["name"] == "All brands"
    assert _target(world, every["question"], mode="brand") is None       # leaves the mode


def test_indonesian_numbers_dates_and_source_labels_are_read_right():
    assert ba.normalize_numbers("Rp 1.234.567 naik 12,3% dari 1.234,5") == \
        "Rp 1,234,567 naik 12.3% dari 1,234.5"
    assert ba.normalize_numbers("tahun 2.026 dan 1,234 order") == "tahun 2.026 dan 1,234 order"
    ground = narrate.Ground()
    ground.add([1234567.0, 12.3])
    problems = ba.check("NMV Rp 1.234.567 naik 12,3% sejak 9 September 2026 "
                        "[Kalender kampanye — Slide 3].", ground)
    assert problems == [], problems
    problems = ba.check(f"Bulan depan {BAD}.", ground)
    assert problems and problems[0].startswith("figures not in the data")
    problems = ba.check("Lihat kolom order_status.", ground)
    assert any(p.startswith("technical names") for p in problems)


# ---------------------------------------------------------------- the baseline
def test_the_baseline_is_fixed_cached_encrypted_and_holds_no_rows(world, monkeypatch):
    t = _target(world)
    first = ba.baselines(world["tid"], "analyst", t)
    assert len(first) == 1 and first[0]["cached"] is False
    b = first[0]
    assert b["period"] == "2026-09" and b["cards"] and b["scorecard"]["rows"]
    assert b["sections"]["scorecard"] in ("complete", "partial")
    assert all("rows" not in q and isinstance(q["row_count"], int) for q in b["queries"])
    assert b["ground"]["values"]

    monkeypatch.setattr(ba.mbr_engine, "prepare",
                        lambda *a, **k: pytest.fail("a cached baseline reads nothing"))
    again = ba.baselines(world["tid"], "analyst", t)
    assert again[0]["cached"] is True
    assert {k: v for k, v in again[0].items() if k != "cached"} == \
        {k: v for k, v in b.items() if k != "cached"}

    c = store._c()
    try:
        rows = c.execute("SELECT kind, data FROM analysis_cache WHERE tenant_id=?",
                         (world["tid"],)).fetchall()
    finally:
        c.close()
    assert [r["kind"] for r in rows] == ["brand_baseline"]
    assert b"ACMESMILE" not in bytes(rows[0]["data"])                     # encrypted at rest

    focus = ba.focus_text(t, again)
    assert "'ACMESMILE'" in focus and "Sep 2026 (2026-09)" in focus and "Already computed" in focus


def test_a_failed_baseline_leaves_the_turn_to_its_own_evidence(world, monkeypatch):
    t = _target(world)

    def boom(*a, **k):
        raise RuntimeError("warehouse down")
    monkeypatch.setattr(ba.mbr_engine, "prepare", boom)
    assert ba.baselines(world["tid"], "analyst", t) == []


# ---------------------------------------------------------------- the analysis
def test_the_answer_is_the_models_own_markdown_over_the_basis_line(world, analyst):
    m = analyst()
    t = _target(world)
    bases = ba.baselines(world["tid"], "analyst", t)
    ctx = _ctx(world)
    out = ba.compose(ctx, t, bases, {}, QUESTION)
    assert out and m.calls == 1 and out["flagged"] == []
    text = out["text"]
    # the model's answer, as it wrote it (bullets, its own table), then the basis line
    assert text.startswith(out["answer"]) and out["answer"].startswith("NMV Acmesmile naik")
    assert "- Pengambilalihan toko oleh Acme menaikkan volume." in text
    assert "| Angka | Nilai |\n|---|---|\n| NMV |" in text
    assert "_Dasar analisa: angka dasar ACMESMILE untuk Sep 2026" in text
    # none of the old fixed headings or code-written tables
    for heading in ("**ACMESMILE · Sep 2026**", "**Angka kunci**", "| Ukuran |",
                    "**Yang terlihat**", "**Hipotesis**", "**Langkah yang disarankan**",
                    "**Belum bisa disimpulkan**", "keyakinan tinggi"):
        assert heading not in text, heading
    assert "Reply with this JSON" not in m.prompts[0]
    assert ctx.usage and ctx.usage[0]["model"] == "fake-analyst"
    meta = ba.meta(t, bases, out)
    assert meta["brands"] == ["ACMESMILE"] and meta["period"] == "2026-09"
    assert "hypotheses" not in meta and meta["flagged"] == 0 and "repaired" not in meta


def test_an_untraced_figure_stays_in_the_answer_and_is_flagged(world, analyst):
    t = _target(world)
    bases = ba.baselines(world["tid"], "analyst", t)
    stubborn = analyst(bad_always=True)
    out = ba.compose(_ctx(world), t, bases, {}, QUESTION)
    # no repair turn and nothing deleted (chat overhaul item 6): the reader sees the
    # sentence, and the response names the figure that matched no data
    assert stubborn.calls == 1 and "repaired" not in out
    assert f"Harga turun senilai {BAD}." in out["text"] and f"| Target | {BAD} |" in out["text"]
    assert [r["text"] for r in out["flagged"]] == [
        f"Harga turun senilai {BAD}.", f"Bulan depan diperkirakan {BAD}.",
        f"| Target | {BAD} |"]
    assert all("figures not in the data" in r["reason"] for r in out["flagged"])
    assert ba.unverified(out["flagged"]) == ["987.6 miliar"]
    vr = ba.settle_verdict({"ok": True, "issues": [], "checks": {}}, bases, out)
    assert vr["checks"]["figures_traceable"] is False and not vr["ok"]
    assert vr["unverified_figures"] == ["987.6 miliar"]


def test_without_a_model_there_is_no_brand_answer(world, monkeypatch):
    t = _target(world)
    bases = ba.baselines(world["tid"], "analyst", t)
    monkeypatch.setattr(ba.llm, "available", lambda: False)
    assert ba.compose(_ctx(world), t, bases, {}, QUESTION) is None


def test_pii_values_never_reach_the_model(world, analyst):
    tid, did = world["tid"], world["did"]
    ds = store.get_datasource(did)
    mdl = dict(ds["mdl"])
    mdl["pii_columns"] = sorted(set(mdl.get("pii_columns") or []) | {"product_name"})
    store.update_mdl(did, mdl)
    store.create_role(tid, "pii_reader", {"full_access": True, "can_see_pii": True})
    store.set_role_businesses(tid, "pii_reader", [world["biz"]])
    m = analyst()
    t = _target(world, role="pii_reader")
    bases = ba.baselines(tid, "pii_reader", t)
    out = ba.compose(_ctx(world, "pii_reader"), t, bases, {}, QUESTION)
    assert out
    sent = "\n".join(m.prompts)
    assert "[PII]" in sent
    assert not re.search("product sku-a", sent, re.I)
    assert not re.search("product sku-a", json.dumps(bases), re.I)      # nor the cache


def test_the_verifier_does_not_fail_numbers_it_never_saw():
    vr = {"ok": False, "checks": {"figures_traceable": False, "grounded": False},
          "issues": ["figures not present in evidence: 1,234", "answer has no grounded "
                     "evidence", "missing citation"]}
    out = ba.settle_verdict(vr, [{"brand": "ACMESMILE"}])
    assert out["checks"]["figures_traceable"] and out["checks"]["grounded"]
    assert out["issues"] == ["missing citation"] and out["ok"] is False


# ---------------------------------------------------------------- /v1/chat end to end
def _gate(monkeypatch):
    gate = GateDecision(allow=True, intent="data_query", complexity="complex",
                        engine="llm:fake-gate")
    monkeypatch.setattr(orch.llm, "screen", lambda *a, **k: gate)


def test_chat_answers_with_the_analysis_and_the_same_question_gets_it_again(
        world, analyst, monkeypatch):
    _gate(monkeypatch)
    m = analyst()
    mbr_engine._brand_cache.clear()
    h = _hdr(world["tid"])
    body = {"message": QUESTION, "business": "MBR", "analysis_mode": "brand"}
    r = client.post("/v1/chat", headers=h, json=body)
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["analysis_mode"] == "brand" and first["router"]["analysis_mode"] == "brand"
    assert first["status"] in ("complete", "partial")
    assert first["answer"].startswith("NMV Acmesmile naik karena pembeli bertambah")
    assert "_Dasar analisa:" in first["answer"]
    assert any(p.startswith("brand_baseline(ACMESMILE:fresh") for p in first["plan"])
    assert "brand_analysis(0 flagged)" in first["plan"] and first["unverified_figures"] == []
    meta = first["brand_analysis"]
    assert meta["brands"] == ["ACMESMILE"] and meta["period"] == "2026-09"
    assert meta["answer_cached"] is False and meta["flagged"] == 0
    assert m.calls == 1
    audit = next(a for a in store.list_audit(world["tid"]) if a["event"] == "answer")
    assert audit["detail"]["analysis_mode"] == "brand"
    assert audit["detail"]["brand_analysis"]["flagged"] == 0

    # the same question, spaced and cased differently: the same analysis, no model call
    again = client.post("/v1/chat", headers=h, json={
        "message": "  Kenapa NMV ACMESMILE naik di September 2026? ", "business": "MBR",
        "analysis_mode": "brand"}).json()
    assert m.calls == 1
    assert again["plan"] == ["brand_analysis(cached)"]
    assert again["answer"] == first["answer"]
    assert again["brand_analysis"]["answer_cached"] is True
    assert again["status"] == first["status"]


def test_chat_in_brand_mode_without_a_brand_answers_with_an_open_reading(
        world, analyst, monkeypatch):
    """Chat overhaul item 5: no "which brand?" — the turn is answered and says what it
    took; with no tool-capable model no baseline is read, so nothing is marked chosen."""
    _gate(monkeypatch)
    m = analyst()
    mbr_engine._brand_cache.clear()
    r = client.post("/v1/chat", headers=_hdr(world["tid"]), json={
        "message": "bagaimana performanya bulan lalu?", "business": "MBR",
        "analysis_mode": "brand"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] != "awaiting_confirmation" and "confirmation" not in out
    assert "ask_brand" not in out["plan"]
    reading = out["assumptions"][0]
    assert reading["kind"] == "brand" and reading["chosen"] is None
    assert [o["id"] for o in reading["options"]][:1] == ["ACMESMILE"]
    assert not any(k.startswith("_") for k in reading)   # server-only keys stay home
    assert m.calls == 0


def test_chat_in_brand_mode_a_correction_takes_one_brand_or_all_of_them(
        world, analyst, monkeypatch):
    _gate(monkeypatch)
    m = analyst()
    mbr_engine._brand_cache.clear()
    h = _hdr(world["tid"])
    original = "analisa gmv bulan September 2026"

    def chat(message):
        r = client.post("/v1/chat", headers=h, json={
            "message": message, "business": "MBR", "analysis_mode": "brand"})
        assert r.status_code == 200, r.text
        return r.json()

    first = chat(original)
    options = {o["id"]: o for o in first["assumptions"][0]["options"]}
    every = chat(options[ba.brand_confirm.ALL]["question"])     # "all brands" leaves the mode
    assert every.get("brand_analysis") is None and every["assumptions"] == []
    picked = chat(options["ACMESMILE"]["question"])
    assert picked["brand_analysis"]["brands"] == ["ACMESMILE"] and m.calls == 1
    assert picked["assumptions"] == []                 # the brand is named now


def test_an_ordinary_number_question_stays_on_the_ordinary_path(world, analyst, monkeypatch):
    _gate(monkeypatch)
    m = analyst()
    mbr_engine._brand_cache.clear()
    out = client.post("/v1/chat", headers=_hdr(world["tid"]), json={
        "message": "berapa nmv Acmesmile September 2026", "business": "MBR"}).json()
    assert out.get("analysis_mode") != "brand" and out.get("brand_analysis") is None
    assert m.calls == 0
