# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS frontend-build
WORKDIR /build/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY frontend/ ./
ARG VITE_API_BASE
ENV VITE_API_BASE=${VITE_API_BASE}
RUN npm run build


# Codex is a runtime dependency for the optional "Codex (device login)" model
# provider. Keep it in its own stage so npm is not shipped in the final image,
# and pin the default version so a staging rebuild cannot silently pick up a
# breaking CLI release. The pipeline may override this build argument after a
# compatibility test.
FROM node:22-bookworm-slim AS codex-cli
ARG CODEX_CLI_VERSION=0.146.1
RUN test -n "${CODEX_CLI_VERSION}" \
    && npm install --global --ignore-scripts "@openai/codex@${CODEX_CLI_VERSION}" \
    && codex --version


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    FRONTEND_DIST_DIR=/app/frontend/dist \
    DOCS_FILES_DIR=/var/lib/poros/documents \
    CODEX_HOME=/var/lib/poros/codex \
    # Pinned rather than left to default at $HOME/.cache/huggingface, because
    # deploy/production/deployment.yaml mounts a writable volume at this exact path for
    # the sentence-transformers weights. With readOnlyRootFilesystem an implicit default
    # that drifts (a different USER, a base-image change) would not fall back to a
    # writable location — the first model load would fail instead.
    HF_HOME=/home/poros/.cache/huggingface \
    # Defence in depth for the BigQuery connector: google-auth will only run an
    # executable-sourced credential config when this is "1". The connector already
    # refuses external_account configs outright, so this is the second lock — it keeps a
    # future WIF rollout or a debugging session from silently re-opening the path.
    GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES=0 \
    APPS_GATEWAY_TRUST_HEADER=1 \
    APPS_GATEWAY_NETWORK_ISOLATED=1 \
    APPS_GATEWAY_AUTO_PROVISION=1 \
    APPS_GATEWAY_DEFAULT_ROLE=tenant_manager

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
COPY backend/requirements.txt /tmp/requirements.txt
# The PyPI Linux torch package now resolves a full CUDA runtime even on CPU-only
# hosts. Install the official CPU wheel first; sentence-transformers reuses it.
# Model weights and numerical behavior stay the same, without several GB of
# unused NVIDIA libraries in the production image.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install --no-cache-dir -r /tmp/requirements.txt

# The globally installed launcher is a relative symlink into the package tree,
# so copy both it and its Node runtime. OAuth credentials are deliberately not
# copied: staging creates them later in the CODEX_HOME persistent volume.
COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-cli /usr/local/bin/codex /usr/local/bin/codex
COPY --from=codex-cli /usr/local/lib/node_modules/@openai/codex /usr/local/lib/node_modules/@openai/codex
RUN node --version && codex --version

COPY backend/ /app/backend/
COPY --from=frontend-build /build/frontend/dist/ /app/frontend/dist/

# Bake build identity so /dangerously-skip-login/health can answer "which build is
# this?" without a login. built= always changes per image build; sha= is filled when
# the pipeline passes --build-arg GIT_SHA=$(git rev-parse --short HEAD) and stays
# "unknown" otherwise (.git is dockerignored, so it cannot be read at build time).
ARG GIT_SHA=unknown
RUN printf 'sha=%s\nbuilt=%s\n' "$GIT_SHA" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /app/backend/BUILD_INFO \
    && useradd --create-home --uid 10001 poros \
    && mkdir -p /var/lib/poros/documents /var/lib/poros/codex \
    && chown -R poros:poros /var/lib/poros /home/poros

USER poros

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/dangerously-skip-login/health', timeout=3).read()"

# --http h11: the Apps Gateway re-frames authenticated write requests (body
# inspection) in ways the stricter httptools/llhttp parser rejects outright
# ("Invalid HTTP request received." -> 400 on every console POST/PATCH/DELETE).
# h11 applies the RFC 7230 recovery rules instead (e.g. Transfer-Encoding wins
# over Content-Length, duplicate identical Content-Length collapsed). Lenient
# framing is safe HERE ONLY because the backend is network-isolated behind the
# gateway (APPS_GATEWAY_NETWORK_ISOLATED) — do not copy this to an internet-
# facing deployment, where strict parsing guards against request smuggling.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --http h11"]
