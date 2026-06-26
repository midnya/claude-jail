# From https://github.com/VishalJ99/claude-docker/commit/e8461c1dcf4150dc60b1b3180398a3a79511885a
FROM node:20.18.1-slim

# delete default node user if exists
# we will likely need his UID
RUN deluser node || true
RUN delgroup node || true

ENV NODE_ENV=production

RUN apt-get update && apt-get install -y \
    bubblewrap \
    build-essential \
    curl \
    gettext-base \
    git \
    ncurses-term \
    python3 \
    python3-pip \
    socat \
    sudo \
    wget \
    && rm -rf /var/lib/apt/lists/*

RUN (type -p wget >/dev/null || (sudo apt update && sudo apt install wget -y)) \
    && sudo mkdir -p -m 755 /etc/apt/keyrings \
    && out=$(mktemp) && wget -nv -O$out https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    && cat $out | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && sudo mkdir -p -m 755 /etc/apt/sources.list.d \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && sudo apt update \
    && sudo apt install gh -y \
    && rm -rf /var/lib/apt/lists/*

ARG USER_UID=1000
ARG USER_GID=1000
RUN if getent group $USER_GID > /dev/null 2>&1; then \
        GROUP_NAME=$(getent group $USER_GID | cut -d: -f1); \
    else \
        groupadd -g $USER_GID claude && GROUP_NAME=claude; \
    fi && \
    useradd -o -m -s /bin/bash -u $USER_UID -g $GROUP_NAME claude

WORKDIR /app

ARG CLAUDE_CODE_VERSION=""
RUN if [ -n "$CLAUDE_CODE_VERSION" ]; then \
        echo "Installing Claude Code version: $CLAUDE_CODE_VERSION" && \
        npm install -g @anthropic-ai/claude-code@$CLAUDE_CODE_VERSION; \
    else \
        echo "Installing latest Claude Code" && \
        npm install -g @anthropic-ai/claude-code; \
    fi

RUN mkdir -p /app/.claude /home/claude/.claude
RUN chown -R claude /app /home/claude

ARG EXTRA_APT_PACKAGES=""
RUN if [ -n "$EXTRA_APT_PACKAGES" ]; then \
        apt-get update && \
        apt-get install -y $EXTRA_APT_PACKAGES && \
        rm -rf /var/lib/apt/lists/*; \
    fi

# uv 0.11.24-python3.13-trixie-slim
COPY --from=ghcr.io/astral-sh/uv@sha256:dbabc35ae87836369ca9a2052a4f0bd3c76135b1cf32c33d8ec149927135019d /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/

ENV PATH="/opt/jail-venv/bin:$PATH"

ENV PYTHONNOUSERSITE=1
RUN uv venv --seed --system-site-packages --python /usr/bin/python3 /opt/jail-venv
# Numbered so login shells source venv then node, leaving node's bin first on PATH
RUN printf 'export PATH="/opt/jail-venv/bin:$PATH"\n' > /etc/profile.d/10-jail-venv.sh

ARG EXTRA_PIP_PACKAGES=""
RUN if [ -n "$EXTRA_PIP_PACKAGES" ]; then \
        set -f; \
        printf '%s\n' $EXTRA_PIP_PACKAGES > /tmp/requirements.txt && \
        uv pip install --python /opt/jail-venv/bin/python --no-cache -r /tmp/requirements.txt && \
        rm /tmp/requirements.txt; \
    fi

# Extra npm packages, installed at build time with the bundled yarn into a
# root-owned (so read-only to the agent), world-readable tree, prepended to PATH.
# NODE_PATH makes them require()-able too (CommonJS only; the ESM loader ignores it).
ENV NODE_PATH="/opt/jail-node/node_modules"
ENV PATH="/opt/jail-node/node_modules/.bin:$PATH"
RUN printf 'export PATH="/opt/jail-node/node_modules/.bin:$PATH"\n' > /etc/profile.d/20-jail-node.sh

ARG EXTRA_NPM_PACKAGES=""
RUN if [ -n "$EXTRA_NPM_PACKAGES" ]; then \
        set -f; \
        mkdir -p /opt/jail-node && \
        printf '{"name":"jail-node","private":true}\n' > /opt/jail-node/package.json && \
        cd /opt/jail-node && \
        yarn add --non-interactive --no-progress --cache-folder /tmp/yarn-cache $EXTRA_NPM_PACKAGES && \
        rm -rf /tmp/yarn-cache; \
    fi

COPY entrypoint.sh /usr/local/bin/claude-jail-entrypoint
RUN chmod +x /usr/local/bin/claude-jail-entrypoint

USER claude

WORKDIR /workspace

CMD ["claude-jail-entrypoint"]
