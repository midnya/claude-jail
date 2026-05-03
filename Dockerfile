# From https://github.com/VishalJ99/claude-docker/commit/e8461c1dcf4150dc60b1b3180398a3a79511885a
FROM node:20.18.1-slim

# delete default node user if exists
# we will likely need his UID
RUN deluser node || true
RUN delgroup node || true

ENV NODE_ENV=production

RUN apt-get update && apt-get install -y \
    git \
    curl \
    wget \
    python3 \
    python3-pip \
    build-essential \
    sudo \
    gettext-base \
    && rm -rf /var/lib/apt/lists/*

ARG USER_UID=1000
ARG USER_GID=1000
RUN if getent group $USER_GID > /dev/null 2>&1; then \
        GROUP_NAME=$(getent group $USER_GID | cut -d: -f1); \
    else \
        groupadd -g $USER_GID claude && GROUP_NAME=claude; \
    fi && \
    useradd -m -s /bin/bash -u $USER_UID -g $GROUP_NAME claude
    # echo "claude ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

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

USER claude

WORKDIR /workspace

CMD ["claude"]
