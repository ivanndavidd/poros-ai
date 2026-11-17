"""Every remaining prompt boundary found by the PII-to-LLM audit.

Hard rule (CLAUDE.md): PII is NEVER sent to an LLM. An audit of all outbound model
calls found the rule was enforced at some boundaries and quietly skipped at others —
the same string arrived tokenized on one path and verbatim on the next. These pin the
gaps shut. Companion file: test_pii_prompt_redaction.py (single-shot SQL + the
sentence-ending-email regex gap).

Offline: every model call is monkeypatched and the outgoing prompt is captured.
"""
from types import SimpleNamespace

import pytest

from app.skills import registry
from app.skills.registry import TextPiiRedactor, Ctx
from app.memory.conversation import Conversation

EMAIL = "budi.santoso@acme.co.id"
PHONE = "081234567890"
NIK = "3174092509900001"

MDL = {
    "tables": {"orders": ["order_id", "customer_email", "gmv"]},
    "allowed_tables": ["orders"],
    "pii_columns": ["customer_email"],
}


def _ctx(question, conversation=""):
    ctx = Ctx(tenant_id="t1", role="Admin", role_policy={"full_access": True},
              question=question, answer_purpose="answer_complex")
    ctx.conversation = conversation
    return ctx


@pytest.fixture
def captured(monkeypatch):
    sent = []

    def fake_complete(tenant_id, purpose, messages, **kw):
        sent.extend(m["content"] for m in messages)
        return SimpleNamespace(text="ok", model="fake", provider="fake",
                               prompt_tokens=1, completion_tokens=1, cost=0.0)

    monkeypatch.setattr(registry.llm, "complete", fake_complete)
    monkeypatch.setattr(registry, "_record_usage", lambda *a, **k: None)
    return sent


def test_docs_query_rewrite_redacts_the_question(captured):
    """The retrieval-retry rewrite sent the raw question while its siblings redacted it."""
    registry._rewrite_docs_query(_ctx(f"bagaimana prosedur komplain untuk {EMAIL}?"))
    blob = "\n".join(captured)
    assert EMAIL not in blob
    assert "[PII:EMAIL_1]" in blob


def test_evidence_text_redacts_sql_errors_and_source_labels():
    """Driver errors quote the offending literal, and an upload filename carries the
    name of a person -- both sat raw next to already-redacted chunk text."""
    ev = {
        "sql": {"ok": False, "code": "DRY_RUN_ERROR",
                "error": f"invalid input syntax for value '{EMAIL}' at row 3"},
        "docs": {"results": [{"citation": "c",
                              "citation_label": f"Kontrak {EMAIL}.pdf - Slide 3",
                              "text": "isi dokumen"}], "citations": ["c"]},
        "web": {"ok": True, "text": "hasil web",
                "sources": [{"label": f"profil {EMAIL}",
                             "url": f"https://x.test/u?e={EMAIL}"}]},
    }
    out = registry._evidence_text(ev, MDL)
    assert EMAIL not in out
    assert "[PII:EMAIL_1]" in out


def test_template_answer_masks_rows_and_text():
    """This string BECOMES the answer, and an answer goes back to a model later (memory
    capture, the next turn's history) -- so raw rows here cross the provider boundary."""
    ev = {"sql": {"ok": True, "columns": ["customer_email", "gmv"],
                  "rows": [[EMAIL, 100]], "pii_columns": ["customer_email"],
                  "safe_sql": "SELECT customer_email, gmv FROM orders",
                  "dialect": "postgres"},
          "memory": {"results": [{"text": f"kontak {EMAIL}"}]}}
    out = registry._template_answer(_ctx("total order"), ev)
    assert EMAIL not in out


def test_web_research_redacts_at_the_choke_point(monkeypatch):
    """This payload leaves the company for an external search service -- the strictest
    boundary in the system. Redacting at the choke point covers every caller, whatever
    it passes; the prompt had always CLAIMED the evidence was redacted."""
    from app import web_research

    sent = {}
    monkeypatch.setattr(web_research, "selected_provider", lambda *_a, **_k: "openai")
    monkeypatch.setattr(
        web_research, "_openai_call",
        lambda tenant_id, prompt: sent.setdefault("prompt", prompt) or {"ok": True})
    web_research.research("t1", f"cari info {EMAIL}",
                          f"bukti internal: {PHONE} dan NIK {NIK}")
    assert EMAIL not in sent["prompt"]
    assert PHONE not in sent["prompt"]
    assert NIK not in sent["prompt"]


def test_memory_capture_redacts_the_whole_turn(monkeypatch):
    """The richest PII surface in the pipeline: the user's question AND the answer."""
    from app.memory import capture

    sent = []
    monkeypatch.setattr(capture.llm, "available", lambda *_a, **_k: True)
    monkeypatch.setattr(
        capture.llm, "complete",
        lambda t, p, messages, **kw: sent.extend(m["content"] for m in messages) or
        SimpleNamespace(text="[]", model="fake", provider="fake",
                        prompt_tokens=1, completion_tokens=1, cost=0.0))
    monkeypatch.setattr(capture.store, "log_usage", lambda *a, **k: None)
    capture.auto_capture({"tenant_id": "t1", "key": "k", "role": "Admin"}, "u1",
                         f"pesanan {EMAIL}", f"nomor {PHONE}", "trace",
                         {"auto_memory": True, "persistent": True})
    blob = "\n".join(sent)
    assert EMAIL not in blob
    assert PHONE not in blob


def test_conversation_summary_redacts_history(monkeypatch):
    """Older turns are LLM-summarised; the whole history was going out verbatim."""
    from app.memory import conversation

    sent = []
    monkeypatch.setattr(conversation.llm, "available", lambda *_a, **_k: True)
    monkeypatch.setattr(
        conversation.llm, "complete",
        lambda t, p, messages, **kw: sent.extend(m["content"] for m in messages) or
        SimpleNamespace(text="ringkasan", model="fake", provider="fake",
                        prompt_tokens=1, completion_tokens=1, cost=0.0))
    conversation._summarize("t1", [{"role": "user", "content": f"hubungi {EMAIL}"},
                                   {"role": "assistant", "content": f"nomor {PHONE}"}], {})
    blob = "\n".join(sent)
    assert EMAIL not in blob
    assert PHONE not in blob


def test_chat_agent_prompt_uses_the_shared_redactor(monkeypatch):
    """The planner that carried this check is gone (chat overhaul item 3): the one chat
    agent writes every prompt of a chat turn, with or without a SQL datasource."""
    from app.agent import sql_agent
    seen = []

    def fake(tenant_id, purpose, messages, tools, **kw):
        seen.append("\n".join(m["content"] for m in messages))
        return SimpleNamespace(text="ok", model="m", provider="p", prompt_tokens=1,
                               completion_tokens=1, cost=0.0, tool_calls=[])

    monkeypatch.setattr(sql_agent.llm, "complete_tools", fake)
    ctx = Ctx(tenant_id="t", role="r", role_policy={}, question=f"kenapa {EMAIL} komplain")
    ctx.conversation = f"user: email saya {EMAIL}"
    ctx.chat_history = Conversation(turns=[{"role": "user", "content": f"email saya {EMAIL}"}])
    sql_agent.run_sql_agent(ctx, None, enable_docs=True)
    sql_agent.run_sql_agent(ctx, {"id": "d", "mdl": {"tables": {"t": ["a"]}}})
    assert len(seen) == 2 and all(EMAIL not in s for s in seen)
    assert all("[PII:EMAIL_1]" in s for s in seen)


@pytest.mark.parametrize("text", [
    "telepon 021-5551234",           # landline: the old pattern only matched mobile "8"
    "call +65 6123 4567",
    "(021) 555-1234",
    "+62 21 5551234",
    "Nama Lengkap: Budi Santoso",    # qualifier word between keyword and separator
    "Alamat Rumah: Jl. Merdeka 10",
])
def test_widened_patterns_catch_what_they_used_to_miss(text):
    assert "[PII:" in TextPiiRedactor().redact(text), text


@pytest.mark.parametrize("text", [
    "harga 1.000.000 rupiah", "revenue 9.200.000.000", "month_id 202607",
    "tanggal 2026-07-30", "total gmv per bulan", "naik 15% dibanding bulan lalu",
    "order 12345 sudah dikirim",
])
def test_widened_patterns_leave_business_figures_alone(text):
    """A redactor that eats revenue numbers would silently destroy every answer."""
    assert TextPiiRedactor().redact(text) == text, text
