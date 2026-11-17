"""MBR narration (phase 4): the model writes the story, the data keeps the numbers.

Pinned here, with the model faked:
  * a figure the model writes must be a number the page showed it (rounded the way it is
    written) or a change / difference / share / ratio of two numbers that sit together;
  * an untraceable figure is sent back once, then dropped for the rule-written text;
  * the synthesis lands as labelled claims, the reviewer's rewrites pass the same checks;
  * no model, a dead provider or MBR_NARRATION=0 leaves the package exactly as before;
  * PII values never reach a prompt;
  * the deck still passes its house QC, the evidence file says what the AI wrote.
"""
import io
import json
import re
import sys
from pathlib import Path

import pytest
from pptx import Presentation

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import jobs, store                                      # noqa: E402
from app.llm import gateway                                      # noqa: E402
from app.llm.gateway import LLMResult                            # noqa: E402
from app.reports import markdown, mbr_engine, narrate, pptx_writer, xlsx_writer  # noqa: E402
from test_mbr_report import _hdr, _model, client, world          # noqa: E402,F401

BAD = "IDR 987.6 bn"                  # in no page's data


# ---------------------------------------------------------------- the fake model
def _between(text, start, end):
    i = text.index(start) + len(start)
    return text[i:text.index(end, i)]


def _a_number(card) -> str | None:
    """A number the page really shows, written the way a person would write it."""
    for t in card.get("tables") or []:
        for row in t["rows"]:
            for v in row:
                if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) > 10 \
                        and not (1990 <= abs(v) <= 2100):
                    return f"{v:,}"
    return None


class FakeModel:
    """Answers the three prompts narrate sends; ``bad_*`` plants untraceable figures."""

    def __init__(self, bad_first=False, bad_always=False, review=None, fail=False):
        self.bad_first, self.bad_always, self.fail = bad_first, bad_always, fail
        self.review = review
        self.prompts, self.pages = [], {}

    def __call__(self, tenant_id, purpose, messages, **kw):
        assert purpose == "answer_complex" and kw.get("timeout")
        self.prompts.append("\n".join(m["content"] for m in messages))
        if self.fail:
            raise gateway.LLMUnavailable("provider down")
        user, repair = messages[1]["content"], len(messages) > 2
        if user.startswith("You are the reviewing manager"):
            reply = {"problems": self.review or []}
        elif "Write the synthesis of the month" in user:
            reply = self._synthesis(json.loads(_between(user, "missing):\n", "\n\nWrite")))
        else:
            reply = self._page(json.loads(_between(user, "The data on this page:\n",
                                                   "\n\nReply with")), repair)
        return LLMResult(text=json.dumps(reply), model="fake-model", provider="fake",
                         prompt_tokens=100, completion_tokens=40, cost=0.01)

    def _page(self, card, repair):
        n = _a_number(card)
        head = f"{card['title']} reached {n} this month" if n else f"{card['title']} held up"
        self.pages[card["page"]] = head
        insights = [{"label": "INTERPRETATION", "text": "Buyers, not prices, moved the page.",
                     "confidence": "high"},
                    {"label": "INFERENCE", "text": "The campaign pulled demand forward.",
                     "confidence": "medium", "alternative": "A competitor ran out of stock."}]
        if self.bad_always or (self.bad_first and not repair):
            insights.append({"label": "INFERENCE", "text": f"Next month brings {BAD}.",
                             "confidence": "low"})
        if self.bad_always:
            head = f"{card['title']} hit {BAD}"
        return {"headline": head, "takeaway": "Keep the stock of the leading pack ready.",
                "insights": insights, "framing": "Shows where the month was won.",
                "message": "Say what changed and ask for the stock plan."}

    @staticmethod
    def _synthesis(pages):
        refs = [p["page"] for p in pages]
        return {
            "story": "Acmesmile grew on more buyers while the price held",
            "summary": [{"text": p["headline"], "pages": [p["page"]]} for p in pages[:3]],
            "tension": "Sales grew while cancellations stayed high.",
            "drivers": [{"text": "More buyers came through the campaign.", "confidence": "medium",
                         "alternative": "A competitor ran out of stock.", "pages": refs[:2]},
                        {"text": f"A price cut worth {BAD} drove it.", "pages": refs[:1]}],
            "foresight": [{"text": "October depends on keeping the hero pack in stock.",
                           "confidence": "medium", "pages": refs[-1:]}],
            "asks": [{"text": "Share the campaign calendar two weeks earlier.",
                      "pages": refs[:1]},
                     {"text": "Confirm the stock plan for the leading pack.", "pages": ["Z9"]}],
            "strategy": [{"action": "Push the leading pack on the biggest marketplace.",
                          "why": "It carries most of the growth.", "impact": "Higher NMV.",
                          "assumption": "Stock holds through the month.", "pages": refs[:2]}],
            "decisions": [{"text": "Agree the stock plan for the leading pack.",
                           "owner": "Brand", "when": "this week",
                           "unlocks": "a full month of the hero pack on sale",
                           "pages": refs[:1]},
                          {"text": f"Fund a {BAD} campaign.", "owner": "Acme",
                           "pages": refs[:1]}],
        }


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        m = FakeModel(**kw)
        monkeypatch.setattr(narrate.gateway, "is_configured", lambda tid, purpose: True)
        monkeypatch.setattr(narrate.gateway, "complete", m)
        return m
    return install


def _narrated(world, fake_model):
    run, model = _model(world)
    before = {s["id"]: s.get("headline") for s in model["sections"]}
    model["narration"] = narrate.narrate(run, model)
    return run, model, before


# ---------------------------------------------------------------- the figure checker
def test_figures_trace_rounding_and_derived_numbers():
    g = narrate.Ground()
    g.add([1_120_000_000, 1_230_000_000])
    ok = lambda t: narrate.check(t, g, 300)[1] == []            # noqa: E731
    assert ok("NMV rose from IDR 1.12 bn to IDR 1.23 bn (up 9.8%)")
    assert ok("a gap of IDR 110 m between the months")          # a difference of the pair
    assert ok("the 9.9 campaign on 9 Sep 2026 is on page C2; 3 products led")
    assert not ok("NMV reached IDR 1.5 bn")
    assert not ok("NMV up 9.9%")                    # a percent is a figure, not a sale day
    assert not ok("IDR 2.2 bn in the quarter")
    assert not ok("sold in 45 marketplaces")         # month words must be whole
    problems = narrate.check("orders_monthly grew", g, 300)[1]
    assert any("technical names" in p for p in problems)
    assert narrate.tidy("NMV grew — strongly") == "NMV grew, strongly"


def test_a_label_written_into_the_text_moves_to_its_field():
    """Staging: 'INTERPRETATION, high confidence: ...' reached slide titles and cards."""
    assert narrate.tidy("INTERPRETATION, high confidence: Buyers fell.") == "Buyers fell."
    assert narrate.tidy("RECOMMENDATION: Approve the plan.") == "Approve the plan."
    assert narrate.tidy("High confidence: The page overstates it.") == "The page overstates it."
    assert narrate.tidy("Interpretation of the month is clear") == \
        "Interpretation of the month is clear"               # a word, not a label
    g = narrate.Ground()
    rejected = []
    items = narrate._clean_items(
        [{"text": "INFERENCE, medium confidence: October stays ahead of the pace. "
                  "Alternative: a late campaign could change it.", "pages": ["A1"]}],
        ("text", "alternative"), g, {"A1": "A1"}, rejected, "foresight", 2)
    assert items == [{"text": "October stays ahead of the pace.",
                      "alternative": "a late campaign could change it.",
                      "confidence": "medium", "pages": ["A1"]}], (items, rejected)


def test_a_label_or_page_list_at_the_end_of_a_text_moves_to_its_field():
    """Staging package 3: '(INTERPRETATION, high confidence.)' closed summary, driver and
    ask cards, and '(Summary 1, D1, D3)' closed the slide title of the synthesis."""
    assert narrate.tidy("NMV reached IDR 430.7 m. (INTERPRETATION, high confidence.)") == \
        "NMV reached IDR 430.7 m."
    assert narrate.tidy("Approve a day by day plan. (RECOMMENDATION.)") == \
        "Approve a day by day plan."
    assert narrate.tidy("COD carries 35.0% of NMV. (Summary 1, D1, D3)") == \
        "COD carries 35.0% of NMV."
    for kept in ("Buyers fell (from 7,133 to 5,154).", "Pack of 4 (45.2%) leads",
                 "Low conversion (high traffic, low conversion)"):
        assert narrate.tidy(kept) == kept                    # ordinary brackets stay
    assert narrate.lead_confidence("It slowed. (INFERENCE, medium confidence.)") == "medium"
    g = narrate.Ground()
    items = narrate._clean_items(
        [{"text": "Buyers fell while orders held. (INTERPRETATION, high confidence.) (D5, A1)"}],
        ("text",), g, {"D5": "customers", "A1": "monthly_trend"}, [], "summary", 4)
    assert items == [{"text": "Buyers fell while orders held.", "confidence": "high",
                      "pages": ["customers", "monthly_trend"]}], items


def test_a_side_panel_never_runs_past_its_box():
    """Staging slides 11 and 26: insights plus facts spilled over the takeaway band."""
    deck = pptx_writer._Deck({"brand": "ACMESMILE", "period": "2026-09"})
    s = deck.slide("C", "A page")
    long = ("NMV fell from IDR 552.9 m to IDR 430.7 m as lower units took IDR 173.0 m off, "
            "partly offset by higher prices adding IDR 49.6 m and new listings adding more")
    facts = [("Interpretation:", long)] * 6 + [(None, "A short fact.")]
    placed = pptx_writer._panel(s, 8.55, pptx_writer.BODY_Y, 4.35, pptx_writer.BODY_H,
                                "Units, price and listings", facts)
    assert 2 <= placed < len(facts)
    text = [sh.text_frame.text for sh in s.shapes if sh.has_text_frame]
    assert any("A short fact." in t for t in text)           # a shorter one still fits
    first = pptx_writer._panel(s, 0.45, pptx_writer.BODY_Y, 4.35, 1.2, "Tiny", [("Lead:", long)])
    assert first == 1                                         # the first item always shows, cut


# ---------------------------------------------------------------- no model: nothing changes
def test_without_a_model_the_package_is_unchanged(world, monkeypatch):
    monkeypatch.setattr(narrate.gateway, "is_configured", lambda tid, purpose: False)
    run, model = _model(world)
    before = json.dumps(model, sort_keys=True, default=str)
    out = narrate.narrate(run, model)
    assert out["status"] == "skipped" and "no model" in out["reason"]
    assert json.dumps(model, sort_keys=True, default=str) == before
    model["narration"] = out
    assert "No text in this file was written by a model" in markdown.evidence(model)
    assert "Narration did not run" in markdown.open_items(model)
    monkeypatch.setenv("MBR_NARRATION", "0")
    monkeypatch.setattr(narrate.gateway, "is_configured", lambda tid, purpose: True)
    assert narrate.narrate(run, model)["reason"] == "switched off"


def test_a_dead_provider_costs_nothing_but_the_narration(world, fake):
    m = fake(fail=True)
    run, model = _model(world)
    before = {s["id"]: s.get("headline") for s in model["sections"]}
    out = narrate.narrate(run, model)
    assert out["status"] == "failed" and out["calls"] == 0
    assert out["failed_calls"] <= 8                 # stops after three in a row
    assert len(m.prompts) <= 8
    assert {s["id"]: s.get("headline") for s in model["sections"]} == before


# ---------------------------------------------------------------- pages
def test_pages_get_a_traced_headline_and_labelled_insights(world, fake):
    m = fake(bad_first=True)
    _run, model, before = _narrated(world, m)
    out = model["narration"]
    pages = [s for s in model["sections"] if s.get("narration")]
    assert pages and out["pages_written"] == out["pages_total"] == len(pages)
    for s in pages:
        assert s["narration"]["status"] == "repaired"           # the planted figure was fixed
        assert s["computed_headline"] == before[s["id"]]
        assert s["headline"] == m.pages[s["ref"]]
        assert s["takeaway"] == "Keep the stock of the leading pack ready."
        ai = [f for f in s["facts"] if f.get("by") == "ai"]
        assert {f["label"] for f in ai} == {"INTERPRETATION", "INFERENCE"}
        assert all(BAD not in f["text"] for f in ai)
        assert next(f for f in ai if f["label"] == "INFERENCE")["alternative"]
    assert [r for r in out["rejected"] if r["page"] != "SYNTHESIS"] == []
    assert out["model"] == "fake-model" and out["prompt_tokens"] == 100 * out["calls"]
    assert out["cost"] == pytest.approx(0.01 * out["calls"])
    appendix = [s for s in model["sections"] if s["group"] == "Appendix"]
    assert all(not s.get("narration") for s in appendix)


def test_a_figure_that_never_traces_falls_back_to_the_rule_text(world, fake):
    m = fake(bad_always=True)
    _run, model, before = _narrated(world, m)
    out = model["narration"]
    pages = [s for s in model["sections"] if s.get("narration")]
    assert pages
    for s in pages:
        assert s["headline"] == before[s["id"]]                  # rule text kept
        assert s["narration"]["status"] == "partial"
        assert all(BAD not in f["text"] for f in s["facts"])
    assert any(r["field"] == "headline" and "987.6 bn" in r["reason"]
               for r in out["rejected"])
    assert "dropped because a figure or wording failed" in markdown.open_items(model)


# ---------------------------------------------------------------- synthesis + review
def test_synthesis_review_and_writers(world, fake):
    first = None

    def review_for(model):
        return [{"page": first, "field": "headline", "problem": "No so what.",
                 "replacement": "Sales reached IDR 999.9 bn"},
                {"page": "SYNTHESIS", "field": "story", "problem": "Too vague.",
                 "replacement": "Acmesmile grew on more buyers; the price held"}]

    m = fake()
    run, model = _model(world)
    first = next(s["ref"] for s in model["sections"] if narrate.narratable(s))
    m.review = review_for(model)
    model["narration"] = out = narrate.narrate(run, model)
    syn = next(s for s in model["sections"] if s["id"] == "synthesis")
    assert out["status"] == "written" and out["synthesis"]
    assert syn["status"] == "complete" and not syn["missing"]
    assert syn["headline"] == "Acmesmile grew on more buyers; the price held"   # review applied
    labels = {f["label"] for f in syn["facts"]}
    assert labels == {"INTERPRETATION", "INFERENCE", "RECOMMENDATION"}
    assert all(f["by"] == "ai" for f in syn["facts"])
    assert all(BAD not in f["text"] for f in syn["facts"])                # bad driver dropped
    assert any("Expected impact: Higher NMV." in f["text"] for f in syn["facts"])
    assert [d["text"] for d in syn["synthesis"]["decisions"]] == [
        "Agree the stock plan for the leading pack."]                     # bad one dropped
    assert any(f["text"].startswith("Decision this week: Agree the stock plan") and
               "Owner: Brand." in f["text"] for f in syn["facts"])
    assert syn["synthesis"]["asks"][1]["pages"] == []                     # unknown page code
    page = next(s for s in model["sections"] if s["ref"] == first)
    assert page["headline"] == m.pages[first]                             # bad rewrite refused
    review = {p["page"]: p for p in out["review"]}
    assert review[first]["applied"] is False and "999.9" in review[first]["reason"]
    assert review["SYNTHESIS"]["applied"] is True
    assert any(r["field"] == "drivers" for r in out["rejected"])

    ev = markdown.evidence(model)
    assert "INFERENCE, AI" in ev and "Alternative reading:" in ev
    assert "were written by fake-model" in ev
    oi = markdown.open_items(model)
    assert "Narration (AI) review" in oi and "for the owner" in oi
    book = xlsx_writer.build(model)
    assert book[:2] == b"PK"
    import zipfile
    strings = zipfile.ZipFile(io.BytesIO(book)).read("xl/sharedStrings.xml").decode("utf-8")
    assert "Main storyline:" in strings and "Agree the stock plan" in strings
    main = xlsx_writer.main_story(model)
    assert 10 <= len(main) <= 15 or len(main) == len(
        [s for s in model["sections"] if s.get("headline")])

    data = pptx_writer.build(model)
    assert pptx_writer.qc(data) == []               # dashes, sizes, pictures, notes
    slides = list(Presentation(io.BytesIO(data)).slides)
    texts = [" ".join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame)
             for s in slides]
    assert "Acmesmile grew on more buyers; the price held" in texts[1]      # the executive summary slide
    assert any("What Acme asks of Acmesmile" in t for t in texts)
    assert any("decision to take this week" in t and "Owner:" in t for t in texts)
    assert any("moves for Acme next month" in t or "move for Acme next month" in t
               for t in texts)
    notes = " ".join(s.notes_slide.notes_text_frame.text for s in slides)
    assert "written by AI" in notes
    assert "Say what changed and ask for the stock plan." in notes      # page message
    assert BAD not in " ".join(texts) + notes


# ---------------------------------------------------------------- PII
def test_pii_values_never_reach_the_model(world, fake):
    tid, did = world["tid"], world["did"]
    ds = store.get_datasource(did)
    mdl = dict(ds["mdl"])
    mdl["pii_columns"] = sorted(set(mdl.get("pii_columns") or []) | {"product_name"})
    store.update_mdl(did, mdl)
    store.create_role(tid, "pii_reader", {"full_access": True, "can_see_pii": True})
    store.set_role_businesses(tid, "pii_reader", [world["biz"]])
    m = fake()
    run = mbr_engine.prepare(tid, "pii_reader", "ACMESMILE", "2026-09", "MBR")
    model = mbr_engine.build(run)
    shown = json.dumps(model, default=str)
    assert "Product SKU-A" in shown                 # the reader may see it ...
    out = narrate.narrate(run, model)
    sent = "\n".join(m.prompts)
    assert out["pii_values_masked"] >= 1
    assert "[PII]" in sent
    assert not re.search("product sku-a", sent, re.I)   # ... the model may not


# ---------------------------------------------------------------- the job
def test_the_report_job_meters_the_narration(world, fake):
    fake()
    h = _hdr(world["tid"])
    r = client.post("/v1/reports/mbr", headers=h,
                    json={"brand": "acmesmile", "period": "2026-09", "business": "MBR"})
    assert r.status_code == 202, r.text
    rid = r.json()["report_id"]
    assert jobs.run_one("pytest-mbr-narration") is True
    st = client.get(f"/v1/reports/{rid}", headers=h).json()
    assert st["status"] == "succeeded", st
    c = store._c()
    try:
        rows = c.execute("SELECT model, prompt_tokens, completion_tokens, cost FROM usage_log "
                         "WHERE tenant_id=? AND action='mbr_report'",
                         (world["tid"],)).fetchall()
    finally:
        c.close()
    assert len(rows) == 1                           # one request, narration included
    assert rows[0]["model"] == "fake-model" and rows[0]["prompt_tokens"] > 0
    assert rows[0]["cost"] > 0
    md = client.get(f"/v1/reports/{rid}/files/EVIDENCE-AND-REASONING.md", headers=h).text
    assert "INTERPRETATION, AI" in md
