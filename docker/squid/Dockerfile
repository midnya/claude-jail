FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        squid && \
    rm -rf /var/lib/apt/lists/*

COPY squid.conf /etc/squid/squid.conf
COPY entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/entrypoint && touch /etc/squid/jail-acl.conf

EXPOSE 3128

CMD ["entrypoint"]
