FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 && \
    rm -rf /var/lib/apt/lists/*

# -u equivalent, so both roles stream their logs to `logs ide-relay`/`ide-host`.
ENV PYTHONUNBUFFERED=1

COPY src/ide/relay.py /usr/local/bin/relay
COPY src/ide/forward.py /usr/local/bin/forward
RUN chmod +x /usr/local/bin/relay /usr/local/bin/forward
