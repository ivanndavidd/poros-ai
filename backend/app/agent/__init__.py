"""Agent orchestration (Feature 2.2): gatekeeper -> router -> one agent loop (tools over
DB + Docs + Memory, the model decides which) -> verifier -> answer.
"""
from .orchestrator import run_agent

__all__ = ["run_agent"]
