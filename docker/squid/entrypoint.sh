#!/bin/sh
set -eu

log_dir=/var/log/squid
mkdir -p "$log_dir"
touch "$log_dir/access.log" "$log_dir/cache.log"
chown -R proxy:proxy "$log_dir"

printf '%s\n' "${JAIL_SQUID_ACL:-}" > /etc/squid/jail-acl.conf

squid -N -f /etc/squid/squid.conf >&2 &

exec tail -n0 -F "$log_dir/access.log"
