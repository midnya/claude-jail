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
    useradd -m -s /bin/bash -u $USER_UID -g $GROUP_NAME claude

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

COPY entrypoint.sh /usr/local/bin/claude-jail-entrypoint
RUN chmod +x /usr/local/bin/claude-jail-entrypoint

USER claude

WORKDIR /workspace

CMD ["claude-jail-entrypoint"]
