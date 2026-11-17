"""Ask the end user WHICH business a question belongs to, instead of guessing it.

Phase 3 made an unresolvable business an error. For ``/v1/query`` — raw SQL, nobody to ask
— that is the right answer. For a chat turn it is not: the person typing is exactly who
knows whether they meant 1P or 3P, so the turn should come back as a QUESTION carrying the
choices, not as a refusal the consumer app has to translate into one.

Nothing here is inferred by a model. Guessing the business is the failure this whole layer
exists to prevent ("What is GMV for Philips" answered from a 3P table called "Philipsome"),
and a guess with a confirmation step bolted on top is still a guess — it just moves the
blame to whoever clicked "yes". So the only ways a business gets chosen are: the caller
named it, the person picked it from this menu, or the person wrote it as a business in
the question ("in 1P business").
"""
import re

from . import business_scope, confirm, store

KIND = "business"

# The opening line the reply carries, so a later turn can recognise this question inside
# client-supplied history. See confirm.py for why the text itself is the marker.
ASK_PREFIX_ID = "Pertanyaan ini bisa dijawab dari lebih dari satu business"
ASK_PREFIX_EN = "This question can be answered from more than one business"


class Turn:
    """What the pipeline should do with this turn.

    Exactly one of ``business``/``ask`` is meaningful: with ``ask`` set the turn stops and
    the question is the reply, otherwise it runs inside ``business`` (which is still None
    for a tenant that has not adopted businesses).
    """

    __slots__ = ("business", "question", "ask")

    def __init__(self, business=None, question="", ask=None):
        self.business = business
        self.question = question
        self.ask = ask


def question_for(options: list, message: str) -> dict:
    """The confirmation payload: prose for a human, ``options`` for a UI to render."""
    english = confirm.looks_english(message)
    names = ", ".join(o["name"] for o in options)
    lines = [f"{ASK_PREFIX_EN} ({names}). Which one do you mean?" if english
             else f"{ASK_PREFIX_ID} ({names}). Yang mana yang Anda maksud?", ""]
    for option in options:
        descr = (option.get("descr") or "").strip()
        lines.append(f"- **{option['name']}**" + (f" — {descr}" if descr else ""))
    lines += ["", "Pick one and your question is answered from that business's data."
              if english else
              "Pilih salah satu, lalu pertanyaan Anda dijawab dari data business tersebut."]
    return {"kind": KIND, "question": "\n".join(lines), "options": options,
            "pending_question": message}


def selection_reply(tenant_id: str, role_name: str, ref, message: str, history):
    """``(ref, question)`` — fold "the user just typed the business name" back into the turn.

    A consumer app that renders ``confirmation`` resends the question with ``business`` set
    and never reaches this. One that only prints the answer text would otherwise ask the
    same question forever, because the person's reply ("3P") is not a question anyone can
    answer. Narrow on purpose: only when the previous assistant turn IS this confirmation
    and the whole message is one granted business name.

    A pinned ``ref`` does NOT short-circuit this: apps re-send the conversation's business
    with every turn, so the reply to a switch question ("GDV" while pinned to 1P) arrives
    with the OLD pin attached — and the person's own reply to a visible question outranks
    the app's memory. Anything more than a bare granted name keeps the pin untouched.

    Called BEFORE the gatekeeper deliberately. ``history`` is client-supplied, so a question
    recovered from it has to be screened this turn like any other input — recovering it
    afterwards would let a caller smuggle a payload in as a prior "user" turn and then fire
    it with a one-word message.
    """
    name = (message or "").strip()
    if not name or not confirm.answered(history, ASK_PREFIX_ID, ASK_PREFIX_EN):
        return ref, message
    hit = next((c for c in business_scope.choices(tenant_id, role_name)
                if c["name"].lower() == name.lower()), None)
    if not hit:
        return ref, message
    return hit["id"], confirm.previous_user(history, message) or message


def _mentioned(message: str, options: list) -> list:
    """Granted businesses whose NAME the message contains as a whole token.

    Deterministic on purpose — the same whole-identifier idea the routing and pruning
    layers use, so 'GDVX' never claims 'GDV' and '23P' never claims '3P'. Single-character
    names are skipped: too short to be a mention rather than an accident.
    """
    found = []
    for option in options:
        name = (option.get("name") or "").strip()
        if len(name) < 2:
            continue
        if re.search(rf"(?<![0-9A-Za-z_]){re.escape(name)}(?![0-9A-Za-z_])",
                     message or "", re.IGNORECASE):
            found.append(option)
    return found


def _named_as_business(message: str, options: list) -> list:
    """Granted businesses the message names AS a business: "in 1P business", "bisnis 3P".

    The person naming it in their own words is the person picking it, so asking "which
    business?" right after "the list of clients in 1P business" only read as not listening.
    The word business next to the name is required: a bare "GDV" can also be a metric term,
    and that one keeps the question.
    """
    found = []
    for option in _mentioned(message, options):
        name = re.escape(option["name"].strip())
        if re.search(rf"(?<![0-9A-Za-z_]){name}\s+(?:business|bisnis)\b|"
                     rf"\b(?:business|bisnis)\s+{name}(?![0-9A-Za-z_])",
                     message or "", re.IGNORECASE):
            found.append(option)
    return found


def switch_question(current: dict, named: list, message: str) -> dict:
    """Ask whether to stay on the pinned business or move to the one(s) the text names.

    The pin is the APP's memory of an earlier pick; the question is the PERSON's words.
    When they disagree ("for GDV?" while pinned to 1P) neither silently staying nor
    silently switching is honest — a business name can also be an ordinary metric term —
    so the turn ends in this menu: the named businesses first, the current one last.
    Opens with the same ASK prefix as ``question_for`` so a plain-text reply ("GDV") is
    recognised by ``selection_reply`` exactly like any other business confirmation.
    """
    english = confirm.looks_english(message)
    options = named + [{"id": current["id"], "name": current["name"],
                        "descr": (current.get("descr") or "").strip()}]
    names = ", ".join(o["name"] for o in options)
    mentioned = ", ".join(o["name"] for o in named)
    lines = [f"{ASK_PREFIX_EN} ({names}). This conversation is currently on business "
             f"{current['name']}, but your question names {mentioned}. Which one do you mean?"
             if english else
             f"{ASK_PREFIX_ID} ({names}). Percakapan ini sedang memakai business "
             f"{current['name']}, tetapi pertanyaan Anda menyebut {mentioned}. "
             "Yang mana yang Anda maksud?", ""]
    for option in options:
        descr = (option.get("descr") or "").strip()
        lines.append(f"- **{option['name']}**" + (f" — {descr}" if descr else ""))
    lines += ["", "Pick one and your question is answered from that business's data."
              if english else
              "Pilih salah satu, lalu pertanyaan Anda dijawab dari data business tersebut."]
    return {"kind": KIND, "question": "\n".join(lines), "options": options,
            "pending_question": message}


def for_turn(tenant_id: str, role_name: str, ref, message: str) -> Turn:
    """Resolve the business for a chat turn, or produce the question to ask instead.

    Still raises ``BusinessError`` for what a person cannot fix by picking: a business they
    named that is not theirs (offering a menu there would turn a refused escalation into a
    suggestion), and a role holding no grants at all (only an admin can grant one).
    """
    wanted = (ref or "").strip()
    if wanted:
        current = business_scope.resolve(tenant_id, role_name, wanted)
        # The ref is usually an app's per-conversation pin, replayed on every turn.
        # A question that names a DIFFERENT granted business ("for GDV?" while pinned
        # to 1P) is the person's words disagreeing with the app's memory — ask, never
        # follow either silently. Naming only the current business changes nothing,
        # and an ungranted name is not in `choices`, so it is never advertised.
        others = [c for c in _mentioned(message, business_scope.choices(tenant_id, role_name))
                  if c["id"] != current["id"]] if current else []
        if others:
            return Turn(None, message, ask=switch_question(current, others, message))
        return Turn(current, message)
    if not business_scope.is_required(tenant_id):
        return Turn(business_scope.resolve(tenant_id, role_name, None), message)
    options = business_scope.choices(tenant_id, role_name)
    if not options:
        business_scope.resolve(tenant_id, role_name, None)   # raises BUSINESS_NOT_GRANTED
    if len(options) == 1:
        # One grant is not a choice. Asking a question with a single possible answer is
        # friction, not governance — the slice is the same either way.
        return Turn(store.get_business(options[0]["id"]), message)
    named = _named_as_business(message, options)
    if len(named) == 1:
        return Turn(store.get_business(named[0]["id"]), message)
    # The menu offers only the businesses meant for chat; one the admin keeps out of it (the
    # MBR report's slice) still answers when the person or the app names it. When the menu
    # would be empty the whole grant list is offered rather than no way through.
    menu = [o for o in options if o.get("in_chat", True)] or options
    if len(menu) == 1:
        return Turn(store.get_business(menu[0]["id"]), message)
    return Turn(None, message, ask=question_for(
        [{k: o[k] for k in ("id", "name", "descr")} for o in menu], message))
