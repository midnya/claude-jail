#!/usr/bin/env python3
"""Republish the host editor's loopback MCP ports onto the docker bridge gateway.

Runs in the `ide-host` service, which shares the host network namespace
(network_mode: host): the editor's 127.0.0.1 is the host's, and binding the
bridge gateway IP is binding a host interface. For each port `P` advertised in
the watched lockfile dir it listens on `<bind-ip>:P` and forwards to
`127.0.0.1:P` — a plain TCP pipe; the relay has already rewritten payloads.
Ports come and go as editors open and close, so it reconciles a set of per-port
servers on a short poll.

Exposure note: the bind is the docker bridge *gateway*, so ANY container on that
bridge — not just the jailed relay — can reach the republished editor port, with
the editor's authToken as the only gate. That is wider than the relay alone and
is accepted for the opt-in, distrusted bridge; binding the gateway is the
mechanism by which the jailed relay (on a separate network) reaches the host at
all. We still refuse an empty gateway (would bind every host interface incl. the
LAN), so the floor is the bridge, never all interfaces.

Config comes from the environment, like relay.py: JAIL_IDE_GATEWAY is the bridge
gateway IP to bind (the relay reaches the host there) and JAIL_IDE_HOST_DIR the
lockfile dir (default /host-ide).
"""
import asyncio
import os
import signal
import sys
from pathlib import Path

IDE_DIR = Path(os.environ.get("JAIL_IDE_HOST_DIR", "/host-ide"))
POLL_SECONDS = 1.0
_VALID_PORT = range(1, 65536)


def ports_from_lockfiles(ide_dir: Path) -> "set[int]":
    """The ports advertised by *.lock files in `ide_dir` (the port is the filename)."""
    ports: "set[int]" = set()
    try:
        locks = sorted(ide_dir.glob("*.lock"))
    except OSError:
        return ports
    for lock in locks:
        try:
            port = int(lock.stem)
        except ValueError:
            continue
        if port in _VALID_PORT:
            ports.add(port)
    return ports


def reconcile(current: "set[int]", desired: "set[int]"
              ) -> "tuple[set[int], set[int]]":
    """(to_add, to_remove) to move the live server set from `current` to `desired`."""
    return desired - current, current - desired


async def _pipe(reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter) -> None:
    """Copy one direction of a connection until EOF, then close the writer."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def _handle(local_reader: asyncio.StreamReader,
                  local_writer: asyncio.StreamWriter, port: int) -> None:
    """Bridge one accepted connection to the editor on 127.0.0.1:<port>."""
    try:
        remote_reader, remote_writer = await asyncio.open_connection(
            "127.0.0.1", port)
    except OSError:
        local_writer.close()
        return
    pipes = [
        asyncio.ensure_future(_pipe(local_reader, remote_writer)),
        asyncio.ensure_future(_pipe(remote_reader, local_writer)),
    ]
    try:
        # Tear both directions down when either ends: a closed writer does not
        # make a half-open peer's read EOF, so the other pipe would leak.
        await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in (local_writer, remote_writer):
            try:
                w.close()
            except OSError:
                pass
        for p in pipes:
            p.cancel()
        await asyncio.gather(*pipes, return_exceptions=True)


async def _serve(ide_dir: Path, bind_ip: str) -> None:
    """Reconcile per-port forwarding servers to the advertised lockfile ports."""
    servers: "dict[int, asyncio.Server]" = {}
    failed: "set[int]" = set()
    print(f"ide-host: republishing {ide_dir} ports on {bind_ip}", flush=True)
    known: "set[int]" = set()
    try:
        while True:
            desired = ports_from_lockfiles(ide_dir)
            failed &= desired
            if desired != known:
                print(f"ide-host: forwarding ports {sorted(desired)}", flush=True)
                known = desired
            to_add, to_remove = reconcile(set(servers), desired)
            for port in to_add:
                try:
                    servers[port] = await asyncio.start_server(
                        lambda r, w, p=port: _handle(r, w, p), bind_ip, port)
                    failed.discard(port)
                except OSError as e:
                    # Port already bound (e.g. the editor also listens on
                    # 0.0.0.0:P) or the gateway IP isn't local: skip and retry on
                    # a later poll, but say so once so a dead bridge is visible
                    # rather than silently never forwarding.
                    if port not in failed:
                        failed.add(port)
                        print(f"ide-host: cannot bind {bind_ip}:{port} ({e}); "
                              f"that editor port will not be republished",
                              flush=True)
            for port in to_remove:
                server = servers.pop(port)
                server.close()
                try:
                    await server.wait_closed()
                except OSError:
                    pass
            await asyncio.sleep(POLL_SECONDS)
    finally:
        for server in servers.values():
            server.close()


def main() -> None:
    # PID 1 gets no default signal action, and asyncio.run only turns SIGINT into
    # KeyboardInterrupt; map SIGTERM to it too so `docker compose stop`/`down`
    # shuts the loop down cleanly instead of waiting out the grace period.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    bind_ip = os.environ.get("JAIL_IDE_GATEWAY", "")
    if not bind_ip:
        # Refuse to bind every interface; the launcher leaves this empty when it
        # could not determine the gateway, in which case the bridge is simply
        # unavailable this run.
        sys.exit("forward: no JAIL_IDE_GATEWAY; refusing to bind all interfaces")
    try:
        asyncio.run(_serve(IDE_DIR, bind_ip))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
