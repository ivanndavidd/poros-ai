"""Minimal stubs so Wren's vendored policy.py runs standalone in this PoC.
Mirrors the interfaces of wren.config.WrenConfig and wren.model.error.*"""
from dataclasses import dataclass

@dataclass
class WrenConfig:
    strict_mode: bool = True
    denied_functions: frozenset = frozenset()

class ErrorCode:
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    BLOCKED_FUNCTION = "BLOCKED_FUNCTION"

class ErrorPhase:
    SQL_POLICY_CHECK = "SQL_POLICY_CHECK"

class WrenError(Exception):
    def __init__(self, code, message, phase=None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.phase = phase
