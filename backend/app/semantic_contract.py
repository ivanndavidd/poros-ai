"""Deterministic business-concept resolution for model-independent analytics.

An MDL describes what tables and columns exist.  A business semantic contract adds
the missing prescriptive layer: when a user says e.g. "sales", which table, metric,
time column and aggregation are authoritative for that tenant.

Models may still choose how to explain evidence, but they may not silently redefine
matched business concepts.  Contracts live inside each datasource MDL under
``business_concepts`` and are therefore versioned/edited with the rest of the
curated semantic catalog.
"""
from __future__ import annotations

import re
from typing import Iterable

import sqlglot
from sqlglot import exp


_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)
_VALID_AGGREGATIONS = {"SUM", "AVG", "MIN", "MAX", "COUNT", "COUNT_DISTINCT"}
_MAX_FORMULA_LENGTH = 1000
# v2 identity fields (optional per concept). concept_id is EXPLICIT and admin-approved —
# semantic identity is never derived from a display name, because two rows both named
# "GMV" may be two different numbers. source_binding marks deliberate physical replicas
# of ONE concept: same concept_id on several datasources counts as the same meaning only
# when every copy shares an equivalence_contract_id and exactly one is 'primary'
# (enforced cross-source in semantic_resolver; shapes are validated here).
_CONCEPT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")
_BINDING_ROLES = {"primary", "fallback"}


def _tokens(value: str) -> list[str]:
    return _TOKEN_RE.findall(str(value or "").lower())


def _phrase_pattern(value: str):
    tokens = _tokens(value)
    if not tokens:
        return None
    return re.compile(
        r"(?<![a-z0-9])" + r"[\s_-]+".join(map(re.escape, tokens)) + r"(?![a-z0-9])",
        re.I,
    )


def _resolve_column(columns: dict, raw: str, concept: str, table: str) -> str:
    column = columns.get(raw.lower())
    if not column:
        raise ValueError(
            f"business concept '{concept}' references unknown column '{table}.{raw}'"
        )
    return column


def _normalize_synonyms(name: str, values: Iterable) -> list[str]:
    synonyms, seen = [], {name.lower()}
    for raw in values or []:
        value = str(raw or "").strip()
        key = value.lower()
        if value and key not in seen:
            synonyms.append(value)
            seen.add(key)
    return synonyms[:30]


def normalize_routing_aliases(values: Iterable) -> list[str]:
    """Normalize admin-authored datasource vocabulary without guessing semantics."""
    aliases, seen = [], set()
    for raw in values or []:
        value = str(raw or "").strip()[:120]
        key = value.lower()
        if not value or not _tokens(value) or key in seen:
            continue
        aliases.append(value)
        seen.add(key)
        if len(aliases) >= 50:
            break
    return aliases


def _definition_fields(index: int, raw: dict) -> tuple[str, str, str, str, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"business concept #{index + 1} must be an object")
    fields = (
        str(raw.get("name") or "").strip(),
        str(raw.get("table") or "").strip(),
        str(raw.get("metric") or "").strip(),
        str(raw.get("formula") or "").strip(),
        str(raw.get("time_dimension") or "").strip(),
    )
    if not fields[0] or not fields[1] or not (fields[2] or fields[3]):
        raise ValueError(
            f"business concept #{index + 1} requires name, table, and either metric or formula"
        )
    return fields


def _claim_name(name: str, seen: set) -> None:
    key = name.lower()
    if key in seen:
        raise ValueError(f"duplicate business concept name: {name}")
    seen.add(key)


def _normalize_concept_id(name: str, raw, seen_ids: set) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return ""
    if not _CONCEPT_ID_RE.match(value):
        raise ValueError(
            f"business concept '{name}' has an invalid concept_id '{raw}' — use "
            "lowercase letters/digits with '_', '.' or '-', max 80 characters"
        )
    if value in seen_ids:
        raise ValueError(
            f"duplicate concept_id '{value}' in one datasource — a replica of the same "
            "concept lives on another datasource, never twice in one"
        )
    seen_ids.add(value)
    return value


def _normalize_binding(name: str, raw, concept_id: str) -> dict:
    if raw in (None, "", {}):
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"business concept '{name}' source_binding must be an object")
    role = str(raw.get("role") or "").strip().lower()
    contract = str(raw.get("equivalence_contract_id") or "").strip()[:120]
    if role not in _BINDING_ROLES:
        raise ValueError(
            f"business concept '{name}' source_binding role must be 'primary' or 'fallback'"
        )
    if not contract:
        raise ValueError(
            f"business concept '{name}' source_binding requires an equivalence_contract_id"
        )
    if not concept_id:
        raise ValueError(
            f"business concept '{name}' source_binding requires a concept_id — replicas "
            "must share an explicit identity"
        )
    return {"role": role, "equivalence_contract_id": contract}


def _canonical_table(name: str, raw: str, allowed: set,
                     table_names: dict) -> str:
    table = table_names.get(raw.lower())
    if not table:
        raise ValueError(f"business concept '{name}' references unknown table '{raw}'")
    if allowed and table not in allowed:
        raise ValueError(f"business concept '{name}' references non-allowlisted table '{table}'")
    return table


def _formula_expression(formula: str, concept: str) -> exp.Expression:
    if len(formula) > _MAX_FORMULA_LENGTH:
        raise ValueError(
            f"business concept '{concept}' formula is longer than {_MAX_FORMULA_LENGTH} characters"
        )
    try:
        statements = sqlglot.parse(f"SELECT {formula}")
    except Exception as exc:
        raise ValueError(
            f"business concept '{concept}' has an invalid SQL formula"
        ) from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise ValueError(
            f"business concept '{concept}' formula must be one SQL expression"
        )
    tree = statements[0]
    if len(tree.expressions) != 1 or tree.find(exp.Table):
        raise ValueError(
            f"business concept '{concept}' formula must be one SQL expression"
        )
    expression = tree.expressions[0]
    invalid_star = any(
        not isinstance(star.parent, exp.Count)
        for star in expression.find_all(exp.Star)
    )
    if expression.find(exp.Select) or expression.find(exp.Table) or invalid_star:
        raise ValueError(
            f"business concept '{concept}' formula cannot contain a query, table, or wildcard"
        )
    if not any(expression.find_all(exp.AggFunc)):
        raise ValueError(
            f"business concept '{concept}' formula must contain an aggregate function"
        )
    return expression


def _normalize_formula(formula: str, concept: str, columns: dict) -> str:
    expression = _formula_expression(formula, concept)
    for column in expression.find_all(exp.Column):
        if column.name.lower() not in columns:
            raise ValueError(
                f"business concept '{concept}' formula references unknown column "
                f"'{column.name}'"
            )
    return formula[:_MAX_FORMULA_LENGTH]


def _normalize_definition(index: int, raw: dict, tables: dict, allowed: set,
                          table_names: dict, seen: set, seen_ids: set) -> dict:
    name, table_raw, metric_raw, formula_raw, time_raw = _definition_fields(index, raw)
    _claim_name(name, seen)
    table = _canonical_table(name, table_raw, allowed, table_names)
    columns = {str(column).lower(): str(column) for column in (tables.get(table) or [])}
    metric = _resolve_column(columns, metric_raw, name, table) if metric_raw else ""
    formula = _normalize_formula(formula_raw, name, columns) if formula_raw else ""
    time_dimension = (
        _resolve_column(columns, time_raw, name, table) if time_raw else ""
    )
    aggregation = str(raw.get("aggregation") or "SUM").strip().upper()
    if aggregation not in _VALID_AGGREGATIONS:
        raise ValueError(
            f"business concept '{name}' uses unsupported aggregation '{aggregation}'"
        )
    concept_id = _normalize_concept_id(name, raw.get("concept_id"), seen_ids)
    out = {
        "name": name,
        "synonyms": _normalize_synonyms(name, raw.get("synonyms") or []),
        "table": table,
        "metric": metric,
        "formula": formula,
        "time_dimension": time_dimension,
        "aggregation": aggregation,
        "description": str(raw.get("description") or "").strip()[:1000],
    }
    # Optional v2 identity: stored only when set, so legacy concepts stay byte-identical.
    if concept_id:
        out["concept_id"] = concept_id
    binding = _normalize_binding(name, raw.get("source_binding"), concept_id)
    if binding:
        out["source_binding"] = binding
    return out


def _definition_scope(mdl: dict) -> tuple[dict, set, dict, set, set]:
    tables = mdl.get("tables") or {}
    return (
        tables,
        set(mdl.get("allowed_tables") or []),
        {str(name).lower(): str(name) for name in tables},
        set(),
        set(),
    )


def validate_definitions(mdl: dict, definitions: Iterable[dict]) -> list[dict]:
    """Validate and normalize admin-authored contracts against the current MDL."""
    tables, allowed, table_names, seen, seen_ids = _definition_scope(mdl)
    return [
        _normalize_definition(index, raw, tables, allowed, table_names, seen, seen_ids)
        for index, raw in enumerate(definitions or [])
    ]


def partition_definitions(mdl: dict, definitions: Iterable[dict]
                          ) -> tuple[list[dict], list[dict]]:
    """Triage contracts per definition: ``(normalized_valid, [{definition, reason}])``.

    Same checker as :func:`validate_definitions`, different failure mode. The admin editor
    wants all-or-nothing so a typo cannot be half-saved; a rescan instead needs to keep
    every contract that still resolves and report the ones the new schema broke.
    """
    tables, allowed, table_names, seen, seen_ids = _definition_scope(mdl)
    kept, rejected = [], []
    for index, raw in enumerate(definitions or []):
        try:
            kept.append(_normalize_definition(
                index, raw, tables, allowed, table_names, seen, seen_ids))
        except ValueError as exc:
            rejected.append({"definition": raw, "reason": str(exc)})
    return kept, rejected


def _best_alias_match(definition: dict, question: str):
    best = None
    for alias in [definition["name"], *(definition.get("synonyms") or [])]:
        pattern = _phrase_pattern(alias)
        match = pattern.search(question or "") if pattern else None
        if not match:
            continue
        candidate = (
            len(_tokens(alias)),
            match.end() - match.start(),
            -match.start(),
            match.start(),
            match.end(),
            alias,
        )
        if best is None or candidate[:3] > best[:3]:
            best = candidate
    return best


def _select_non_overlapping(candidates: list) -> list[dict]:
    occupied: list[tuple[int, int]] = []
    resolved = []
    for _token_count, _chars, _neg_start, start, end, alias, _order, definition in candidates:
        overlaps = any(
            start < used_end and end > used_start
            for used_start, used_end in occupied
        )
        if not overlaps:
            occupied.append((start, end))
            # An invalid/stale specific contract still claims its matching phrase so a
            # broader valid alias cannot silently reinterpret it. Example: stale
            # "offline sales" must not fall back to generic "sales".
            if definition is not None:
                resolved.append({**definition, "matched_alias": alias})
    return resolved


def match_candidates(mdl: dict, question: str) -> list[tuple]:
    """Every concept whose alias appears in the question, one tuple per concept:
    ``(token_count, chars, neg_start, start, end, alias, index, definition_or_None)``.

    The raw material :func:`resolve_mdl` selects from, exposed so ``semantic_resolver``
    can apply the SAME specificity rules across several datasources instead of per MDL.
    A concept that matches but fails normalization keeps its tuple with ``definition``
    None: a stale specific contract must still claim its phrase, or a broader valid
    alias would silently reinterpret it.
    """
    # Resolve valid definitions independently. A datasource rescan can legitimately
    # remove one stale table; that single stale contract must not disable every other
    # still-valid business concept in the datasource.
    source = mdl or {}
    tables = source.get("tables") or {}
    allowed = set(source.get("allowed_tables") or [])
    table_names = {str(name).lower(): str(name) for name in tables}
    candidates, seen, seen_ids = [], set(), set()
    for index, raw in enumerate(source.get("business_concepts") or []):
        if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
            continue
        best = _best_alias_match(raw, question)
        if not best:
            continue
        try:
            definition = _normalize_definition(
                index, raw, tables, allowed, table_names, seen, seen_ids,
            )
        except ValueError:
            definition = None
        candidates.append((*best, index, definition))
    return candidates


def resolve_mdl(mdl: dict, question: str) -> list[dict]:
    """Resolve non-overlapping concept aliases, preferring the most specific phrase.

    Thus ``sales volume`` can override a generic ``sales`` contract, while independent
    phrases such as ``sales and inventory`` can resolve two separate contracts.
    Invalid stale contracts are ignored safely until an admin repairs them.

    Single-MDL view: when the same alias is curated in MORE than one place, this keeps
    its historical index-order pick. Cross-datasource ownership, replica authority and
    ambiguity are ``semantic_resolver``'s job.
    """
    candidates = match_candidates(mdl, question)
    candidates.sort(key=lambda item: (-item[0], -item[1], item[3], item[6]))
    return _select_non_overlapping(candidates)


def resolve_datasources(datasources: Iterable[dict], question: str) -> list[dict]:
    """Resolve contracts across authorized datasources in deterministic source order."""
    out = []
    for datasource in datasources or []:
        for contract in resolve_mdl(datasource.get("mdl") or {}, question):
            out.append({
                **contract,
                "datasource_id": datasource.get("id"),
                "datasource_name": datasource.get("name"),
            })
    return out


def prompt_block(contracts: Iterable[dict]) -> str:
    contracts = list(contracts or [])
    if not contracts:
        return ""
    lines = [
        "MANDATORY BUSINESS SEMANTIC CONTRACTS (server-resolved; do not reinterpret):",
    ]
    for contract in contracts:
        definition = contract.get("formula") or (
            f"{contract['aggregation']}({contract['metric']})"
        )
        time_part = (
            f"; time dimension={contract['time_dimension']}"
            if contract.get("time_dimension") else ""
        )
        note = f"; meaning={contract['description']}" if contract.get("description") else ""
        lines.append(
            f"- User phrase '{contract.get('matched_alias') or contract['name']}' means "
            f"{contract['name']}: {definition} "
            f"from {contract['table']}{time_part}{note}."
        )
    lines.append(
        "For the matched concept, the table and metric above are authoritative. "
        "Other tables may only provide clearly-labelled supplementary evidence."
    )
    return "\n".join(lines)


def required_tables(contracts: Iterable[dict]) -> list[str]:
    return list(dict.fromkeys(
        str(contract.get("table") or "")
        for contract in (contracts or [])
        if contract.get("table")
    ))


def _aggregate_matches(node: exp.AggFunc, contract: dict) -> bool:
    metric = str(contract.get("metric") or "").lower()
    aggregation = str(contract.get("aggregation") or "SUM").upper()
    columns = {str(column.name).lower() for column in node.find_all(exp.Column)}
    if metric not in columns:
        return False
    key = str(getattr(node, "key", "") or "").upper()
    if aggregation == "COUNT_DISTINCT":
        return key == "COUNT" and bool(node.find(exp.Distinct))
    return key == aggregation


def _expression_signature(expression: exp.Expression) -> str:
    """Stable comparison for a contract formula and a generated SELECT expression.

    Table qualifiers and identifier quoting do not change a metric formula's meaning, so
    they are removed. SQLGlot then normalizes function spellings (for example COUNTIF)
    before whitespace/case are folded.
    """
    node = expression.copy()
    if isinstance(node, exp.Alias):
        node = node.this
    for column in node.find_all(exp.Column):
        column.set("catalog", None)
        column.set("db", None)
        column.set("table", None)
    rendered = node.sql()
    return re.sub(r"\s+", "", rendered).replace("`", "").replace('"', "").lower()


def _formula_matches(tree: exp.Select, contract: dict) -> bool:
    formula = str(contract.get("formula") or "").strip()
    if not formula:
        return False
    try:
        expected = _formula_expression(formula, str(contract.get("name") or "metric"))
    except ValueError:
        return False
    expected_signature = _expression_signature(expected)
    return any(
        _expression_signature(expression) == expected_signature
        for expression in tree.expressions
    )


def _aggregate_keys(aggregates: Iterable[exp.AggFunc]) -> set[str]:
    return {str(getattr(node, "key", "") or "").upper() for node in aggregates}


def _is_validation_query(tree: exp.Select, aggregates: list[exp.AggFunc]) -> bool:
    """Allow the agent's usual MIN/MAX/COUNT schema-range probe.

    A lone COUNT is not treated as validation because it may itself be the final business
    answer (and must therefore obey COUNT_DISTINCT/formula contracts).
    """
    if tree.args.get("group") is not None or len(aggregates) < 2:
        return False
    keys = _aggregate_keys(aggregates)
    return bool(keys & {"MIN", "MAX"}) and keys <= {"MIN", "MAX", "COUNT"}


def _parse_select(sql: str, dialect: str) -> exp.Select | None:
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except Exception:
        return None  # governance owns syntax diagnostics
    return tree if isinstance(tree, exp.Select) else None


def _must_enforce(contract: dict, keys: set[str], grouped: bool, has_aggregate: bool) -> bool:
    """Whether this SQL is a business result the contract must govern.

    A formula contract binds as soon as the query aggregates at all: the whole point of a
    formula is that the shape of the aggregation is not inferable from a single column.
    """
    if str(contract.get("formula") or "").strip() and has_aggregate:
        return True
    aggregation = str(contract.get("aggregation") or "SUM").upper()
    count_result = aggregation in {"COUNT", "COUNT_DISTINCT"} and "COUNT" in keys
    return grouped or bool(keys & {"SUM", "AVG"}) or count_result


def _contract_satisfied(tree: exp.Select, aggregates: list[exp.AggFunc],
                        contract: dict) -> bool:
    if str(contract.get("formula") or "").strip():
        return _formula_matches(tree, contract)
    return any(_aggregate_matches(node, contract) for node in aggregates)


def _matched_contracts(tree: exp.Select, contracts: list[dict]) -> list[dict]:
    used_tables = {str(table.name).lower() for table in tree.find_all(exp.Table)}
    return [
        contract for contract in contracts
        if str(contract.get("table") or "").lower() in used_tables
    ]


def _wrong_table_error(contracts: list[dict]) -> str:
    expected = ", ".join(dict.fromkeys(
        str(contract.get("table") or "") for contract in contracts
    ))
    return (
        "This query does not use a canonical table for the resolved business "
        f"concept(s). Use one of: {expected}."
    )


def _contract_error(contract: dict) -> str:
    required = str(contract.get("formula") or "").strip() or (
        f"{contract['aggregation']}({contract['metric']})"
    )
    return (
        f"Business concept '{contract['name']}' requires "
        f"{required} from '{str(contract.get('table') or '')}'."
    )


def sql_violation(sql: str, dialect: str, contracts: Iterable[dict]) -> str | None:
    """Return a deterministic semantic-contract error for direct/fast analytics.

    Validation-only SQL (MIN/MAX/COUNT without GROUP BY) may omit the primary metric,
    but a grouped or SUM/AVG business result must use the canonical aggregation.
    """
    contracts = list(contracts or [])
    if not contracts:
        return None
    tree = _parse_select(sql, dialect)
    if tree is None:
        return None
    matched = _matched_contracts(tree, contracts)
    if not matched:
        return _wrong_table_error(contracts)
    aggregates = list(tree.find_all(exp.AggFunc))
    if _is_validation_query(tree, aggregates):
        return None
    keys = _aggregate_keys(aggregates)
    grouped = tree.args.get("group") is not None
    # A comparison may resolve several independent concepts whose canonical tables
    # should be queried separately. Enforce only the contract(s) represented by this
    # SQL, not every contract from the whole question.
    for contract in matched:
        if not _must_enforce(contract, keys, grouped, bool(aggregates)):
            continue
        if not _contract_satisfied(tree, aggregates, contract):
            return _contract_error(contract)
    return None
