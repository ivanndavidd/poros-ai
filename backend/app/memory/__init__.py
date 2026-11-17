"""Per-(tenant, user) Memory (Feature 1.3 + v2): persistent store + semantic recall,
auto-capture (the AI writes its own notes), and conversation memory.

Hard tenant isolation: every read/write is keyed by (tenant_id, user_id) and never
crosses tenants. Embeddings reuse the RAG embedder (offline by default).
"""
from .recall import remember, recall, MEMORY_KINDS
from .conversation import (MEMORY_DEFAULTS, get_cfg, build_context, build_conversation,
                           as_messages, sanitize_history, HISTORY_RULE)
from .capture import auto_capture, schedule_capture

__all__ = ["remember", "recall", "MEMORY_KINDS", "MEMORY_DEFAULTS", "get_cfg",
           "build_context", "build_conversation", "as_messages", "sanitize_history", "HISTORY_RULE",
           "auto_capture", "schedule_capture"]
