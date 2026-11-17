"""Multi-LLM layer (Feature 1.4): provider-agnostic gateway + gatekeeper + router.

Per-tenant model config lives in the metadata store (llm_providers, llm_models).
Everything degrades gracefully when LiteLLM isn't installed or no model is
configured, so the app still runs self-contained.
"""
from .gateway import complete, complete_tools, supports_tools, LLMResult, ToolResult, LLMUnavailable, available
from .gatekeeper import screen, GateDecision
from .router import pick_answer_purpose

__all__ = ["complete", "complete_tools", "supports_tools", "LLMResult", "ToolResult", "LLMUnavailable", "available",
           "screen", "GateDecision", "pick_answer_purpose"]
