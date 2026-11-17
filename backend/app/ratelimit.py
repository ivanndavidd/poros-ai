"""Shared Postgres token-bucket rate limiter, keyed per opaque API-key id."""
from . import store


def check(tenant_id: str, api_key: str):
    """Return (allowed, retry_after_seconds, limit, remaining).

    Per-key rate_limit wins; otherwise the tenant limit applies. PostgreSQL is the
    authoritative state, so adding uvicorn workers/VM replicas cannot multiply the
    effective allowance.
    """
    key = store.get_api_key_by_id(api_key)
    limit = (key.get("rate_limit") if key else None) or store.get_rate_limit(tenant_id)
    return store.consume_rate_limit(tenant_id, api_key, limit)


def reset(api_key: str = None):
    """Test/operator helper: clear one bucket or all buckets."""
    ref = api_key
    if api_key and str(api_key).startswith("sk_"):
        found = store.get_api_key(api_key)
        ref = found["key"] if found else api_key
    store.reset_rate_limit(ref)
