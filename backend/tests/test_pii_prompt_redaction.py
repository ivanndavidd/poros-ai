"""Hard rule (CLAUDE.md): PII is NEVER sent to an LLM.

Two real leaks this pins shut:
1. The single-shot NL->SQL prompt interpolated ctx.question and ctx.conversation RAW,
   while every other consumer of the same values redacts them (agent/sql_agent.py:138-141,
   skills/registry.py synthesis). Conversation text is client-supplied chat history and
   the question is whatever the end user typed, so both can carry PII.
2. TextPiiRedactor let an email address that ENDS a sentence through untouched — the
   trailing-period case, i.e. the most common way an address appears in prose. That gap
   affected EVERY redacted path (docs, memory, conversation, evidence), not just SQL.

Offline: llm.complete is monkeypatched and the outgoing prompt is captured.
"""
from types import SimpleNamespace

import pytest

from app.skills import registry
from app.skills.registry import TextPiiRedactor, Ctx

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
    """Capture every prompt handed to the model from the single-shot SQL path."""
    sent = []

    def fake_complete(tenant_id, purpose, messages, **kw):
        sent.extend(m["content"] for m in messages)
        return SimpleNamespace(text="SELECT order_id FROM orders", model="fake",
                               provider="fake", prompt_tokens=1, completion_tokens=1,
                               cost=0.0)

    monkeypatch.setattr(registry.llm, "complete", fake_complete)
    monkeypatch.setattr(registry, "_record_usage", lambda *a, **k: None)
    return sent


def test_single_shot_sql_prompt_redacts_the_question(captured):
    registry._llm_sql(_ctx(f"tampilkan order milik {EMAIL} dan {PHONE}"), MDL)
    blob = "\n".join(captured)
    assert EMAIL not in blob
    assert PHONE not in blob
    assert "[PII:EMAIL_1]" in blob and "[PII:PHONE_1]" in blob


def test_single_shot_sql_prompt_redacts_the_conversation(captured):
    registry._llm_sql(
        _ctx("bandingkan dengan bulan lalu",
             f"user: pesanan dari {EMAIL}\nassistant: NIK terkait {NIK}"),
        MDL,
    )
    blob = "\n".join(captured)
    assert EMAIL not in blob
    assert NIK not in blob


def test_single_shot_sql_prompt_still_carries_the_schema(captured):
    """Redaction must not gut the prompt — the model still needs the catalog."""
    registry._llm_sql(_ctx("total gmv"), MDL)
    blob = "\n".join(captured)
    assert "orders" in blob and "gmv" in blob
    assert "total gmv" in blob        # a question without PII passes through verbatim


@pytest.mark.parametrize("text,expect_clean", [
    (f"hubungi {EMAIL}.", True),          # END OF SENTENCE — the regression
    (f"hubungi {EMAIL}", True),
    (f"({EMAIL})", True),
    (f"{EMAIL}; lalu", True),
    (f"kirim ke {EMAIL}!", True),
    ("domain@sub.example.co.uk.", True),
])
def test_email_redaction_covers_sentence_endings(text, expect_clean):
    out = TextPiiRedactor().redact(text)
    assert ("@" not in out) is expect_clean, out
    assert "[PII:EMAIL_1]" in out


def test_redactor_does_not_eat_ordinary_text():
    r = TextPiiRedactor()
    assert r.redact("harga 1.000.000 rupiah") == "harga 1.000.000 rupiah"
    assert r.redact("tidak ada email di sini") == "tidak ada email di sini"
    # a bare domain is not an address and must survive
    assert r.redact("buka acme.co.id.") == "buka acme.co.id."


def test_same_value_gets_a_stable_token():
    """Stable tokens keep the model able to reason about 'the same customer'."""
    r = TextPiiRedactor()
    out = r.redact(f"{EMAIL} lalu {EMAIL}.")
    assert out.count("[PII:EMAIL_1]") == 2
