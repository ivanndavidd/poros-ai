"""Schema/catalog scan -> Poros MDL.

The legacy scanner accepted only ``{table: [column, ...]}``, which remains supported
for compatibility.  Rich connector discovery can additionally populate technical
metadata used by the SQL agent: types, nullability, descriptions, partitioning,
clustering, declared keys, and relationships.  Business semantics (grain and metrics)
are deliberately not guessed here.
"""

import re

# Name-based PII detection. A hint matches WHOLE NAME TOKENS, never a raw substring:
# plain `hint in column.lower()` classified `pembeli_unik` ("unique buyers", a count
# metric) as PII because it contains "nik", and `hpp` (harga pokok penjualan) because
# it contains "hp". A masked metric reaches the model as "[PII:pembeli_unik]", so a
# misclassification silently costs answer quality on every question that touches it.
# Multi-token hints ("no_hp", "bank_account") match a contiguous run of tokens.
PII_HINTS = (
    # contact / identity
    "email", "phone", "telepon", "no_hp", "nohp", "hp", "name", "nama", "address",
    "alamat", "nik", "ktp", "passport", "dob", "birth", "birthday", "gender",
    # glued forms: token matching cannot see "name" inside "username", and the old
    # substring rule caught these by accident — keep them explicitly.
    "username", "firstname", "lastname", "fullname", "phonenumber", "emailaddress",
    # financial
    "gaji", "salary", "card", "cardno", "card_number", "cvv", "iban", "rekening",
    "bank_account", "account_no", "npwp", "tax_id",
    # credentials / secrets
    "password", "passwd", "secret", "token", "api_key", "apikey", "private_key",
    # location / network
    "ip_address", "latitude", "longitude", "lat", "lon",
)

# Tokens that mark a column as naming a THING, PLACE, or ORGANISATION rather than a
# person. Only consulted for the generic hints below — `brand_name`, `warehouse_name`
# and `Material_Name` are business dimensions, while `customer_name` and
# `Delivery_Address` are not. Anything NOT listed here stays PII, so the default
# remains conservative and an unknown qualifier never silently unmasks a column.
_NON_PERSON_QUALIFIERS = frozenset({
    "brand", "product", "item", "sku", "material", "commodity",
    "store", "warehouse", "storage", "site", "area", "outlet", "shop", "facility",
    "organization", "organisation", "company", "business", "partner", "vendor",
    "supplier", "logistics", "service", "merchant", "marketplace", "platform",
    "channel", "campaign", "category", "segment", "department", "division",
    "table", "sheet", "file", "column", "field", "schema", "database", "dataset",
    "report", "template", "event", "job", "task", "batch", "process", "status",
    "creator", "kreator",
    # administrative geography: a city centroid is not a person's location
    "city", "kota", "province", "provinsi", "district", "kecamatan", "kelurahan",
    "region", "country", "postal", "zone",
})

# Hints too generic to stand on their own: they name a person only when the rest of
# the column does. A bare column (`name`, `latitude`) still counts, so the default
# stays conservative; only a recognised non-person qualifier clears the flag.
_GENERIC_HINTS = frozenset({
    "name", "nama", "username", "address", "alamat",
    "latitude", "longitude", "lat", "lon",
})

_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def _tokens(value: str) -> list[str]:
    """Lower-cased name tokens: `Storage_Site_Name` -> [storage, site, name]."""
    return [part.lower() for part in _TOKEN_SPLIT.split(str(value)) if part]


def _matches(hint_tokens: list[str], tokens: list[str]) -> bool:
    """True when the hint's tokens appear as a contiguous run inside `tokens`."""
    span = len(hint_tokens)
    return any(tokens[i:i + span] == hint_tokens for i in range(len(tokens) - span + 1))


def is_pii_column(column: str) -> bool:
    """Whether a column name alone suggests personal data.

    Name matching is a FLOOR, not a ceiling — an administrator can always add columns
    the heuristic cannot see through the MDL patch endpoint.
    """
    tokens = _tokens(column)
    if not tokens:
        return False
    for hint in PII_HINTS:
        hint_tokens = _tokens(hint)
        if not hint_tokens or not _matches(hint_tokens, tokens):
            continue
        if hint in _GENERIC_HINTS and len(tokens) > 1:
            # `customer_name` yes, `brand_name` no. An unrecognised qualifier
            # ("delivery_address") stays PII.
            if any(token in _NON_PERSON_QUALIFIERS for token in tokens):
                continue
        return True
    return False


def _pii_columns(tables: dict) -> list[str]:
    pii = set()
    for columns in tables.values():
        for column in columns or []:
            if is_pii_column(column):
                pii.add(str(column))
    return sorted(pii)


def _base_mdl(tables: dict) -> dict:
    return {
        "tables": tables,
        "allowed_tables": sorted(tables),
        "pii_columns": _pii_columns(tables),
        "denied_functions": ["version", "current_user"],
        "allowed_functions": [],
    }


def build_mdl(introspected: dict) -> dict:
    """Build the backwards-compatible, names-only MDL."""
    tables = {
        str(table): [str(column) for column in (columns or [])]
        for table, columns in (introspected or {}).items()
    }
    return _base_mdl(tables)


def _catalog_relations(catalog: dict) -> list[dict]:
    return [
        item for item in (catalog or {}).get("relations") or []
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]


def _relation_columns(relation: dict) -> list[dict]:
    return [
        item for item in relation.get("columns") or []
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]


def _table_metadata(relation: dict, description: str) -> dict:
    return {
        "namespace": str(relation.get("namespace") or ""),
        "kind": str(relation.get("kind") or "table"),
        "description": description,
        "estimated_rows": relation.get("estimated_rows"),
        "estimated_bytes": relation.get("estimated_bytes"),
        "partitioning": relation.get("partitioning"),
        "clustering": list(relation.get("clustering") or []),
        "primary_key": list(relation.get("primary_key") or []),
    }


def _add_columns(table: str, columns: list[dict], metadata: dict,
                 descriptions: dict) -> None:
    for index, column in enumerate(columns, 1):
        name = str(column["name"])
        key = f"{table}.{name}"
        description = str(column.get("description") or "").strip()[:500]
        if description:
            descriptions[key] = description
        metadata[key] = {
            "data_type": str(column.get("data_type") or ""),
            "nullable": column.get("nullable"),
            "ordinal": int(column.get("ordinal") or index),
            "description": description,
        }


def _declared_relationships(table: str, relation: dict) -> list[dict]:
    result = []
    for foreign in relation.get("foreign_keys") or []:
        if not isinstance(foreign, dict):
            continue
        from_column = str(foreign.get("column") or "").strip()
        to_table = str(foreign.get("references_table") or "").strip()
        to_column = str(foreign.get("references_column") or "").strip()
        if not (from_column and to_table and to_column):
            continue
        result.append({
            "from_table": table,
            "from_column": from_column,
            "to_table": to_table,
            "to_column": to_column,
            "to_namespace": str(foreign.get("references_namespace") or ""),
            "cardinality": "many-to-one",
            "source": "declared_constraint",
        })
    return result


def build_mdl_from_catalog(catalog: dict) -> dict:
    """Build an MDL from normalized ``Connector.discover()`` output.

    ``tables`` intentionally keeps the existing list-of-column-names contract so all
    governance and UI consumers remain compatible. Rich fields are additive.
    """
    tables = {}
    table_metadata = {}
    column_metadata = {}
    descriptions = {}
    relationships = []

    for relation in _catalog_relations(catalog):
        table = str(relation["name"])
        # The existing MDL/governance contract uses unqualified table names. A connector
        # that reports duplicate names across namespaces cannot be represented safely;
        # preserve the first authorized relation instead of silently merging schemas.
        if table in tables:
            continue
        columns = _relation_columns(relation)
        tables[table] = [str(item["name"]) for item in columns]

        table_description = str(relation.get("description") or "").strip()[:1000]
        if table_description:
            descriptions[table] = table_description
        table_metadata[table] = _table_metadata(relation, table_description)
        _add_columns(table, columns, column_metadata, descriptions)
        relationships.extend(_declared_relationships(table, relation))

    mdl = _base_mdl(tables)
    if descriptions:
        mdl["descriptions"] = descriptions
    if table_metadata:
        mdl["table_metadata"] = table_metadata
    if column_metadata:
        mdl["column_metadata"] = column_metadata
    if relationships:
        mdl["relationships"] = relationships
    return mdl


def scan_connector(connector) -> dict:
    """Prefer rich discovery, falling back to legacy introspection when unavailable."""
    try:
        catalog = connector.discover()
        if isinstance(catalog, dict) and catalog.get("relations"):
            return build_mdl_from_catalog(catalog)
    except Exception:
        # Some database identities can read INFORMATION_SCHEMA.COLUMNS but not richer
        # catalog/statistics views. Connecting must remain backwards-compatible.
        pass
    return build_mdl(connector.introspect())
