"""Make an elliptical follow-up self-contained before the deterministic layers read it.

Live Acme case (2026-09-24): turn 1 asked for GMV per client per month as graph + table
and the answer narrated one brand; turn 2 — "show me for each client" — came back as the
ORDER COUNT per client. The conversation history WAS in the prompt, as context, but every
deterministic layer between the gatekeeper and the model reads the raw message: the
request contract saw no metric and no chart, the precision cue saw nothing business-
critical (cheap model), the datasource router scored the word "client" alone, and the
business/entity/semantic confirmations had nothing to resolve. A message that only means
something against the previous turn lost everything that turn had established, and the
model — the one layer that still knew — guessed.

This module rewrites such a follow-up into ONE self-contained question and is built to
prefer doing nothing over guessing:

  * it runs only for a SHORT message that continues an existing conversation, whose gate
    verdict is a data lane, that carries no business-precision term of its own, when the
    last assistant turn is not one of our own confirmation questions (a pick is folded by
    the module that asked) and no structured confirmation reply is attached;
  * the draft is a model call, but its output is GATED deterministically: every content
    token must come from the current message or the user's OWN earlier turns (plural /
    prefix leniency only, digits verbatim). Assistant turns are never sent to the model —
    that is where invented requirements ("the biggest brand", "Samsung") come from;
  * the caller re-screens the draft with the gatekeeper THIS turn: it is derived from
    client-supplied history, the same rule business_confirm.selection_reply lives by;
  * any failure — no model, an ungrounded or unchanged draft, a screening block — leaves
    the turn byte-identical to today. Kill switch: FOLLOWUP_REWRITE=0.
"""
import os
import re
from dataclasses import dataclass

from . import business_confirm, confirm
from .llm import gateway, gatekeeper

# our own confirmation question — a reply to it is folded by business_confirm. Names,
# brands and KPI meanings are no longer asked (overhaul item 5): a correction after an
# assumed reading ("no, Philips Lighting") is an ordinary follow-up and rewritten here.
ASK_PREFIXES = (business_confirm.ASK_PREFIX_ID, business_confirm.ASK_PREFIX_EN)

MAX_WORDS = 14                  # a longer message states its own context
_MAX_DRAFT_CHARS = 600          # a runaway draft is not a question
_PRIOR_TURNS = 6                # how far back the user's own turns are read
_TOKEN = re.compile(r"[a-z0-9]+")
_QUOTES = "\"'`“”‘’«»"
_LABEL = re.compile(r"^(?:question|pertanyaan|rewritten|rewrite|output)\s*:\s*", re.I)

# Function words and generic request verbs the draft may use freely. Deliberately NOT in
# here: grouping cues (per/by/each/every/setiap/tiap/masing), outputs (chart/graph/table/
# dashboard), measures (total/much/many/nilai/jumlah) and time words — adding any of those
# changes what the question REQUIRES, so they must come from the user's own words.
_STOPWORDS = frozenset("""
a an the and or but of for to in on at from with as is are was were be been being am it
its this that these those there here what which who whom whose when where why how show me
us give tell list display see view get let make please can could would should will shall
may might you your yours i we my our ours do does did done have has had having all any
some more most less least than then also just only about into over under between out up
down not no yes ok okay same both again versus vs new now like want need so if else while
during since until till after before too very
yang dan atau dari ke di pada untuk dengan tentang sebagai adalah ini itu tersebut saja
juga lagi sudah belum akan telah bisa dapat boleh mau ingin perlu harus tolong mohon coba
silakan tampilkan tunjukkan berikan lihat cek buatkan buat kasih beri saya aku kami kita
anda kamu mereka dia nya ya tidak bukan jangan apa apakah bagaimana gimana mana dimana
kapan siapa berapa kenapa mengapa lalu kemudian terus lanjut lanjutkan tadi kalau jika
bila maka agar supaya sama seperti hanya cuma semua seluruh
""".split())

_SYSTEM = (
    "You rewrite the LATEST user message of an analytics chat into ONE self-contained "
    "question, using ONLY the user's earlier messages shown to you.\n"
    "Rules:\n"
    "1. Resolve what the latest message refers to (it, that, the same, each, for each, "
    "yang tadi, juga, saja, ...) from the earlier USER messages.\n"
    "2. Carry over only the details the latest message depends on: the metric, the "
    "breakdown (per X), the time range in the user's own words, the filters, and the "
    "requested outputs (chart, graph, table).\n"
    "3. NEVER add a metric, filter, time range, breakdown, output or number the user did "
    "not state.\n"
    "4. Keep the user's language and wording. Do not translate, do not answer, do not "
    "explain.\n"
    "5. If the latest message already stands on its own, return it unchanged.\n"
    "Output the question only."
)


@dataclass
class Draft:
    """Outcome of one rewrite attempt. ``question`` is None unless the draft was adopted;
    ``reason`` says why not ("unavailable" | "unchanged" | "empty" | "too_long" |
    "ungrounded:<token>"); ``usage`` is the metering entry when a model was billed."""
    question: str | None
    reason: str
    model: str = ""
    usage: dict | None = None


def enabled() -> bool:
    return os.getenv("FOLLOWUP_REWRITE", "1").strip().lower() not in ("0", "false", "off", "no")


def is_candidate(message: str, history, gate, confirmation, sql_intents,
                 standalone_cue=None) -> bool:
    """Whether this turn is an elliptical follow-up worth one cheap model call.

    ``standalone_cue`` (a compiled regex) names terms that make a short message stand on
    its own — a business metric ("gmv brand philips") already tells every layer what it is
    about, and rewriting it would only invite context it did not ask for.
    """
    if not enabled() or confirmation:
        return False
    if not (getattr(gate, "allow", False) and getattr(gate, "intent", "") in sql_intents):
        return False
    text = (message or "").strip()
    if not text or len(text.split()) > MAX_WORDS:
        return False
    if standalone_cue is not None and standalone_cue.search(text):
        return False
    if confirm.answered(history, *ASK_PREFIXES):
        return False                 # a reply to our own question; its module folds it
    return bool(gatekeeper._prior_user_turn(history, message))


def _tokens(text: str) -> list:
    return _TOKEN.findall((text or "").lower())


def _grounded(token: str, allowed: set) -> bool:
    """Is this token the user's own word? Exact, a stopword, a plural of one, or (>=4
    chars) a prefix/extension of one — "clients"/"client", "monthly"/"month". Digits
    must be verbatim: a year or a count the user never typed is an invented filter."""
    if token in allowed or token in _STOPWORDS:
        return True
    if token.isdigit():
        return False
    if token.rstrip("s") in allowed:
        return True
    if len(token) >= 4:
        return any(len(word) >= 4 and (word.startswith(token) or token.startswith(word))
                   for word in allowed)
    return False


def guard(draft: str, message: str, prior_turns) -> str | None:
    """None when every content token of the draft is the user's own; else the first token
    that is not — the audit trail names what the model tried to add."""
    allowed = set(_tokens(message))
    for turn in prior_turns:
        allowed.update(_tokens(turn))
    for token in _tokens(draft):
        if not _grounded(token, allowed):
            return token
    return None


def _clean(text: str) -> str:
    """One line, no wrapping quotes, no "Question:" label."""
    body = " ".join((text or "").split())
    body = body.strip(_QUOTES + " ")
    body = _LABEL.sub("", body).strip(_QUOTES + " ")
    return body


def _ask_model(tenant_id: str, prior_turns, message: str):
    """The one model call. Cheap lane on purpose — a one-sentence rewrite is what the
    Simple role exists for; answer_simple degrades to Primary when none is configured."""
    earlier = "\n".join(f"- {turn}" for turn in prior_turns)
    user = (f"Earlier user messages (oldest first):\n{earlier}\n\n"
            f"Latest user message:\n{message}")
    # max_tokens leaves room for a reasoning model's hidden thinking; the visible output
    # is one sentence (see gatekeeper._llm_screen for the empty-text failure at 200).
    return gateway.complete(tenant_id, "answer_simple",
                            [{"role": "system", "content": _SYSTEM},
                             {"role": "user", "content": user}],
                            temperature=0, max_tokens=1000)


def _usage_of(res) -> dict | None:
    entry = {"model": getattr(res, "model", "") or "",
             "prompt_tokens": int(getattr(res, "prompt_tokens", 0) or 0),
             "completion_tokens": int(getattr(res, "completion_tokens", 0) or 0),
             "cost": float(getattr(res, "cost", 0.0) or 0.0)}
    return entry if (entry["prompt_tokens"] or entry["completion_tokens"]) else None


def rewrite(tenant_id: str, message: str, history) -> Draft:
    """Draft a self-contained question for ``message`` from the user's own earlier turns.

    The caller decides what to do with an adopted draft (re-screen it, then use it); this
    function only decides whether the model produced something the user actually said.
    """
    prior = list(reversed(gatekeeper._recent_user_turns(history, message, limit=_PRIOR_TURNS)))
    if not prior:
        return Draft(None, "no_prior_turn")
    try:
        res = _ask_model(tenant_id, prior, message)
    except Exception:                       # no model / provider down / budget: as typed
        return Draft(None, "unavailable")
    model, usage = getattr(res, "model", "") or "", _usage_of(res)
    text = _clean(getattr(res, "text", "") or "")
    if not text:
        return Draft(None, "empty", model, usage)
    if len(text) > _MAX_DRAFT_CHARS:
        return Draft(None, "too_long", model, usage)
    if _tokens(text) == _tokens(message):
        return Draft(None, "unchanged", model, usage)
    bad = guard(text, message, prior)
    if bad:
        return Draft(None, f"ungrounded:{bad}", model, usage)
    return Draft(text, "rewritten", model, usage)
