"""Shared machinery for turns that end in a QUESTION instead of an answer.

Two of them exist so far — which business, which entity — and they share a contract worth
stating in one place:

  * the question is DETERMINISTIC. No model decides what the person meant; guessing that is
    the failure these confirmations exist to prevent, and a guess wrapped in a confirmation
    is still a guess.
  * it travels as an ordinary 200 answer carrying a structured menu, so a consumer app that
    ignores the menu still shows the question to the one person who can answer it.
  * our own question is recognised again through client-supplied ``history`` by its opening
    sentence. History IS text — a consumer replays the transcript and there is no side
    channel to stamp — so the opening line is the only marker that survives the round trip.
"""
import re

_EN_HINT = re.compile(r"\b(what|which|who|when|where|why|how|the|of|for|and|show|list|"
                      r"total|sales|revenue|month|year|top|compare)\b", re.I)
_ID_HINT = re.compile(r"\b(berapa|apa|siapa|kapan|dimana|kenapa|mengapa|bagaimana|yang|"
                      r"dari|untuk|dan|dengan|total|penjualan|bulan|tahun|tertinggi)\b", re.I)


def looks_english(text: str) -> bool:
    """Indonesian is the default: these tenants curate and ask in Indonesian, and a wrong
    guess in that direction is the one the user already complained about."""
    body = text or ""
    return len(_EN_HINT.findall(body)) > len(_ID_HINT.findall(body))


def answered(history, *prefixes) -> bool:
    """True when the most recent assistant turn in `history` is one of these questions.

    Also the loop breaker: a client that re-sends without picking gets its question
    answered as typed rather than asked the same thing forever.
    """
    for turn in reversed(history or []):
        if not isinstance(turn, dict):
            continue
        if str(turn.get("role") or "").lower() != "assistant":
            continue
        text = str(turn.get("content") or "").lstrip()
        return any(text.startswith(prefix) for prefix in prefixes)
    return False


def previous_user(history, message: str) -> str:
    """The question that triggered the confirmation — the last user turn that is not the
    reply itself (some clients replay the current message into history)."""
    current = (message or "").strip().lower()
    for turn in reversed(history or []):
        if not isinstance(turn, dict) or str(turn.get("role") or "").lower() != "user":
            continue
        text = str(turn.get("content") or "").strip()
        if text and text.lower() != current:
            return text
    return ""


def asked_about(history, *prefixes) -> str:
    """The user question our last confirmation was ABOUT, or '' when the most recent
    assistant turn is not one of these questions.

    Walks to the user turn immediately BEFORE the ask instead of trusting the current
    message's absence from history — clients differ on whether they replay the message
    they are sending, and this works either way.
    """
    turns = [t for t in history or [] if isinstance(t, dict)]
    for i in range(len(turns) - 1, -1, -1):
        if str(turns[i].get("role") or "").lower() != "assistant":
            continue
        text = str(turns[i].get("content") or "").lstrip()
        if not any(text.startswith(prefix) for prefix in prefixes):
            return ""
        for j in range(i - 1, -1, -1):
            if str(turns[j].get("role") or "").lower() == "user":
                return str(turns[j].get("content") or "").strip()
        return ""
    return ""
