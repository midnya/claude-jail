#!/usr/bin/env python3
"""The `ide-relay` side-container: mirror editor lockfiles, forward to the editor.

Two jobs, reconciled on a short poll as editors open and close:
  1. Mirror each `<port>.lock` from /host-ide into /jail-ide (the dir the in-jail
     CLI scans), rewriting host paths in `workspaceFolders` to their `/workspace`
     form so the workspace matches. The port is the filename; `authToken` is kept.
  2. For each advertised port P, accept on :P and forward to JAIL_IDE_TARGET:P.
     The HTTP/WebSocket handshake passes through untouched (auth and subprotocol
     negotiate end to end with the editor); after it, frames are parsed and the
     `/workspace` prefix on paths + `file://` URIs in their JSON payloads is
     rewritten both ways.
"""
import asyncio
import ipaddress
import json
import os
import signal
import socket
import sys
from pathlib import Path

# The container mount prefix (jail_config.container_path's `/workspace<host>`),
# passed in by the launcher so the relay's path rewriting stays the inverse of
# the actual bind layout rather than a coincidentally-matching literal.
WORKSPACE = os.environ.get("JAIL_IDE_WORKSPACE", "/workspace")
FILE_URI = "file://"

HOST_IDE_DIR = Path(os.environ.get("JAIL_IDE_HOST_DIR", "/host-ide"))
JAIL_IDE_DIR = Path(os.environ.get("JAIL_IDE_JAIL_DIR", "/jail-ide"))
TARGET = os.environ.get("JAIL_IDE_TARGET", "host.docker.internal")
POLL_SECONDS = 1.0
_VALID_PORT = range(1, 65536)
# Cap a single frame's declared length so a buggy/hostile peer can't make us
# buffer an arbitrary payload; and raise the stream buffer (on BOTH the listen
# side and the upstream connection, see _serve/_handle) so a large handshake
# header doesn't trip readuntil's default 64 KiB limit.
MAX_FRAME = 64 * 1024 * 1024
_STREAM_LIMIT = 1024 * 1024
# When one direction of a proxied connection ends (usually a close frame), give
# the other a brief, bounded window to flush data already in flight before we
# tear it down. A closed writer doesn't EOF a half-open peer, so we can't wait
# unbounded; a responsive peer finishes its own close/EOF well within this.
_CLOSE_DRAIN_SECONDS = 1.0


def _strip_workspace(s: str) -> str:
    if s.startswith(FILE_URI):
        path = s[len(FILE_URI):]
        return FILE_URI + _strip_workspace(path) if path.startswith("/") else s
    if s == WORKSPACE:
        return "/"
    if s.startswith(WORKSPACE + "/"):
        return s[len(WORKSPACE):]
    return s


def _add_workspace(s: str) -> str:
    if s.startswith(FILE_URI):
        path = s[len(FILE_URI):]
        return FILE_URI + _add_workspace(path) if path.startswith("/") else s
    if s == "/":  # the host root is the container's /workspace (inverse of strip)
        return WORKSPACE
    if s != WORKSPACE and not s.startswith(WORKSPACE + "/") and s.startswith("/"):
        return WORKSPACE + s
    return s


def _prefix_fn(to_host: bool):
    """The per-direction leaf rewriter: strip /workspace for the host, add it back.

    The single owner of the direction->function choice, so rewrite() and
    rewrite_message() can't pick different mappings for the same `to_host`.
    """
    return _strip_workspace if to_host else _add_workspace


def _rewrite_tree(obj: "object", fn) -> "tuple[object, bool]":
    """Map fn over string leaves; return (new_obj, changed).

    `changed` is True iff some leaf actually moved, so rewrite_message can return
    the original bytes (skipping json.dumps + encode) when nothing did.
    """
    if isinstance(obj, str):
        new = fn(obj)
        return new, new != obj
    if isinstance(obj, list):
        changed = False
        out = []
        for x in obj:
            nx, c = _rewrite_tree(x, fn)
            out.append(nx)
            changed = changed or c
        return out, changed
    if isinstance(obj, dict):
        changed = False
        out = {}
        for k, v in obj.items():
            nv, c = _rewrite_tree(v, fn)
            out[k] = nv
            changed = changed or c
        return out, changed
    return obj, False


def rewrite(obj: "object", to_host: bool) -> "object":
    """Deep-copy obj, mapping the /workspace prefix on string leaves (host<->container).

    Known limitation: the JSON-RPC schema is not known here, so this maps EVERY
    string leaf by prefix, not just dedicated path fields. A non-path value that
    legitimately begins with the prefix (host->container) or with `/` (container
    ->host) is rewritten too — e.g. a selection or diagnostic string whose first
    character is `/`. Field-aware rewriting would need the protocol schema; the
    opt-in, distrusted bridge accepts the over-reach.
    """
    return _rewrite_tree(obj, _prefix_fn(to_host))[0]


def mirror_lockfile(src_text: str) -> str:
    """Rewrite a host lockfile's JSON for the jail: host paths -> /workspace."""
    return json.dumps(rewrite(json.loads(src_text), to_host=False))


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp sibling then rename, so a scanner never sees a torn file.

    The temp name is not `*.lock`, so neither the in-jail CLI nor the prune below
    picks it up mid-write.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _scan_sources() -> "dict[int, Path]":
    """Valid `{port: path}` for the `<port>.lock` files in HOST_IDE_DIR.

    The port is the lockfile's name (`<port>.lock`), not a field in its JSON, and
    must be in range — a non-numeric or out-of-range name is skipped, mirroring
    forward.py (start_server raises OverflowError, not OSError, on a bad port).
    Scanned once per poll and fed to both the listener reconcile and sync_mirror,
    so HOST_IDE_DIR is never globbed twice in one iteration, and so a listener is
    bound *before* its lockfile is published (a mirror never leads its live relay).
    """
    sources: "dict[int, Path]" = {}
    try:
        locks = HOST_IDE_DIR.glob("*.lock")
    except OSError:
        return sources
    for lock in locks:
        try:
            port = int(lock.stem)
        except ValueError:
            continue
        if port in _VALID_PORT:
            sources[port] = lock
    return sources


def sync_mirror(sources: "dict[int, Path]", served: "set[int]",
                seen: "dict[str, float]") -> None:
    """Make /jail-ide reflect the served host lockfiles (rewritten).

    `sources` is _scan_sources()'s `{port: path}` (so HOST_IDE_DIR isn't re-
    globbed here). `served` restricts the published mirror to ports with a live
    listener: the in-jail CLI then never sees a `<port>.lock` for a port nothing
    is serving (still binding, or whose bind failed). Any other mirror — source
    gone, or port not served — is pruned. `seen` caches each source's mtime so an
    unchanged lockfile isn't re-rewritten every poll.
    """
    published: "set[str]" = set()
    for port, src in sources.items():
        if port not in served:
            continue  # not listening on it yet: don't advertise its lockfile
        name = src.name
        published.add(name)
        try:
            mtime = src.stat().st_mtime
        except OSError:
            continue
        target = JAIL_IDE_DIR / name
        # Re-mirror when the source changed OR the mirror is gone: a concurrent
        # sibling session sharing this dir can delete our mirror out-of-band
        # (prepare()/cleanup()'s clear), and the source mtime is then unchanged,
        # so an mtime-only check would never restore it. Checking the target's
        # existence is what makes the documented <=1s self-heal actually hold.
        if seen.get(name) != mtime or not target.exists():
            try:
                text = mirror_lockfile(src.read_text(encoding="utf-8"))
                _atomic_write(target, text)
            except (OSError, ValueError):
                continue
            seen[name] = mtime
    try:
        for mirror in JAIL_IDE_DIR.glob("*.lock"):
            if mirror.name not in published:
                mirror.unlink(missing_ok=True)
                seen.pop(mirror.name, None)
    except OSError:
        pass


def rewrite_message(data: bytes, to_host: bool) -> bytes:
    """Rewrite the /workspace prefix in a JSON text frame; pass non-JSON through.

    A frame with no `/` byte can hold no absolute path or `file://` URI, so it is
    forwarded unchanged — most protocol chatter, spared a parse+reserialize. A
    frame that parses but whose leaves don't move (a `/` only in a non-path value
    like a mimetype) is likewise returned verbatim, so only frames that actually
    change pay the reserialize.
    """
    if b"/" not in data:
        return data
    try:
        obj = json.loads(data)  # json.loads decodes bytes itself; no extra pass
    except (UnicodeDecodeError, ValueError):
        return data
    new, changed = _rewrite_tree(obj, _prefix_fn(to_host))
    if not changed:
        return data
    return json.dumps(new).encode("utf-8")


def _mask(payload: bytes, key: bytes) -> bytes:
    """XOR a payload with a repeating 4-byte key as one big-int op, not per byte."""
    if not payload:
        return payload
    n = len(payload)
    repeated = key * (n // 4) + key[: n % 4]
    masked = int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")
    return masked.to_bytes(n, "big")


def build_frame(fin: bool, opcode: int, payload: bytes, mask: bool) -> bytes:
    """Encode a WebSocket frame; client->server frames (mask=True) are masked."""
    out = bytearray([(0x80 if fin else 0) | opcode])
    flag = 0x80 if mask else 0
    length = len(payload)
    if length < 126:
        out.append(flag | length)
    elif length < 65536:
        out.append(flag | 126)
        out += length.to_bytes(2, "big")
    else:
        out.append(flag | 127)
        out += length.to_bytes(8, "big")
    if mask:
        key = os.urandom(4)
        out += key
        out += _mask(payload, key)
    else:
        out += payload
    return bytes(out)


async def _read_frame(reader: asyncio.StreamReader):
    """Read one frame -> (fin, opcode, payload) with the payload unmasked, or None.

    None on a short read or an over-cap declared length (refusing the frame
    rather than buffering an arbitrary payload).
    """
    try:
        head = await reader.readexactly(2)
        fin, opcode = bool(head[0] & 0x80), head[0] & 0x0F
        masked, length = bool(head[1] & 0x80), head[1] & 0x7F
        if length == 126:
            length = int.from_bytes(await reader.readexactly(2), "big")
        elif length == 127:
            length = int.from_bytes(await reader.readexactly(8), "big")
        if length > MAX_FRAME:
            print(f"ide-relay: dropping connection; declared frame length "
                  f"{length} exceeds the {MAX_FRAME} cap", flush=True)
            return None
        key = await reader.readexactly(4) if masked else b""
        payload = await reader.readexactly(length) if length else b""
    except asyncio.IncompleteReadError:
        return None
    if masked:
        payload = _mask(payload, key)
    return fin, opcode, payload


def _drop_extensions(header: bytes) -> bytes:
    """Strip Sec-WebSocket-Extensions lines from a handshake header block.

    The frame codec assumes no extension (RSV bits clear, uncompressed payloads).
    Dropping the client's offer keeps the server from negotiating permessage-
    deflate, so the simple parser stays correct end to end. An obs-fold header
    (the value continued on lines that start with whitespace) is dropped together
    with its continuation lines — a value split across lines must not slip the
    extension past a bare line-prefix match and let compression negotiate.
    """
    kept: "list[bytes]" = []
    dropping = False
    for ln in header.split(b"\r\n"):
        if ln[:1] in (b" ", b"\t"):  # continuation of the previous header
            if dropping:
                continue
        else:
            dropping = ln.lower().startswith(b"sec-websocket-extensions:")
            if dropping:
                continue
        kept.append(ln)
    return b"\r\n".join(kept)


def _upgrade_rejected(header: bytes) -> bool:
    """True when `header` is an HTTP response that is NOT a 101 upgrade.

    Only the editor->CLI direction carries a status line (the CLI->editor
    direction is a `GET` request); a non-101 status there means the editor
    refused the upgrade, so the bytes that follow are an HTTP body, not WS
    frames, and must not be fed to _read_frame.
    """
    if not header.startswith(b"HTTP/"):
        return False  # a request line (e.g. `GET ... HTTP/1.1`), not a response
    parts = header.split(b"\r\n", 1)[0].split(b" ", 2)
    return len(parts) < 2 or parts[1] != b"101"


async def _ws_pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                   mask_out: bool, to_host: bool) -> None:
    try:
        header = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        writer.close()
        return
    writer.write(_drop_extensions(header))
    await writer.drain()
    if _upgrade_rejected(header):
        # The editor refused the upgrade (401/426/...): what follows is an HTTP
        # body, not WS frames. Relay it opaquely so the rejection reaches the
        # peer intact, then let the connection close — never parse it as frames.
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
        return
    fragments: "list[bytes]" = []
    buffered = 0
    opcode = None
    try:
        while True:
            frame = await _read_frame(reader)
            if frame is None:
                break
            fin, op, payload = frame
            if op >= 0x8:  # control frame (close/ping/pong): forward verbatim
                writer.write(build_frame(fin, op, payload, mask_out))
                await writer.drain()
                if op == 0x8:
                    break
                continue
            if op != 0x0:
                if opcode is not None:
                    break  # a new data frame before the previous finished: malformed
                opcode = op
            elif opcode is None:
                break  # a continuation with no initiating data frame: malformed
            fragments.append(payload)
            buffered += len(payload)
            if buffered > MAX_FRAME:
                # _read_frame caps a single frame, but fragmentation would defeat
                # that — bound the reassembled message too, so a peer can't grow it
                # without limit and OOM the relay.
                print(f"ide-relay: dropping connection; reassembled message "
                      f"exceeds the {MAX_FRAME} cap", flush=True)
                break
            if not fin:
                continue
            message = b"".join(fragments)
            fragments = []
            buffered = 0
            if opcode in (0x1, 0x2):  # text or binary: a JSON payload may carry paths
                message = rewrite_message(message, to_host)
            writer.write(build_frame(True, opcode, message, mask_out))
            await writer.drain()
            opcode = None
    except OSError:
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def _handle(local_reader: asyncio.StreamReader,
                  local_writer: asyncio.StreamWriter, port: int,
                  dial_failed: "set[int]") -> None:
    try:
        # Same raised limit as the listen side, so the editor's 101-response
        # handshake header isn't bounded by readuntil's default 64 KiB.
        remote_reader, remote_writer = await asyncio.open_connection(
            TARGET, port, limit=_STREAM_LIMIT)
    except OSError as e:
        # We listen here, but the chain to the editor (ide-host binding
        # gateway:P, then the editor on 127.0.0.1:P) may not be up yet, or
        # ide-host's bind for this port failed. Say so once per port — otherwise
        # /ide reads a published lockfile and every connect is silently dropped.
        if port not in dial_failed:
            dial_failed.add(port)
            print(f"ide-relay: cannot reach the editor at {TARGET}:{port} ({e}); "
                  f"is the ide-host forwarder up? /ide will not connect to it",
                  flush=True)
        local_writer.close()
        return
    dial_failed.discard(port)
    pumps = [
        asyncio.ensure_future(
            _ws_pump(local_reader, remote_writer, mask_out=True, to_host=True)),
        asyncio.ensure_future(
            _ws_pump(remote_reader, local_writer, mask_out=False, to_host=False)),
    ]
    try:
        # When either direction ends, tear the other down too: closing only one
        # writer does not make a half-open peer's read EOF, so without this the
        # surviving pump (and its sockets) would leak. But first give the
        # survivor a bounded grace to drain frames already in flight (e.g. a
        # final data frame or the reciprocal close), so a clean close isn't
        # truncated into an abrupt reset.
        _, pending = await asyncio.wait(pumps,
                                        return_when=asyncio.FIRST_COMPLETED)
        if pending:
            await asyncio.wait(pending, timeout=_CLOSE_DRAIN_SECONDS)
    finally:
        for w in (local_writer, remote_writer):
            try:
                w.close()
            except OSError:
                pass
        for p in pumps:
            p.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)


def _bind_ip() -> "str | None":
    """The relay's own address on the jail-internal interface, so it listens
    there only and isn't reachable from jail-egress (where it also sits to dial
    the host editor).

    Returns None when the subnet is unknown or the local address can't be
    probed; the caller then refuses to start (like forward.py's empty-gateway
    guard) rather than binding every interface — degrading *closed*, not open, so
    the editor proxy is never exposed on jail-egress. The launcher always
    supplies JAIL_NET_SUBNET, so None means a misconfiguration or an
    out-of-launcher run, not the normal path.
    """
    subnet = os.environ.get("JAIL_NET_SUBNET", "")
    if not subnet:
        return None
    try:
        net = ipaddress.ip_network(subnet, strict=False)
        probe = str(net.network_address + 1)  # the jail-internal gateway
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((probe, 9))  # UDP connect routes but sends nothing
            return sock.getsockname()[0]
        finally:
            sock.close()
    except (OSError, ValueError):
        return None


async def _serve(bind_ip: str) -> None:
    servers: "dict[int, asyncio.Server]" = {}
    seen: "dict[str, float]" = {}
    failed: "set[int]" = set()
    dial_failed: "set[int]" = set()
    JAIL_IDE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"ide-relay: watching {HOST_IDE_DIR} -> {JAIL_IDE_DIR} "
          f"(target {TARGET}, bind {bind_ip})", flush=True)
    live: "set[int]" = set()
    try:
        while True:
            # One scan per poll, off the event loop (it does blocking globs), so
            # live WebSocket pumps aren't stalled.
            sources = await asyncio.to_thread(_scan_sources)
            desired = set(sources)
            failed &= desired
            dial_failed &= desired
            # Bind the listener before sync_mirror publishes its lockfile, so the
            # in-jail CLI never sees a <port>.lock for a port nothing is serving.
            for port in desired - set(servers):
                try:
                    servers[port] = await asyncio.start_server(
                        lambda r, w, p=port: _handle(r, w, p, dial_failed),
                        bind_ip, port, limit=_STREAM_LIMIT)
                    failed.discard(port)
                except OSError as e:
                    if port not in failed:  # report a stuck bind once, not silently
                        failed.add(port)
                        print(f"ide-relay: cannot listen on {bind_ip}:{port} "
                              f"({e}); /ide will not reach that editor",
                              flush=True)
            for port in set(servers) - desired:
                server = servers.pop(port)
                server.close()
                try:
                    await server.wait_closed()
                except OSError:
                    pass
            # Mirror only the lockfiles whose port now has a live listener; the
            # blocking filesystem work also runs off the event loop.
            await asyncio.to_thread(sync_mirror, sources, set(servers), seen)
            if set(servers) != live:  # report only ports actually being served
                live = set(servers)
                print(f"ide-relay: serving ports {sorted(live)}", flush=True)
            await asyncio.sleep(POLL_SECONDS)
    finally:
        for server in servers.values():
            server.close()


def main() -> None:
    # PID 1 gets no default signal action, and asyncio.run only turns SIGINT into
    # KeyboardInterrupt; map SIGTERM to it too so `docker compose stop`/`down`
    # shuts the loop down cleanly instead of waiting out the grace period.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    bind_ip = _bind_ip()
    if bind_ip is None:
        sys.exit("relay: could not determine the jail-internal bind address; "
                 "refusing to bind all interfaces")
    try:
        asyncio.run(_serve(bind_ip))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
