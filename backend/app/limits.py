"""Resource ceilings for governed query execution.

`govern()` bounded WHAT a query may read but never HOW MUCH work it may cause, and no
connector set a timeout. A single `SELECT * FROM <large table>` over non-PII columns
passed every governance layer, was materialised whole by `fetchall()` and serialised to
JSON, driving a shared multi-tenant pod toward its memory limit; an expensive aggregation
simply ran until the customer's database gave up. Rate limiting bounds how OFTEN a caller
can ask, never how much one request costs.

Kept in one module so governance and the connectors cannot drift to different numbers.
"""
import os


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(os.getenv(name, str(default))), high))
    except (TypeError, ValueError):
        return default


def governed_max_rows() -> int:
    """Rows a governed query may return. 0 disables the cap."""
    return _int_env("GOVERNED_MAX_ROWS", 10_000, 0, 1_000_000)


def db_connect_timeout_sec() -> int:
    return _int_env("DB_CONNECT_TIMEOUT_SEC", 10, 1, 300)


def db_statement_timeout_ms() -> int:
    """Server-side ceiling per statement. 0 disables (not recommended)."""
    return _int_env("DB_STATEMENT_TIMEOUT_MS", 30_000, 0, 600_000)
