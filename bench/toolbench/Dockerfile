# The tool-bench sandbox: js plus the toolchains the suite's repositories need.
# Build context is bench/toolbench; `just toolbench-image` drops the js wheel
# into wheel/ first. Everything runs as the host user (sandbox.sh passes
# --user), so tool installs live under /opt and caches under the /cache volume.
FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git build-essential pkg-config \
        ripgrep fd-find trash-cli jq xz-utils procps \
    && apt-get clean \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd

# uv (python + tool installs), node LTS 22, go: current patch of each, pulled
# at build time rather than pinned by hand.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
RUN set -eux; f=$(curl -fsSL https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt | grep -o 'node-v22[^ ]*-linux-x64\.tar\.xz' | head -1); \
    curl -fsSL "https://nodejs.org/dist/latest-v22.x/$f" | tar -xJ -C /usr/local --strip-components=1; \
    corepack enable; node --version
RUN set -eux; v=$(curl -fsSL 'https://go.dev/dl/?mode=json' | jq -r '.[0].version'); \
    curl -fsSL "https://go.dev/dl/${v}.linux-amd64.tar.gz" | tar -xz -C /usr/local; /usr/local/go/bin/go version
ENV PATH=/usr/local/go/bin:$PATH \
    UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/uv-python

COPY wheel/*.whl /tmp/wheel/
RUN uv tool install --python 3.12 /tmp/wheel/js-*.whl && js --help >/dev/null && chmod -R a+rX /opt

# world-writable homes for whatever uid sandbox.sh runs as; /cache is a named volume
RUN mkdir -p /cache /tmp/home /workspace && chmod 1777 /cache /tmp/home && chmod 777 /workspace
COPY agents /opt/js-config/js/agents
COPY jsrc /opt/js-config/js/jsrc
COPY image/js-bench-agent /usr/local/bin/js-bench-agent
RUN chmod -R a+rX /opt/js-config && chmod 755 /usr/local/bin/js-bench-agent

ENV HOME=/tmp/home USER=bench \
    UV_CACHE_DIR=/cache/uv npm_config_cache=/cache/npm \
    GOPATH=/cache/go GOMODCACHE=/cache/gomod GOCACHE=/cache/gocache GOFLAGS=-buildvcs=false
WORKDIR /workspace
