# syntax=docker/dockerfile:1.7

# Match the native Mac toolchain; update these pins with .python-version/.node-version.
ARG PYTHON_IMAGE=python:3.14.7-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56
ARG NODE_IMAGE=node:26.9.0-bookworm-slim@sha256:582460f614631b59b824ac6020533b9bf339c7fdf3a6d7db31abb6b4065f0212

FROM ${NODE_IMAGE} AS dashboard-build

WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web/ ./
RUN npm run build


FROM ${PYTHON_IMAGE} AS python-deps

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=/usr/local/bin/python \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project


FROM debian:bookworm-slim AS ibapi-source

ARG IBKR_API_URL=https://interactivebrokers.github.io/downloads/twsapi_macunix.1050.01.zip
ARG IBKR_API_SHA256=aa065722ca732a41aab202c7bb72932e179b86e7ec51cefa063eb1983fe9f597
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl --fail --show-error --silent --location "${IBKR_API_URL}" --output /tmp/twsapi.zip \
    && echo "${IBKR_API_SHA256}  /tmp/twsapi.zip" | sha256sum --check --strict \
    && unzip -q /tmp/twsapi.zip -d /source \
    && test -f /source/IBJts/source/pythonclient/ibapi/client.py \
    && test -f /source/IBJts/source/pythonclient/ibapi/order_cancel.py \
    && rm -f /tmp/twsapi.zip


FROM ${PYTHON_IMAGE} AS runtime

ARG BUILD_REVISION=unknown
LABEL org.opencontainers.image.title="ZQ Polymarket Arbitrage Monitor" \
      org.opencontainers.image.description="Fail-closed ZQ and Polymarket monitoring engine" \
      org.opencontainers.image.source="https://github.com/yzy0806/Poly-ZQ-trading" \
      org.opencontainers.image.revision="${BUILD_REVISION}"

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IBKR_PYTHON_API_PATH=/opt/ibapi

RUN groupadd --gid 10001 zqarb \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/zqarb zqarb \
    && install -d -o 10001 -g 10001 -m 0750 /var/lib/zq-arb

WORKDIR /app
COPY --from=python-deps /app/.venv /app/.venv
COPY --from=ibapi-source /source/IBJts/source/pythonclient /opt/ibapi
COPY src/ /app/src/
COPY --from=dashboard-build /build/web/dist /app/web/dist

USER 10001:10001
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3).read()"]

ENTRYPOINT ["python", "-m", "zq_arb.main"]
