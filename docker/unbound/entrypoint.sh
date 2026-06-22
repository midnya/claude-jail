#!/bin/sh
set -eu

log_dir=/var/log/unbound
mkdir -p "$log_dir"
touch "$log_dir/unbound.log"

# Trust the jail's internal subnet to query us (the resolver also sits on
# jail-egress, which must stay default-refuse), then the rendered allow/deny
# zones. Both are server-clause directives the base config includes.
{
    if [ -n "${JAIL_NET_SUBNET:-}" ]; then
        printf 'access-control: %s allow\n' "$JAIL_NET_SUBNET"
    fi
    printf '%s\n' "${JAIL_DNS_ACL:-}"
} > /etc/unbound/jail-dns.conf

# Start unbound as a background daemon (a detached daemon logs to its configured
# logfile, not stderr) and tail that log to stdout, so `logs dns` shows queries
# while they persist in the volume — mirroring squid. A config/bind failure exits
# non-zero before detaching, so `set -e` surfaces it instead of tailing forever.
unbound

exec tail -n0 -F "$log_dir/unbound.log"
