from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict, Any


# ---- tenants / datasources / roles / keys ----
class TenantIn(BaseModel):
    name: str
    descr: str = ""
    icon: str = "hub"
    status: str = "active"

class TenantUpdateIn(BaseModel):
    name: Optional[str] = None
    descr: Optional[str] = None
    icon: Optional[str] = None
    status: Optional[str] = None
    owner_email: Optional[str] = None    # admin-only: reassign owner ("" = unassign)


class TenantGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    descr: str = Field(default="", max_length=500)
    tenant_ids: List[str] = Field(default_factory=list)
    user_ids: List[str] = Field(default_factory=list)


class TenantGroupUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    descr: Optional[str] = Field(default=None, max_length=500)
    tenant_ids: Optional[List[str]] = None
    user_ids: Optional[List[str]] = None


class ConfigIn(BaseModel):
    data: dict

class DataSourceIn(BaseModel):
    tenant_id: str
    type: str
    name: str
    conn: dict

class TestConnIn(BaseModel):
    # tenant_id is REQUIRED so the endpoint can be scoped like every other write:
    # without it, any signed-in console user could make the server dial an arbitrary
    # host:port and read the outcome from the error — an outbound probe, not a test.
    tenant_id: str
    type: str
    conn: dict

class AsyncTestConnIn(BaseModel):
    tenant_id: str
    type: str
    conn: dict

class DSConnIn(BaseModel):
    """Replace a datasource's connection config (e.g. rotate password, or switch
    BigQuery to keyless WIF/impersonation). The whole conn is replaced."""
    conn: dict

class DSUpdateIn(BaseModel):
    """Datasource lifecycle. enabled=False PAUSES the source: connection config, MDL
    curation and attached documents are all kept, but no answering path can see it."""
    enabled: Optional[bool] = None

class DocUpdateIn(BaseModel):
    """Document lifecycle: archived=True hides a stale version from retrieval (kept on disk)."""
    archived: Optional[bool] = None

class DocConfigIn(BaseModel):
    """Per-document RAG config edit -> re-chunk + re-embed that one document (from its stored
    original). The embedding model is per-tenant, so only chunking + OCR live here."""
    chunk_size: Optional[int] = None       # words per chunk
    chunk_overlap: Optional[int] = None    # words of overlap between chunks
    ocr: Optional[bool] = None             # read text baked into images (PPT/PDF)

class MdlPatchIn(BaseModel):
    allowed_tables: Optional[List[str]] = None
    pii_columns: Optional[List[str]] = None
    denied_functions: Optional[List[str]] = None
    allowed_functions: Optional[List[str]] = None
    descriptions: Optional[Dict[str, str]] = None
    # semantic catalog (curated-data layer)
    metrics: Optional[List[Dict[str, Any]]] = None            # [{name, definition, note?}]
    reference_values: Optional[Dict[str, List[Any]]] = None   # {dimension: [valid values]}
    grain: Optional[Dict[str, str]] = None                    # {table: dedup/grain note}
    examples: Optional[List[Dict[str, Any]]] = None           # [{q, sql}]
    business_concepts: Optional[List[Dict[str, Any]]] = None  # canonical term -> table/metric/time
    routing_aliases: Optional[List[str]] = None                # business phrases -> this datasource

class RoleIn(BaseModel):
    tenant_id: str
    name: str
    policy: dict

class ApiKeyIn(BaseModel):
    tenant_id: str
    role: str = ""                       # legacy/unused — role now arrives per request via JWT
    label: str = ""
    env: str = "Production"
    rate_limit: Optional[int] = None
    expiry: Optional[str] = None

class ApiKeyUpdateIn(BaseModel):
    env: Optional[str] = None
    rate_limit: Optional[int] = None
    expiry: Optional[str] = None
    label: Optional[str] = None

class RateLimitIn(BaseModel):
    tenant_id: str
    per_min: int

class JwtMintIn(BaseModel):
    tenant_id: str
    user_id: str
    role: str
    ttl_min: int = 60

class RoleMappingIn(BaseModel):
    external_id: str                     # the CLIENT system's role/group id (opaque string)
    role_name: str                       # one of OUR tenant roles it maps onto


# ---- businesses (knowledge scope: Tenant > Role > Business > Datasource) ----
class BusinessIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # what this slice covers, in the admin's words. Not decoration: it is the text the
    # business confirmation question shows the end user when asking which one they mean.
    descr: str = Field(default="", max_length=500)
    status: str = "active"


class BusinessUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    descr: Optional[str] = Field(default=None, max_length=500)
    status: Optional[str] = None
    # False = left out of the chat's "which business?" menu; still usable when named
    in_chat: Optional[bool] = None


class BusinessKnowledgeIn(BaseModel):
    """Curation a business applies to ONE datasource; None leaves that field unchanged.

    pii_columns is absent on purpose. PII is a hard rule and stays on the datasource, so
    one business cannot mark a column sensitive while another leaves it exposed.
    """
    allowed_tables: Optional[List[str]] = None
    descriptions: Optional[Dict[str, str]] = None
    metrics: Optional[List[Dict[str, Any]]] = None
    reference_values: Optional[Dict[str, List[Any]]] = None
    examples: Optional[List[Dict[str, Any]]] = None
    business_concepts: Optional[List[Dict[str, Any]]] = None
    routing_aliases: Optional[List[str]] = None


class RoleBusinessesIn(BaseModel):
    business_ids: List[str] = Field(default_factory=list)


# ---- consumer ----
class QueryIn(BaseModel):
    sql: str
    datasource_id: Optional[str] = None
    business: Optional[str] = None      # knowledge slice, by id or name ("3P")

class SqlConsoleIn(BaseModel):
    """Console SQL editor: a script run as a tenant role, governed like /v1/query."""
    sql: str
    datasource_id: Optional[str] = None
    role: Optional[str] = None          # tenant role to run as; None = the tenant's first role
    business: Optional[str] = None      # optional slice of that role, by id or name

class ChatIn(BaseModel):
    message: str
    datasource_id: Optional[str] = None
    user_id: Optional[str] = None       # end-user identity (for per-user memory)
    history: Optional[list] = None      # prior turns [{role: user|assistant, content}] — conversation
                                        # memory (v2); client-supplied, sanitized server-side (stateless)
    analysis_mode: str = "auto"         # auto | deep (quality-first) | fast (bounded/low-latency)
                                        # | brand (alias client/klien: the fixed brand analysis;
                                        # no brand named = the brand is an open reading)
    business: Optional[str] = None      # knowledge slice, by id or name ("3P"); which tables and
                                        # which curation this question is answered from
    session_id: Optional[str] = None    # the app's own conversation id: groups this turn with
                                        # the rest of its session in the audit trail (display
                                        # only; an id that is not a plain token is ignored)
    confirmation: Optional[Dict[str, Any]] = None
                                        # accepted for older clients and unused: the metric
                                        # question whose {token, choice_id} it carried is an
                                        # open reading since chat overhaul item 5

class MbrReportIn(BaseModel):
    """Monthly Business Review package request: one brand, one calendar month."""
    brand: str = Field(min_length=1, max_length=100)
    period: str = Field(pattern=r"^\d{4}-\d{2}$")      # review month, e.g. "2026-09"
    business: Optional[str] = None                        # knowledge slice by id or name
    targets: Optional[Dict[str, float]] = None            # NMV target in IDR typed by the
                                                          # requester: {"2026-09": 1e9,
                                                          # "2026-10": 1.2e9}; review month
                                                          # and the month after only
    inputs: Optional[str] = Field(default=None, pattern=r"^inp_[0-9a-f]{32}$")
                                                          # closed report input (attached
                                                          # exports) read by this report only

class FeedbackIn(BaseModel):
    """End-user feedback for one /v1/chat result.

    Question/answer are looked up server-side from the trace so a consumer cannot
    forge the content being reviewed.
    """
    trace_id: str
    rating: str                         # helpful | unhelpful
    reason: Optional[str] = None        # incorrect | incomplete | irrelevant | unclear | outdated | other
    comment: Optional[str] = None

class RetrieveIn(BaseModel):
    query: str
    top_k: int = 5
    datasource_id: Optional[str] = None


class ConsoleChatSessionIn(BaseModel):
    id: str
    title: str = ""
    created: Any = None
    updated: Any = None
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    # which business this conversation runs in, once its owner picked one. Kept with the
    # transcript so reopening the session does not re-ask a question already answered.
    business: Optional[Dict[str, Any]] = None


# ---- model setup (1.4) ----
class ProviderIn(BaseModel):
    tenant_id: str
    provider: str                        # openai | anthropic | gemini | azure | ...
    label: str = ""
    api_key: str = ""
    api_base: str = ""
    extra: Dict[str, Any] = {}

# Only generation knobs may cross the API boundary into `llm_models.params`. That blob is
# splatted into the LLM SDK call, so an unconstrained key set is arbitrary keyword
# injection: `api_base` redirects the request (taking the tenant's provider key with it),
# `mock_response` forges model output, `custom_llm_provider` reroutes the transport.
ALLOWED_MODEL_PARAMS = frozenset({
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "presence_penalty",
    "frequency_penalty", "stop", "seed", "reasoning_effort", "timeout_sec",
})


class ModelIn(BaseModel):
    tenant_id: str
    purpose: str                         # gatekeeper | answer_simple | answer_complex | embedding
    provider: str
    model: str
    params: Dict[str, Any] = {}
    monthly_budget_usd: float = 0.0
    fallback_model: str = ""
    enabled: bool = True

    @field_validator("params")
    @classmethod
    def _only_generation_knobs(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        rejected = sorted(set(value or {}) - ALLOWED_MODEL_PARAMS)
        if rejected:
            raise ValueError(f"unsupported model params: {', '.join(rejected)}")
        return value or {}

class BudgetIn(BaseModel):
    tenant_id: str
    monthly_usd: float


# ---- memory admin (1.3) ----
class MemoryIn(BaseModel):
    tenant_id: str
    user_id: str
    kind: str = "fact"                   # fact | preference | context
    text: str
