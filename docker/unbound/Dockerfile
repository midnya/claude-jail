FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        unbound dnsutils && \
    rm -rf /var/lib/apt/lists/*

COPY unbound.conf /etc/unbound/unbound.conf
COPY entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/entrypoint && touch /etc/unbound/jail-dns.conf

EXPOSE 53/udp 53/tcp

CMD ["entrypoint"]
