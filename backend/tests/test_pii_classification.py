"""Name-based PII classification (schema_scan).

Every case below is drawn from the live Acme catalogs. The old rule was a raw
substring test, which misclassified business dimensions as personal data — and a
misclassified column is not a harmless over-block: `mask_pii_for_llm` replaces its
VALUES with "[PII:<col>]" before the prompt, so the model can no longer name the
brand, the warehouse, or read the metric it is being asked about.

Both directions matter, so both are asserted: business columns must stay readable,
and genuine personal data must stay masked.
"""
import pytest

from app.schema_scan import build_mdl, is_pii_column


# Business dimensions the substring rule wrongly flagged. Each one broke a real
# question: "[PII:warehouse_name]" cannot answer "which warehouse should we expand".
NOT_PII = [
    "pembeli_unik",           # "unique buyers" count — matched "nik" inside "unik"
    "hpp",                    # harga pokok penjualan — matched "hp"
    "brand_name", "product_name", "product_name_pair",
    "warehouse_name", "Material_Name", "material_name",
    "Storage_Site_Name", "Storage_Area_Name",
    "Organization_Name", "business_partner_name", "logistics_service_name",
    "store_address", "sheet_name", "table_name",
    "creator_name", "creator_username", "nama_kreator",
    "latitude_city", "longitude_city",   # city centroid, not a person's location
    "cardinality", "latest_status", "related_id", "translate_key",
]

# Personal data that must remain masked. Regressions here are security regressions.
IS_PII = [
    "customer_name", "cust_name", "complete_name", "First_Name", "Last_Name",
    "recipient_name", "picker_name", "packer_name", "salesman_name", "sales_name",
    "Ship_By_Name", "user_name", "name",
    "buyer_username",         # a buyer identity, unlike creator_username
    "customer_email", "receipt_reminder_email", "customerEmail",
    "Mobile_Phone_Number", "buyer_phone_number", "no_hp", "telepon",
    "Delivery_Address", "street_address", "shipping_street_address",
    "order_lines_customer_complete_address", "alamat_lengkap",
    "l10n_id_nik", "ktp", "passport", "customer_birthday", "dob",
    "gaji", "salary",
    "latitude", "longitude",  # bare, on the order table = buyer delivery coordinates
]

# Categories the auditor asked us to add. They must widen coverage without
# reintroducing substring false positives (see "cardinality" in NOT_PII).
NEWLY_COVERED = [
    "card_number", "cvv", "iban", "rekening", "bank_account", "npwp", "tax_id",
    "password", "passwd", "secret", "api_key", "apikey", "private_key",
    "ip_address",
]


@pytest.mark.parametrize("column", NOT_PII)
def test_business_columns_are_not_personal_data(column):
    assert is_pii_column(column) is False, (
        f"{column!r} is a business dimension; masking it hides the value the "
        "answer is about"
    )


@pytest.mark.parametrize("column", IS_PII + NEWLY_COVERED)
def test_personal_data_stays_classified(column):
    assert is_pii_column(column) is True


def test_generic_token_needs_a_person_qualifier():
    """`name`/`address` are only personal once the rest of the column says so."""
    assert is_pii_column("brand_name") is False
    assert is_pii_column("customer_name") is True
    # An UNRECOGNISED qualifier must stay PII — the default is conservative, so a
    # word the qualifier list has never seen can never silently unmask a column.
    assert is_pii_column("delivery_address") is True
    assert is_pii_column("wibblefrotz_name") is True


def test_hint_matches_whole_tokens_not_substrings():
    assert is_pii_column("unik") is False          # not "nik"
    assert is_pii_column("pembeli_unik") is False
    assert is_pii_column("l10n_id_nik") is True    # the real thing still matches
    assert is_pii_column("hpp") is False           # not "hp"
    assert is_pii_column("no_hp") is True


def test_build_mdl_classifies_the_acme_shape():
    mdl = build_mdl({
        "acme_store_order_mp": ["order_id", "brand", "brand_name", "buyer_username",
                               "latitude", "gmv"],
        "ims_outbound": ["warehouse_name", "Material_Name", "total_cbm"],
    })
    assert mdl["pii_columns"] == ["buyer_username", "latitude"]
    assert sorted(mdl["allowed_tables"]) == ["acme_store_order_mp", "ims_outbound"]
