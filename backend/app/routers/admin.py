import json, os, re, time
from fastapi import APIRouter, Depends, HTTPException
from .. import store
from ..auth import validate_jwt_config
from .. import vectorstore as vs
from ..api_errors import raise_safe
from ..runtime_config import production        # one source of truth for hardened-mode
from ..console_auth import console_guard, current_principal, ensure_tenant_access, require_admin_role
from ..models import (TenantIn, TenantUpdateIn, TenantGroupIn, TenantGroupUpdateIn,
                      ConfigIn, DataSourceIn, DSConnIn, DSUpdateIn, RoleIn, ApiKeyIn,
                      ApiKeyUpdateIn, TestConnIn, MdlPatchIn, RateLimitIn, JwtMintIn,
                      RoleMappingIn, ConsoleChatSessionIn,
                      BusinessIn, BusinessUpdateIn, BusinessKnowledgeIn,
                      RoleBusinessesIn)

from ..memory.conversation import MEMORY_DEFAULTS   # single source of truth — these knobs are WIRED (v2)

from ..connectors.factory import make_connector, CONNECTOR_SPECS
from ..schema_scan import scan_connector
from .. import business_scope, jobs, mdl_merge, semantic_contract, semantic_resolver

_CONFIG_DEFAULTS = {
    # chunk_size / chunk_overlap / ocr are now PER-DOCUMENT (set at upload, editable per doc);
    # only the embedding model + tenant-wide policies live here.
    "document": {"embedding_model": "offline", "max_documents": 0, "auto_reindex": False},
    "memory": dict(MEMORY_DEFAULTS),
    # "required": every question must name a business. OFF by default — switching it on
    # changes what consumer apps have to send, so it is the tenant's decision, not ours.
    "business": {"required": False, "confirm_entities": False},
    # "strict_metrics": a KPI phrase with more than one curated meaning REFUSES (409 with
    # the choices) instead of being settled by datasource order. Its own section, not a
    # third key in "business": set_config REPLACES a section wholesale and the console
    # posts the business pair on every save, which would silently wipe this flag.
    "semantic": dict(semantic_resolver.CONFIG_DEFAULTS),
    # "engine": which chat engine answers this tenant — "classic" (deterministic layers
    # pick the business, values and tables before the model reads the question) or
    # "agentic" (the model reads the Knowledge Base through tools). Own section, same
    # reason as "semantic".
    "chat": {"engine": "classic"},
}

# console_guard: authenticates (session user OR legacy master key) AND enforces
# tenant ownership for tenant_manager on every {tid}/{did}/{doc_id}/{key} route.
# Endpoints that carry tenant_id in the BODY call ensure_tenant_access() themselves.
router = APIRouter(prefix="/admin", tags=["admin (control plane)"], dependencies=[Depends(console_guard)])


def _err(status, code, message):
    raise HTTPException(status, {"code": code, "message": message})


@router.get("/whoami")
def whoami(p: dict = Depends(current_principal)):
    """Identity bootstrap for the console UI. Works in BOTH deployment modes: standalone
    (session token / master key) and behind Apps Gateway (the injected X-User-Id header).
    Deliberately under /admin — a NON-reserved path the gateway forwards — because the
    gateway owns /auth/**, so /auth/me is unreachable behind it. Returns current_principal
    ({user_id, email, name, role, provider, via})."""
    return p


# ---- tenants ----
@router.get("/tenants")
def list_tenants(p: dict = Depends(current_principal)):
    # Admin sees all tenants. A manager sees directly-owned tenants plus every tenant
    # assigned through their tenant-group memberships.
    return store.list_tenants(owner_id=None if p["role"] == "admin" else p["user_id"])

@router.post("/tenants")
def create_tenant(b: TenantIn, p: dict = Depends(current_principal)):
    # the creator becomes the owner, so a manager-created tenant shows up in their
    # view AND (like everything) in the admin's. Master key creates unowned tenants.
    return {"id": store.create_tenant(b.name, b.descr, b.icon, b.status, owner_id=p["user_id"])}

@router.patch("/tenants/{tid}")
def update_tenant(tid: str, b: TenantUpdateIn, p: dict = Depends(current_principal)):
    store.update_tenant(tid, b.name, b.descr, b.icon, b.status)
    if b.owner_email is not None:                      # reassign ownership: admin only
        if p["role"] != "admin":
            _err(403, "ADMIN_ONLY", "hanya admin yang boleh memindahkan kepemilikan tenant")
        if b.owner_email == "":
            store.set_tenant_owner(tid, None)
        else:
            u = store.get_console_user_by_email(b.owner_email)
            if not u:
                _err(404, "USER_NOT_FOUND", f"user '{b.owner_email}' tidak ditemukan")
            store.set_tenant_owner(tid, u["id"])
    return {"ok": True}

@router.delete("/tenants/{tid}")
def delete_tenant(tid: str):
    store.delete_tenant(tid); return {"ok": True}


# ---- tenant groups (platform-admin-managed console authorization) ----
def _clean_group_name(name):
    value = (name or "").strip()
    if not value:
        _err(400, "GROUP_NAME_REQUIRED", "nama kelompok wajib diisi")
    return value


def _validate_group_name(name, current_id=None):
    value = _clean_group_name(name)
    existing = store.get_tenant_group_by_name(value)
    if existing and existing["id"] != current_id:
        _err(409, "GROUP_NAME_TAKEN", f"kelompok '{value}' sudah ada")
    return value


def _unique_or_none(values):
    return list(dict.fromkeys(values or [])) if values is not None else None


def _validate_group_tenants(tenant_ids):
    missing = [tid for tid in tenant_ids if not store.get_tenant(tid)]
    if missing:
        _err(400, "GROUP_TENANT_INVALID",
             f"tenant tidak ditemukan: {', '.join(missing[:5])}")


def _validate_group_users(user_ids):
    invalid = []
    for uid in user_ids:
        user = store.get_console_user(uid)
        if not user or user.get("role") != "tenant_manager":
            invalid.append(uid)
    if invalid:
        _err(400, "GROUP_USER_INVALID",
             "kelompok hanya dapat berisi akun Tenant Manager yang valid")


def _validate_group_members(tenant_ids=None, user_ids=None):
    clean_tenants = _unique_or_none(tenant_ids)
    clean_users = _unique_or_none(user_ids)
    if clean_tenants is not None:
        _validate_group_tenants(clean_tenants)
    if clean_users is not None:
        _validate_group_users(clean_users)
    return clean_tenants, clean_users


@router.get("/tenant-groups", dependencies=[Depends(require_admin_role)])
def list_tenant_groups():
    return store.list_tenant_groups()


@router.post("/tenant-groups", dependencies=[Depends(require_admin_role)])
def create_tenant_group(b: TenantGroupIn):
    name = _validate_group_name(b.name)
    tenant_ids, user_ids = _validate_group_members(b.tenant_ids, b.user_ids)
    gid = store.create_tenant_group(name, b.descr)
    store.replace_tenant_group_members(gid, tenant_ids, user_ids)
    return next(g for g in store.list_tenant_groups() if g["id"] == gid)


@router.patch("/tenant-groups/{gid}", dependencies=[Depends(require_admin_role)])
def update_tenant_group(gid: str, b: TenantGroupUpdateIn):
    if not store.get_tenant_group(gid):
        _err(404, "GROUP_NOT_FOUND", "kelompok tenant tidak ditemukan")
    name = _validate_group_name(b.name, gid) if b.name is not None else None
    tenant_ids, user_ids = _validate_group_members(b.tenant_ids, b.user_ids)
    store.update_tenant_group(gid, name, b.descr)
    store.replace_tenant_group_members(gid, tenant_ids, user_ids)
    return next(g for g in store.list_tenant_groups() if g["id"] == gid)


@router.delete("/tenant-groups/{gid}", dependencies=[Depends(require_admin_role)])
def delete_tenant_group(gid: str):
    if not store.get_tenant_group(gid):
        _err(404, "GROUP_NOT_FOUND", "kelompok tenant tidak ditemukan")
    store.delete_tenant_group(gid)
    return {"ok": True}


@router.get("/monitoring")
def monitoring(range: str = "all", p: dict = Depends(current_principal)):
    range_key = str(range or "all").strip().lower()
    if range_key not in {"all", "30d", "7d"}:
        _err(400, "RANGE_INVALID", "range harus all, 30d, atau 7d")
    out = store.monitoring_summary(
        owner_id=None if p["role"] == "admin" else p["user_id"],
        range_key=range_key,
    )
    # this process only — every replica shares the env, so one answer stands for all;
    # it exists because nothing else reports what JOB_WORKERS actually produced on the pod
    out["job_workers"] = jobs.worker_summary()
    return out


@router.get("/audit-delivery-status", dependencies=[Depends(require_admin_role)])
def audit_delivery_status():
    return store.admin_audit_delivery_status()


@router.get("/security-status", dependencies=[Depends(require_admin_role)])
def security_status():
    """Which hardening mode this instance runs in, and what a hardened start would refuse.

    Exists so switching APP_ENV is a checked move instead of a gamble: read this on a
    running relaxed instance, close everything it lists, THEN set APP_ENV. Without it the
    only way to discover a missing variable was a container that refuses to boot.

    Names the failing checks, never a secret value. Platform-admin only.
    """
    from .. import security
    return security.security_report()


def _chat_owner(p):
    return p.get("user_id") or "master-key"


@router.get("/tenants/{tid}/chat-sessions")
def list_chat_sessions(tid: str, p: dict = Depends(current_principal)):
    return store.list_console_chat_sessions(tid, _chat_owner(p))


@router.post("/tenants/{tid}/chat-sessions")
def save_chat_session(tid: str, b: ConsoleChatSessionIn,
                      p: dict = Depends(current_principal)):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", b.id or ""):
        _err(400, "SESSION_ID_INVALID", "session id tidak valid")
    payload = b.model_dump() if hasattr(b, "model_dump") else b.dict()
    payload["title"] = str(payload.get("title") or "")[:160]
    payload["messages"] = list(payload.get("messages") or [])[-30:]
    if len(json.dumps(payload, default=str).encode("utf-8")) > 2 * 1024 * 1024:
        _err(400, "SESSION_TOO_LARGE", "riwayat sesi melebihi batas 2 MB")
    store.upsert_console_chat_session(tid, _chat_owner(p), payload)
    return {"ok": True, "id": b.id}


@router.delete("/tenants/{tid}/chat-sessions/{session_id}")
def delete_chat_session(tid: str, session_id: str,
                        p: dict = Depends(current_principal)):
    store.delete_console_chat_session(tid, _chat_owner(p), session_id)
    return {"ok": True}


# ---- vector store (pgvector mirror) — platform infra, admin only ----
@router.get("/vector/status", dependencies=[Depends(require_admin_role)])
def vector_status():
    """Which vector backend is active + pgvector column geometry. SQLite is the
    always-present source of truth; pgvector is an optional accelerated mirror."""
    return {"backend": vs.backend(), "pgvector_enabled": vs.enabled(),
            "table": vs._table(), "dim": vs._dim()}

@router.post("/vector/rebuild", dependencies=[Depends(require_admin_role)])
def vector_rebuild(tenant_id: str = None):
    """(Re)build the pgvector mirror from the SQLite source of truth — run once after
    switching VECTOR_BACKEND to pgvector, or to recover from drift. Pass ?tenant_id=
    to rebuild a single tenant; omit for all tenants."""
    if not vs.enabled():
        _err(400, "NOT_PGVECTOR", "VECTOR_BACKEND is not 'pgvector' — nothing to rebuild")
    try:
        return vs.rebuild(tenant_id)
    except Exception as e:
        raise_safe(400, "REBUILD_FAILED",
                   "Vector index tidak dapat dibangun ulang. Periksa log backend menggunakan request id.",
                   e, context=f"vector rebuild tenant={tenant_id or '*'}")

@router.get("/tenants/{tid}/config/{section}")
def get_config(tid: str, section: str):
    section = section.strip().lower()   # 'JWT'/'Jwt' must hit the same masked/encrypted path
    if section == "jwt":
        # secret is NEVER returned — only whether one is set (masking, like provider keys)
        cfg = store.get_jwt_config(tid) or dict(store.JWT_CONFIG_DEFAULTS)
        return {**cfg, "secret": "", "has_secret": bool(cfg.get("secret"))}
    cfg = store.get_tenant_config(tid, section)
    if cfg is None:
        cfg = _CONFIG_DEFAULTS.get(section, {})
    return cfg

@router.post("/tenants/{tid}/config/{section}")
def set_config(tid: str, section: str, b: ConfigIn):
    section = section.strip().lower()   # a cased/typo'd 'JWT' must never store plaintext
    if section == "jwt":
        # encrypt-at-rest + keep-existing-secret-on-blank live in store.set_jwt_config
        candidate = {**(store.get_jwt_config(tid) or store.JWT_CONFIG_DEFAULTS), **(b.data or {})}
        try:
            validate_jwt_config(candidate)
        except ValueError as exc:
            _err(400, "JWT_CONFIG_INVALID", str(exc))
        store.set_jwt_config(tid, b.data); return {"ok": True}
    store.set_tenant_config(tid, section, b.data); return {"ok": True}


# ---- connection form metadata ----
@router.get("/connector-specs")
def connector_specs():
    """Drives the typed connection forms in the UI (fields per DB type)."""
    return CONNECTOR_SPECS


# ---- datasources ----
@router.get("/tenants/{tid}/datasources")
def list_ds(tid: str):
    # never leak raw secrets to the console
    # include_disabled: the console is the ONE caller that must still see a paused source —
    # every answering path gets the filtered default.
    out = []
    for ds in store.list_datasources(tid, include_disabled=True):
        ds = {**ds, "conn": {k: ("***" if k in ("password", "credentials") else v) for k, v in ds["conn"].items()}}
        out.append(ds)
    return out

@router.post("/test-connection")
def test_connection(b: TestConnIn, p: dict = Depends(current_principal)):
    """Introspect WITHOUT saving, so admins can validate before creating a datasource.

    Scoped like the async twin (`POST /admin/jobs/datasource-test`), which the console
    actually uses. Unscoped, this endpoint let ANY signed-in console user aim the server
    at any host:port and tell open ports from closed ones by the error text — and behind
    the Apps Gateway "any signed-in console user" is any employee the gateway admits.
    """
    ensure_tenant_access(p, b.tenant_id)
    if b.type == "docs":
        return {"ok": True, "tables": [], "note": "documentation source — no SQL schema"}
    try:
        intro = make_connector(b.type, b.conn).introspect()
    except Exception as e:
        raise_safe(400, "CONNECT_FAILED",
                   "Koneksi / scan gagal. Periksa host, kredensial, dan akses jaringan; "
                   "gunakan request id untuk melihat detailnya di log backend.",
                   e, context=f"test-connection type={b.type} tenant={b.tenant_id}")
    return {"ok": True, "tables": sorted(intro.keys()), "table_count": len(intro)}

@router.post("/datasources")
def create_ds(b: DataSourceIn, p: dict = Depends(current_principal)):
    ensure_tenant_access(p, b.tenant_id)
    if b.type == "docs":
        mdl = {"kind": "docs", "tables": {}, "allowed_tables": [], "pii_columns": [],
               "denied_functions": [], "allowed_functions": []}
        return {"id": store.create_datasource(b.tenant_id, b.type, b.name, b.conn, mdl), "mdl": mdl}
    try:
        connector = make_connector(b.type, b.conn)
        mdl = scan_connector(connector)
    except Exception as e:
        raise_safe(400, "CONNECT_FAILED",
                   "Koneksi / scan gagal. Periksa host, kredensial, dan akses jaringan; "
                   "gunakan request id untuk melihat detailnya di log backend.",
                   e, context=f"create datasource type={b.type} tenant={b.tenant_id}")
    did = store.create_datasource(b.tenant_id, b.type, b.name, b.conn, mdl)
    return {"id": did, "mdl": mdl}

@router.patch("/datasources/{did}")
def update_ds(did: str, b: DSUpdateIn):
    """Pause or resume a datasource.

    Disabling is the reversible alternative to deleting: the connection config, the curated
    MDL and any attached documents all stay, but the source becomes invisible to routing,
    cross-datasource planning, /v1/query and document retrieval. Use it to take a broken or
    expensive source out of service without losing hand-written curation.

    Admin operations (rescan, auto-enrich, connection rotation) deliberately KEEP working
    while paused, so a source can be repaired before it is switched back on.
    """
    ds = store.get_datasource(did)
    if not ds:
        _err(404, "NOT_FOUND", "datasource not found")
    if b.enabled is not None:
        store.set_datasource_enabled(did, b.enabled)
    return {**store.get_datasource(did), "conn": {"redacted": True}}


@router.patch("/datasources/{did}/conn")
def update_ds_conn(did: str, b: DSConnIn):
    """Rotate/replace ONLY the connection config (e.g. new password, or switch BigQuery
    to keyless WIF/impersonation). VALIDATE-FIRST: the new conn must connect+introspect
    before anything is saved — a failed attempt leaves the working config untouched.
    MDL (allowlist/PII/descriptions/catalog) and documents are preserved."""
    ds = store.get_datasource(did)
    if not ds:
        _err(404, "NOT_FOUND", "datasource not found")
    if ds["type"] == "docs":
        _err(400, "NOT_SQL", "documentation source has no connection to update")
    try:
        intro = make_connector(ds["type"], b.conn).introspect()
    except Exception as e:
        raise_safe(400, "CONNECT_FAILED",
                   "Koneksi / scan gagal dengan konfigurasi BARU; konfigurasi lama tetap "
                   "dipakai. Gunakan request id untuk melihat detailnya di log backend.",
                   e, context=f"rotate conn datasource={did}")
    store.update_datasource_conn(did, b.conn)
    return {"ok": True, "table_count": len(intro)}

@router.post("/datasources/{did}/rescan")
def rescan(did: str):
    ds = store.get_datasource(did)
    if not ds:
        _err(404, "NOT_FOUND", "datasource not found")
    if ds["type"] == "docs":
        return ds["mdl"]
    fresh = scan_connector(make_connector(ds["type"], ds["conn"]))
    # MERGE: preserve admin enrichment (manual allowlist narrowing, extra PII, curation),
    # minus curation the fresh scan no longer supports — see mdl_merge.
    try:
        merged, pruned = mdl_merge.merge_scan(ds["mdl"], fresh)
    except mdl_merge.ScanLostAllTables as e:
        _err(400, "SCAN_EMPTY", f"scan tidak menemukan tabel apa pun; katalog lama tetap dipakai ({e})")
    store.update_mdl(did, merged)
    # Businesses curate the same tables. A name the scan no longer returns must not survive
    # in a business allowlist, or questions keep routing to a table govern() then refuses.
    scoped = store.prune_business_tables(did, list((merged.get("tables") or {}).keys()))
    # `pruned` is a report for the admin (never persisted): what curation was parked
    # because its table/column vanished, and what came back from quarantine.
    return {**merged, "pruned": pruned, "business_pruned": scoped}

@router.patch("/datasources/{did}/mdl")
def patch_mdl(did: str, b: MdlPatchIn):
    """Admin review/enrich: edit allowlist, mark/unmark PII, add descriptions before publish."""
    ds = store.get_datasource(did)
    if not ds:
        _err(404, "NOT_FOUND", "datasource not found")
    mdl = ds["mdl"]
    if b.allowed_tables is not None:
        mdl["allowed_tables"] = b.allowed_tables
    if b.pii_columns is not None:
        mdl["pii_columns"] = b.pii_columns
    if b.denied_functions is not None:
        mdl["denied_functions"] = b.denied_functions
    if b.allowed_functions is not None:
        mdl["allowed_functions"] = b.allowed_functions
    if b.descriptions is not None:
        mdl["descriptions"] = b.descriptions
    if b.metrics is not None:
        mdl["metrics"] = b.metrics
    if b.reference_values is not None:
        mdl["reference_values"] = b.reference_values
    if b.grain is not None:
        mdl["grain"] = b.grain
    if b.examples is not None:
        mdl["examples"] = b.examples
    if b.business_concepts is not None:
        try:
            mdl["business_concepts"] = semantic_contract.validate_definitions(
                mdl, b.business_concepts,
            )
        except ValueError as exc:
            _err(400, "SEMANTIC_CONTRACT_INVALID", str(exc))
    if b.routing_aliases is not None:
        mdl["routing_aliases"] = semantic_contract.normalize_routing_aliases(
            b.routing_aliases,
        )
    store.update_mdl(did, mdl); return mdl

@router.post("/datasources/{did}/auto-enrich")
def auto_enrich_ds(did: str, use_samples: bool = True):
    """Use the tenant's configured LLM to DRAFT the curation layer (descriptions / metrics /
    reference_values / examples), grounded in the real schema + PII-masked sample data.
    Non-destructive: only fills empty fields / new description keys (admin still edits + saves).
    Manual (button) trigger — sampling costs money on BigQuery, so it never runs on connect."""
    ds = store.get_datasource(did)
    if not ds:
        _err(404, "NOT_FOUND", "datasource not found")
    if ds["type"] == "docs":
        _err(400, "NOT_SQL", "documentation source has no schema to enrich")
    from .. import mdl_enrich
    from ..llm.gateway import LLMUnavailable
    try:
        out = mdl_enrich.enrich(ds["tenant_id"], ds, use_samples=use_samples)
    except LLMUnavailable as e:
        _err(400, "NO_MODEL", f"belum ada model aktif untuk tenant ini — atur dulu di tab Model ({e})")
    except Exception as e:
        _err(400, "ENRICH_FAILED", f"auto-enrich gagal: {e}")
    store.update_mdl(did, out["mdl"])
    return out

@router.delete("/datasources/{did}")
def delete_ds(did: str):
    store.delete_datasource(did); return {"ok": True}


# ---- roles ----
@router.get("/tenants/{tid}/roles")
def list_roles(tid: str):
    return store.list_roles(tid)

@router.post("/roles")
def create_role(b: RoleIn, p: dict = Depends(current_principal)):
    ensure_tenant_access(p, b.tenant_id)
    if store.get_role(b.tenant_id, b.name):
        store.update_role(b.tenant_id, b.name, b.policy); return {"updated": True}
    return {"id": store.create_role(b.tenant_id, b.name, b.policy)}

@router.delete("/tenants/{tid}/roles/{name}")
def delete_role(tid: str, name: str):
    store.delete_role(tid, name); return {"ok": True}


# ---- businesses (knowledge scope: Tenant > Role > Business > Datasource) ----
# A business is a named slice of the tenant's data that MAY span datasources (Acme: 3P reads
# L2+L3, 1P reads L2 only). The curation lives per (business, datasource) pair; the
# datasource keeps the scanned schema and pii_columns.
def _business_or_404(tid: str, bid: str) -> dict:
    """A business, verified to belong to `tid`.

    console_guard checked ownership against the tid in the PATH, so the business id must be
    re-checked against it — otherwise another tenant's business is reachable by guessing.
    """
    b = store.get_business(bid)
    if not b or b["tenant_id"] != tid:
        _err(404, "NOT_FOUND", "business not found for this tenant")
    return b


def _ds_or_404(tid: str, did: str) -> dict:
    ds = store.get_datasource(did)
    if not ds or ds["tenant_id"] != tid:
        _err(404, "NOT_FOUND", "datasource not found for this tenant")
    return ds


@router.get("/tenants/{tid}/businesses")
def list_businesses(tid: str):
    """Every business of a tenant with its per-datasource curation and role assignments —
    one call feeds the whole Knowledge Base tree."""
    return store.list_businesses(tid)


@router.post("/tenants/{tid}/businesses")
def create_business(tid: str, b: BusinessIn):
    if store.get_business_by_name(tid, b.name):
        _err(409, "BUSINESS_EXISTS", f"business '{b.name}' already exists for this tenant")
    return {"id": store.create_business(tid, b.name, b.descr, b.status)}


@router.patch("/tenants/{tid}/businesses/{bid}")
def update_business(tid: str, bid: str, b: BusinessUpdateIn):
    _business_or_404(tid, bid)
    if b.name is not None:
        clash = store.get_business_by_name(tid, b.name)
        if clash and clash["id"] != bid:
            _err(409, "BUSINESS_EXISTS", f"business '{b.name}' already exists for this tenant")
    store.update_business(bid, b.name, b.descr, b.status, b.in_chat)
    return store.get_business(bid)


@router.delete("/tenants/{tid}/businesses/{bid}")
def delete_business(tid: str, bid: str):
    """Deleting a business discards its curation for good. status='paused' is the
    reversible alternative and is what the console offers first."""
    _business_or_404(tid, bid)
    store.delete_business(bid)
    return {"ok": True}


@router.get("/tenants/{tid}/businesses/{bid}/datasources/{did}/knowledge")
def get_business_knowledge(tid: str, bid: str, did: str):
    """The curation a business applies to one datasource, alongside what the datasource can
    actually offer (scanned tables, its own allowlist, PII) so the editor can render its
    pickers and the allowlist ceiling without a second round-trip."""
    business = _business_or_404(tid, bid)
    ds = _ds_or_404(tid, did)
    known = store.get_business_knowledge(bid, did)
    mdl = ds["mdl"]
    allowed, _ = business_scope.narrow_tables(mdl, (known or {}).get("allowed_tables"))
    return {
        # absent != empty: a datasource that is not part of the business must stay
        # unreachable for it, which is not the same as "in scope but nothing curated yet".
        "in_business": known is not None,
        "knowledge": known if known is not None else store.blank_knowledge(),
        # What this slice gets from the WAREHOUSE without curating anything -- the same
        # function the answering path uses, so the editor can never report "0 described"
        # about a prompt that actually carries hundreds. Read-only: saving must not turn
        # inherited annotation into a copy the business then owns and has to maintain.
        "inherited": business_scope.inherited_for(mdl, known, allowed),
        "semantic_findings": _semantic_findings(tid, business),
        "datasource": {"id": ds["id"], "name": ds["name"], "type": ds["type"],
                       "enabled": ds["enabled"], "tables": mdl.get("tables", {}),
                       "allowed_tables": mdl.get("allowed_tables", []),
                       "pii_columns": mdl.get("pii_columns", [])},
    }


def _semantic_findings(tid: str, business: dict) -> list:
    """Alias collisions / broken replica authority across THIS business's whole slice.

    Runs over the same scoped views the answering path reads, so the panel warns about
    exactly the catalog the model sees — surfacing (design doc phase 1) what strict mode
    would refuse and what observe mode is logging."""
    try:
        return semantic_resolver.findings(
            business_scope.visible_datasources(tid, business))
    except Exception:
        return []    # a broken sibling datasource must not take the editor down with it


@router.patch("/tenants/{tid}/businesses/{bid}/datasources/{did}/knowledge")
def set_business_knowledge(tid: str, bid: str, did: str, b: BusinessKnowledgeIn):
    """Upsert the curation a business applies to one datasource — this is also how a
    datasource JOINS a business (an empty body attaches it with blank curation).

    The allowlist is INTERSECTED with the datasource's own: a business may only narrow what
    the connection already permits, never widen it. Anything dropped is reported back
    rather than silently discarded. Concepts and aliases are re-validated on EVERY save,
    not only when they were edited — narrowing the allowlist can orphan a concept that was
    valid a moment earlier, and an orphaned contract is what govern() would refuse at
    answer time.
    """
    business = _business_or_404(tid, bid)
    ds = _ds_or_404(tid, did)
    mdl = ds["mdl"]
    current = store.get_business_knowledge(bid, did)
    k = dict(current if current is not None else store.blank_knowledge())

    dropped = []
    if b.allowed_tables is not None:
        k["allowed_tables"], dropped = business_scope.narrow_tables(mdl, b.allowed_tables)
    for field in ("descriptions", "metrics", "reference_values", "examples",
                  "business_concepts", "routing_aliases"):
        value = getattr(b, field)
        if value is not None:
            k[field] = value
    try:
        k = business_scope.validate(mdl, k)
    except ValueError as exc:
        _err(400, "SEMANTIC_CONTRACT_INVALID", str(exc))

    store.set_business_knowledge(bid, did, k)
    return {"knowledge": store.get_business_knowledge(bid, did), "dropped_tables": dropped,
            # Re-scanned AFTER the write: this save may have introduced (or fixed) a
            # collision with a concept curated on a sibling datasource of the business.
            "semantic_findings": _semantic_findings(tid, business)}


@router.delete("/tenants/{tid}/businesses/{bid}/datasources/{did}")
def remove_business_datasource(tid: str, bid: str, did: str):
    """Take a datasource out of a business. The connection, its schema and every other
    business that uses it are untouched — only this pairing's curation is dropped."""
    _business_or_404(tid, bid)
    _ds_or_404(tid, did)
    store.delete_business_scope(bid, did)
    return {"ok": True}


@router.get("/tenants/{tid}/roles/{name}/businesses")
def list_role_businesses(tid: str, name: str):
    if not store.get_role(tid, name):
        _err(404, "ROLE_NOT_FOUND", f"role '{name}' does not exist for this tenant")
    return store.list_role_businesses(tid, name)


@router.put("/tenants/{tid}/roles/{name}/businesses")
def set_role_businesses(tid: str, name: str, b: RoleBusinessesIn):
    """Replace the set of businesses a role may use. Unknown or foreign ids are rejected
    outright rather than skipped: a half-applied assignment is indistinguishable from a
    deliberate one when read back."""
    if not store.get_role(tid, name):
        _err(404, "ROLE_NOT_FOUND", f"role '{name}' does not exist for this tenant")
    owned = {x["id"] for x in store.list_businesses(tid)}
    unknown = [x for x in b.business_ids if x not in owned]
    if unknown:
        _err(400, "BUSINESS_NOT_FOUND", f"not a business of this tenant: {unknown}")
    store.set_role_businesses(tid, name, b.business_ids)
    return store.list_role_businesses(tid, name)


# ---- role mappings (external RBAC alias -> tenant role) ----
@router.get("/tenants/{tid}/role-mappings")
def list_role_mappings(tid: str):
    return store.list_role_mappings(tid)

@router.post("/tenants/{tid}/role-mappings")
def add_role_mapping(tid: str, b: RoleMappingIn):
    ext = b.external_id.strip()
    if not ext:
        _err(400, "EXTERNAL_ID_EMPTY", "external_id must not be empty")
    if not store.get_role(tid, b.role_name):
        _err(400, "ROLE_NOT_FOUND", f"role '{b.role_name}' is not defined for this tenant")
    store.upsert_role_mapping(tid, ext, b.role_name); return {"ok": True}

@router.delete("/tenants/{tid}/role-mappings/{external_id:path}")
def delete_role_mapping(tid: str, external_id: str):
    # `:path` convertor: external ids may contain '/' (GCP-style roles, LDAP DNs) —
    # servers decode %2F before routing, so a plain segment param could never match.
    store.delete_role_mapping(tid, external_id); return {"ok": True}

@router.get("/tenants/{tid}/unmapped-roles")
def unmapped_roles(tid: str):
    """External role values seen on verified JWTs that resolved to nothing — map or ignore."""
    return store.list_unmapped_roles(tid)


# ---- api keys ----
@router.get("/tenants/{tid}/apikeys")
def list_keys(tid: str):
    return store.list_api_keys(tid)

@router.post("/apikeys")
def create_key(b: ApiKeyIn, p: dict = Depends(current_principal)):
    ensure_tenant_access(p, b.tenant_id)
    raw = store.create_api_key(b.tenant_id, b.role, b.label, b.env, b.rate_limit, b.expiry)
    row = store.get_api_key(raw)
    return {"key": raw, "id": row["key"], "key_prefix": row["key_prefix"]}

@router.patch("/apikeys/{key_id}")
def update_key(key_id: str, b: ApiKeyUpdateIn):
    store.update_api_key(key_id, b.env, b.rate_limit, b.expiry, b.label); return {"ok": True}

@router.delete("/apikeys/{key_id}")
def revoke_key(key_id: str):
    store.revoke_api_key(key_id); return {"ok": True}

@router.post("/apikeys/{key_id}/rotate")
def rotate_key(key_id: str):
    new_key = store.rotate_api_key(key_id)
    if not new_key:
        _err(404, "NOT_FOUND", "api key not found")
    return {"key": new_key}


# ---- rate limit ----
@router.get("/tenants/{tid}/rate-limit")
def get_rate_limit(tid: str):
    return {"per_min": store.get_rate_limit(tid)}

@router.post("/rate-limit")
def set_rate_limit(b: RateLimitIn, p: dict = Depends(current_principal)):
    ensure_tenant_access(p, b.tenant_id)
    store.set_rate_limit(b.tenant_id, b.per_min); return {"ok": True}


# ---- end-user JWT (Pattern B) — DEV/TEST helper ----
@router.post("/mint-jwt")
def mint_jwt(b: JwtMintIn, p: dict = Depends(current_principal)):
    """Mint a signed end-user JWT for testing Pattern B. In production the tenant's own
    identity provider issues these; the platform only verifies them. Signs with the
    tenant's jwt config (secret/alg/role_claim/iss/aud) when set, else env JWT_SECRET.
    `role` may be a tenant role name OR a mapped external id (so mappings are testable)."""
    ensure_tenant_access(p, b.tenant_id)
    if production():
        _err(404, "DEV_HELPER_DISABLED", "JWT mint helper is disabled in production")
    cfg = store.get_jwt_config(b.tenant_id) or {}
    if cfg.get("jwks_url") or cfg.get("public_key"):
        _err(400, "JWT_MINT_EXTERNAL_IDP",
             "This tenant uses asymmetric JWT verification; mint test tokens in its identity provider")
    secret = cfg.get("secret") or os.getenv("JWT_SECRET")
    if not secret:
        _err(400, "JWT_SECRET_UNSET", "Set JWT_SECRET env on the server to mint/verify end-user JWTs")
    try:
        import jwt
    except Exception:
        _err(400, "PYJWT_MISSING", "pip install pyjwt on the server")
    if not (store.get_role(b.tenant_id, b.role) or store.get_role_mapping(b.tenant_id, b.role)):
        _err(400, "ROLE_NOT_FOUND", f"'{b.role}' is neither a defined role nor a mapped external id for this tenant")
    now_ts = int(time.time())
    claim = cfg.get("role_claim") or "role"
    payload = {"sub": b.user_id, claim: b.role, "iat": now_ts, "exp": now_ts + b.ttl_min * 60}
    if cfg.get("issuer"):
        payload["iss"] = cfg["issuer"]
    if cfg.get("audience"):
        payload["aud"] = cfg["audience"]
    algorithm = (cfg.get("algorithms") or ["HS256"])[0]
    if algorithm not in ("HS256", "HS384", "HS512"):
        _err(400, "JWT_MINT_ALGORITHM", "The local helper only supports HS* development tokens")
    token = jwt.encode(payload, secret, algorithm=algorithm)
    return {"token": token, "role": b.role, "user_id": b.user_id, "ttl_min": b.ttl_min}


# ---- usage ----
@router.get("/tenants/{tid}/usage")
def usage(tid: str):
    return store.usage_summary(tid)


# ---- audit log (full per-step governed-query trail) ----
@router.get("/tenants/{tid}/audit")
def audit(tid: str, limit: int = 100):
    return store.list_audit(tid, limit)


@router.get("/tenants/{tid}/conversations")
def conversations(tid: str, limit: int = 40, user_id: str = ""):
    """The audit trail as chat sessions (question, answer, status, feedback per turn, the
    turn's events kept for the detail view), newest session first."""
    return store.list_conversations(tid, limit, (user_id or "").strip()[:200])


@router.get("/security-audit", dependencies=[Depends(require_admin_role)])
def security_audit(limit: int = 200):
    """Append-only control-plane mutations (never includes request bodies/secrets)."""
    return store.list_admin_audit(limit)


# ---- end-user feedback (grouped by trace/question-answer in the console) ----
@router.get("/tenants/{tid}/feedback")
def feedback(tid: str, limit: int = 200):
    return store.list_feedback(tid, max(1, min(limit, 1000)))
