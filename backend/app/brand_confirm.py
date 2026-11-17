"""The brand mode without a brand named: every brand, or which one.

The brand mode (consumer-app's "Client analysis" toggle) analyses ONE brand. A question
about EVERY brand ("GMV split for all clients in 1P") is not a one-brand analysis at all: it
leaves the brand mode for the ordinary governed answer. Any other question that names no
brand used to be asked which one; since chat overhaul item 5 it is an open reading
(app/assumptions.py) and the model takes the brand the conversation is about, saying so.

Light on purpose (no app.agent import), like the module it was.
"""
import re

ALL = "*"                               # option id of "all brands"

_WHO = r"(?:brands?|clients?|klien|merek|merk)"
# A question about every brand rather than one: all/each/every/per/across brands or
# clients, and the Indonesian semua/seluruh/setiap/tiap/masing-masing.
_ALL = re.compile(
    rf"\b(?:(?:all|every|each)\s+(?:of\s+)?(?:the\s+|our\s+|my\s+)?{_WHO}|"
    rf"(?:semua|seluruh|setiap|tiap|masing[\s-]?masing)\s+{_WHO}|"
    rf"(?:per|by|across|antar|lintas)\s+{_WHO}|all\s+of\s+them)\b", re.I)


def about_all(text: str) -> bool:
    """Does this text ask about every brand rather than one?"""
    return bool(_ALL.search(text or ""))


def all_suffix(english: bool) -> str:
    return " (all brands)" if english else " (semua brand)"
