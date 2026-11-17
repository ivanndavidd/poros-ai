"""BigQuery auth without a service-account key: WIF credential-config + impersonation.

Run:  cd backend && python -m pytest tests/test_bigquery_auth.py -q

All offline — only credential OBJECT construction is exercised (google-auth
validates config shape at construction; tokens are fetched lazily on first use,
which never happens here). No GCP access needed.
"""
import json, os, sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

pytest.importorskip("google.auth")

from app.connectors.bigquery_conn import BigQueryConnector  # noqa: E402

# keyless WIF credential-config (type external_account) — CONFIG, not a secret key
WIF_CONFIG = {
    "type": "external_account",
    "audience": ("//iam.googleapis.com/projects/123456/locations/global/"
                 "workloadIdentityPools/tenantos-pool/providers/entra-oidc"),
    "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
    "token_url": "https://sts.googleapis.com/v1/token",
    "credential_source": {"file": "/var/run/oidc/token.jwt"},
}


def _mk(extra):
    return BigQueryConnector({"project": "proj-x", "dataset": "ds", **extra})


def test_blank_credentials_falls_back_to_adc():
    assert _mk({})._credentials() is None                 # None -> bigquery.Client uses ADC


# `conn` is an unvalidated dict from any authenticated console user, so an
# external_account config is request-controlled input to google-auth providers that
# fetch a URL (SSRF), read a local file, or run an executable. Keyless auth is supported
# through host ADC + impersonation instead, which stores no secret at all.
def test_wif_credential_config_dict_is_refused():
    with pytest.raises(ValueError, match="external_account"):
        _mk({"credentials": WIF_CONFIG})._credentials()


def test_wif_credential_config_string_is_refused():
    with pytest.raises(ValueError, match="external_account"):
        _mk({"credentials": json.dumps(WIF_CONFIG)})._credentials()


def test_credentials_string_is_never_read_as_a_file_path(tmp_path):
    """A path was opened and its contents used as credentials — an arbitrary file read."""
    key = tmp_path / "other-tenant-key.json"
    key.write_text(json.dumps({"type": "service_account"}))
    with pytest.raises(ValueError, match="inline JSON"):
        _mk({"credentials": str(key)})._credentials()
    # ...and a path that does NOT exist must not become a silent no-op either
    with pytest.raises(ValueError, match="inline JSON"):
        _mk({"credentials": "/etc/passwd"})._credentials()


def test_executable_sourced_credential_config_is_refused():
    cfg = {**WIF_CONFIG, "credential_source": {"executable": {"command": "/bin/sh -c id"}}}
    with pytest.raises(ValueError, match="external_account"):
        _mk({"credentials": cfg})._credentials()


def test_url_sourced_credential_config_is_refused():
    cfg = {**WIF_CONFIG, "credential_source": {
        "url": "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"}}
    with pytest.raises(ValueError, match="external_account"):
        _mk({"credentials": cfg})._credentials()


def test_service_account_key_still_works():
    # throwaway RSA key so from_service_account_info can parse — never used to sign
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    sa = {"type": "service_account", "project_id": "proj-x", "private_key_id": "kid",
          "private_key": pem, "client_email": "sa@proj-x.iam.gserviceaccount.com",
          "client_id": "1", "token_uri": "https://oauth2.googleapis.com/token"}
    from google.oauth2 import service_account
    got = _mk({"credentials": sa})._credentials()
    assert isinstance(got, service_account.Credentials)


def test_impersonation_still_refuses_an_explicit_wif_base():
    # Impersonation must not become a way back into the external_account constructors.
    with pytest.raises(ValueError, match="external_account"):
        _mk({"credentials": WIF_CONFIG,
             "impersonate_service_account": "sa-acme@proj-x.iam.gserviceaccount.com"})._credentials()


def test_impersonation_over_adc_env(monkeypatch, tmp_path):
    cfg = tmp_path / "wif-credential-config.json"
    cfg.write_text(json.dumps(WIF_CONFIG))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(cfg))   # keyless ADC via env
    # without an explicit project, google.auth.default() would resolve it VIA the
    # credentials (a live STS call); the env keeps this test offline
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-x")
    from google.auth import impersonated_credentials
    got = _mk({"impersonate_service_account": "sa-x@proj-x.iam.gserviceaccount.com"})._credentials()
    assert isinstance(got, impersonated_credentials.Credentials)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
