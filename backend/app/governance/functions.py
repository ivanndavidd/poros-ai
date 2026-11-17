"""Dialect-aware SQL function allowlist for governed analytics queries.

Read-only SQL is not automatically harmless: several databases expose sleep, file,
network, and extension functions from SELECT statements. The governed path therefore
allows a conservative analytics vocabulary and lets an administrator add reviewed
functions through ``mdl.allowed_functions``. A small hard-deny set always wins.
"""
from __future__ import annotations

import re

from sqlglot import exp

from ._compat import WrenError


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# sqlglot may canonicalise a dialect spelling (DATE_TRUNC) to a class key
# (TimestampTrunc). Both spellings are included where needed.
_COMMON = {
    # aggregates / statistics
    "sum", "count", "avg", "min", "max", "anyvalue", "arrayagg",
    "stringagg", "groupconcat", "countif", "approxdistinct", "approxcountdistinct",
    "stddev", "stddevpop", "stddevsamp", "variance", "varpop", "varsamp",
    "percentilecont", "percentiledisc", "quantile", "median",
    # numeric
    "abs", "round", "ceil", "ceiling", "floor", "power", "pow", "sqrt",
    "mod", "ln", "log", "log10", "exp", "sign", "greatest", "least",
    "safeadd", "safedivide", "safemultiply", "safenegate", "safesubtract",
    # null / conditional / conversion
    "and", "or", "case", "coalesce", "nullif", "if", "iff", "ifnull", "nvl", "nvl2", "cast",
    "trycast", "safecast", "convert", "decode",
    # strings
    "lower", "upper", "trim", "ltrim", "rtrim", "substring", "substr",
    "replace", "concat", "concatws", "length", "charlength", "characterlength",
    "left", "right", "strpos", "position", "split", "splitpart",
    "regexpcontains", "regexpextract", "regexpreplace", "regexpinstr",
    # date/time
    "currentdate", "currenttimestamp", "currenttime", "date", "datetime",
    "timestamp", "datetrunc", "timestamptrunc", "timestrtotime", "timetostr",
    "tsordstodate", "datestrtodate", "dateadd", "datesub", "datediff",
    "datefromparts", "datepart", "extract", "formatdate", "formatdatetime",
    "formattimestamp", "parsedate", "parsedatetime", "parsetimestamp",
    "todate", "tochar", "year", "month", "day", "lastday", "strftime",
    # windows
    "rownumber", "rank", "denserank", "percentrank", "cumedist", "ntile",
    "lag", "lead", "firstvalue", "lastvalue", "nthvalue",
    # safe JSON/array scalar operations
    "jsonextract", "jsonextractscalar", "jsonvalue", "jsonquery",
    "arraysize", "arraylength", "arraytostring",
}

_DIALECT = {
    "postgres": {"datebin", "makeinterval", "widthbucket"},
    "mysql": {"dateformat", "strtodate", "timestampdiff", "timestampadd"},
    "duckdb": {"epoch", "epochms", "makedate", "monthname", "dayname"},
    "bigquery": {
        "formatdate", "formatdatetime", "formattimestamp", "parsedate",
        "parsedatetime", "parsetimestamp", "dateadd", "datesub", "datediff",
        "timestampadd", "timestampsub", "timestampdiff",
    },
    "tsql": {"dateadd", "datediff", "datediffbig", "eomonth", "format"},
}

_HARD_DENY = {
    "sleep", "pgsleep", "benchmark", "waitfor",
    "pgreadfile", "pgreadbinaryfile", "pgstatfile", "pglsdir",
    "loadfile", "loadextension", "install", "attach", "detach",
    "loimport", "loexport", "sysreadfile", "syswritefile",
    "syseval", "sysexec", "dblink", "dblinkexec",
    "httpget", "httppost", "urlfetch", "readcsv", "readcsvauto",
    "readjson", "readparquet", "readblob", "glob",
}


def _function_keys(func: exp.Func) -> set[str]:
    if isinstance(func, exp.Anonymous):
        return {_norm(func.name)}
    keys = {_norm(type(func).key)}
    try:
        keys.add(_norm(func.sql_name()))
    except Exception:
        pass
    return {k for k in keys if k}


def validate_function_allowlist(ast: exp.Expression, dialect: str, additions=None) -> None:
    """Reject functions outside the safe analytics vocabulary.

    ``additions`` is an MDL-level allowlist extension for reviewed, datasource-specific
    scalar/aggregate functions. Obvious side-effect functions remain blocked even when
    accidentally added.
    """
    allowed = set(_COMMON)
    allowed.update(_DIALECT.get((dialect or "").lower(), set()))
    allowed.update(_norm(x) for x in (additions or []) if _norm(x))

    for func in ast.find_all(exp.Func):
        keys = _function_keys(func)
        if keys & _HARD_DENY:
            name = func.name if isinstance(func, exp.Anonymous) else func.sql_name()
            raise WrenError("BLOCKED_FUNCTION",
                            f"Function '{name}' is never allowed in governed SQL")
        if not keys & allowed:
            name = func.name if isinstance(func, exp.Anonymous) else func.sql_name()
            raise WrenError(
                "FUNCTION_NOT_ALLOWED",
                f"Function '{name}' is not in the governed allowlist for dialect "
                f"'{dialect}'. Add it to MDL allowed_functions only after review.",
            )
