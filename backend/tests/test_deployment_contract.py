"""Repository-owned production deployment contract."""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main, store


ROOT = Path(__file__).resolve().parents[2]


def test_database_url_accepts_platform_alias(monkeypatch):
    monkeypatch.delenv("ADMIN_DB_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://platform/poros")
    assert store._database_url() == "postgresql://platform/poros"

    monkeypatch.setenv("ADMIN_DB_URL", "postgresql://poros/explicit")
    assert store._database_url() == "postgresql://poros/explicit"


def test_database_url_has_no_hardcoded_password_fallback(monkeypatch):
    monkeypatch.delenv("ADMIN_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="ADMIN_DB_URL or DATABASE_URL"):
        store._database_url()


def test_compiled_frontend_can_be_served_by_fastapi(tmp_path):
    (tmp_path / "index.html").write_text("<h1>Poros production</h1>", encoding="utf-8")
    application = FastAPI()
    assert main._mount_frontend(application, tmp_path) is True
    response = TestClient(application).get("/")
    assert response.status_code == 200
    assert "Poros production" in response.text


def test_missing_frontend_is_a_safe_local_dev_noop(tmp_path):
    assert main._mount_frontend(FastAPI(), tmp_path) is False


def test_docker_contract_uses_python_runtime_and_gateway_health():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:" in dockerfile
    assert "npm ci" in dockerfile and "npm run build" in dockerfile
    assert "uvicorn app.main:app" in dockerfile
    # h11 parser: the Apps Gateway re-frames authenticated writes in ways
    # httptools rejects (400 "Invalid HTTP request received." on every console
    # mutation). Only safe because the backend is gateway-isolated.
    assert "--http h11" in dockerfile
    assert "EXPOSE 8000" in dockerfile
    assert "/dangerously-skip-login/health" in dockerfile
    assert "APPS_GATEWAY_TRUST_HEADER=1" in dockerfile
    assert "APPS_GATEWAY_NETWORK_ISOLATED=1" in dockerfile


def test_docker_contract_installs_pinned_codex_cli_without_credentials():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG CODEX_CLI_VERSION=" in dockerfile
    assert (
        'npm install --global --ignore-scripts '
        '"@openai/codex@${CODEX_CLI_VERSION}"'
    ) in dockerfile
    assert "RUN node --version && codex --version" in dockerfile
    assert "CODEX_HOME=/var/lib/poros/codex" in dockerfile
    assert "auth.json" not in "\n".join(
        line for line in dockerfile.splitlines()
        if line.lstrip().startswith(("COPY ", "ADD "))
    )


def test_production_baseline_preserves_rag_quality():
    config = (ROOT / "deploy" / "production" / "configmap.yaml").read_text(
        encoding="utf-8"
    )
    assert 'ST_EMBED_DISABLE: "0"' in config
    assert 'RAG_RERANK: "1"' in config
    assert 'VECTOR_BACKEND: "sqlite"' in config


def test_production_console_baseline_uses_apps_gateway_identity():
    config = (ROOT / "deploy" / "production" / "configmap.yaml").read_text(
        encoding="utf-8"
    )
    assert 'APPS_GATEWAY_TRUST_HEADER: "1"' in config
    assert 'APPS_GATEWAY_NETWORK_ISOLATED: "1"' in config


def test_production_container_limits_ephemeral_storage():
    deployment = (ROOT / "deploy" / "production" / "deployment.yaml").read_text(
        encoding="utf-8"
    )
    assert deployment.count("ephemeral-storage:") == 2


def test_production_mounts_persistent_codex_oauth_state():
    deployment = (ROOT / "deploy" / "production" / "deployment.yaml").read_text(
        encoding="utf-8"
    )
    config = (ROOT / "deploy" / "production" / "configmap.yaml").read_text(
        encoding="utf-8"
    )
    pvcs = (ROOT / "deploy" / "production" / "pvc.yaml").read_text(
        encoding="utf-8"
    )
    assert "replicas: 1" in deployment
    assert "mountPath: /var/lib/poros/codex" in deployment
    assert "claimName: poros-codex-state" in deployment
    assert 'CODEX_HOME: "/var/lib/poros/codex"' in config
    assert "name: poros-codex-state" in pvcs
    assert "ReadWriteOnce" in pvcs
