# One image that builds any connector, selected at build time.
#
# Eighteen near-identical Dockerfiles would be eighteen places for a base
# image, a Python version or a security patch to drift apart, and the drift
# would be invisible until one of them was the one that mattered. The
# connector is a build argument instead.
#
#   docker build --build-arg CONNECTOR=web_search \
#     --build-arg SERVER_NAME=io.github.magurelabs/web-search -t magone-mcp-web-search .
#   docker run --rm -p 8000:8000 magone-mcp-web-search
#
# The connector's own extra is installed, so a connector that needs a
# third-party library gets it and one that does not stays small. That mapping
# lives in pyproject.toml and is enforced by scripts/dependency_gate.py, which
# fails if a connector imports something no extra declares - the defect that
# left 13 of 23 connectors unable to start from a clean install.

# Pinned by digest: a tag moves, and "what base is this image on" should be a
# fact in the repository rather than a question for the registry.
FROM python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9 AS base

ARG CONNECTOR
# The registry name from the connector's server.json, e.g.
# io.github.magurelabs/google-calendar. The MCP Registry proves ownership of an
# OCI image by reading this label and comparing it to the manifest's `name`.
# It was derived from the directory name (`google_calendar`), which differs
# for every connector with an underscore, so the check would have failed for
# most of the catalogue. A LABEL cannot read a file, so the value is passed in
# and verified against server.json further down.
ARG SERVER_NAME=""
# Fail at build time rather than producing an image that cannot start. A
# default here would silently build the wrong connector.
RUN test -n "$CONNECTOR" || (echo "CONNECTOR build arg is required" && exit 1)

LABEL io.modelcontextprotocol.server.name="${SERVER_NAME}"
LABEL org.opencontainers.image.source="https://github.com/magurelabs/magoneai-mcp"
LABEL org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency metadata first, so a change to connector source does not
# invalidate the dependency layer.
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# The core, then the connector's own extra when pyproject declares one. The
# extra is looked up rather than attempted: `pip install . && pip install
# ".[x]" || true` also swallowed a failed install of the core itself and
# shipped an image that could not start.
RUN pip install --no-cache-dir . \
    && if python -c "import sys, tomllib; sys.exit(0 if sys.argv[1] in tomllib.load(open('pyproject.toml', 'rb'))['project'].get('optional-dependencies', {}) else 1)" "$CONNECTOR"; \
       then pip install --no-cache-dir ".[${CONNECTOR}]"; fi

# Only the selected connector is copied, plus the parent package's empty
# `__init__.py` (a regular package so pytest cannot put `connectors/` itself
# on sys.path and let `connectors/trino` shadow the Trino client).
COPY connectors/__init__.py ./connectors/__init__.py
COPY connectors/${CONNECTOR}/ ./connectors/${CONNECTOR}/

# A publishable connector's label must be its manifest's name, exactly.
RUN python - "$CONNECTOR" "$SERVER_NAME" <<'PY'
import json, pathlib, sys
connector, label = sys.argv[1], sys.argv[2]
manifest = pathlib.Path("connectors", connector, "server.json")
if manifest.exists():
    name = json.loads(manifest.read_text())["name"]
    if label != name:
        sys.exit(f"SERVER_NAME must be {name!r} (from {manifest}); got {label!r}")
PY

# Never root, and the code is not the process's to change: /app stays owned
# by root and readable. A connector parses somebody else's API responses for a
# living, which is exactly the process that should not be able to rewrite
# itself. Scratch space is its home directory and /tmp.
RUN useradd --create-home --uid 10001 connector
USER connector

ENV CONNECTOR=${CONNECTOR} \
    MCP_MODE=platform \
    PORT=8000

EXPOSE 8000

# Liveness from inside the container, on loopback, against /healthz.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=4)" || exit 1

# `local` mode will not bind beyond loopback without MCP_ALLOW_INSECURE_BIND=1,
# so the default MCP_MODE above is what makes this image useful in a cluster
# and refuses to be accidentally unauthenticated.
#
# Set MCP_ALLOWED_HOSTS to the name callers reach this container by (the
# Compose service or Kubernetes Service name). Only loopback is accepted
# otherwise, and every other Host is answered 421.
CMD ["sh", "-c", "python -m connectors.${CONNECTOR}"]
