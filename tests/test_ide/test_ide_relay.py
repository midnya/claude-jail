"""The relay's pure path rewriting, lockfile mirroring, and WS frame codec."""
import asyncio
import contextlib
import io
import json
from pathlib import Path
from unittest import mock

from ide import relay
from jail_test_helpers import JailTestCase  # noqa: I001


class StripWorkspaceTests(JailTestCase):
    def test_paths(self):
        self.assertEqual(relay._strip_workspace("/workspace/a/b"), "/a/b")
        self.assertEqual(relay._strip_workspace("/workspace"), "/")
        self.assertEqual(relay._strip_workspace("/etc/x"), "/etc/x")  # untouched
        self.assertEqual(relay._strip_workspace("relative"), "relative")

    def test_file_uris(self):
        self.assertEqual(relay._strip_workspace("file:///workspace/a"),
                         "file:///a")
        self.assertEqual(relay._strip_workspace("file:///etc/x"),
                         "file:///etc/x")


class AddWorkspaceTests(JailTestCase):
    def test_paths(self):
        self.assertEqual(relay._add_workspace("/home/u/p"), "/workspace/home/u/p")
        self.assertEqual(relay._add_workspace("/workspace/x"), "/workspace/x")
        self.assertEqual(relay._add_workspace("relative"), "relative")  # untouched

    def test_file_uris(self):
        self.assertEqual(relay._add_workspace("file:///home/u"),
                         "file:///workspace/home/u")
        self.assertEqual(relay._add_workspace("file:///workspace/x"),
                         "file:///workspace/x")

    def test_host_path_round_trips(self):
        # add then strip recovers the original host path (the mount map is a
        # reversible prefix), so the two directions can't desync.
        for host in ("/home/u/proj/f.py", "file:///home/u/proj"):
            self.assertEqual(
                relay._strip_workspace(relay._add_workspace(host)), host)

    def test_workspace_root_round_trips_both_ways(self):
        # The mount root: container /workspace <-> host /, no spurious trailing
        # slash in either direction (a single-folder workspace can carry it).
        self.assertEqual(relay._add_workspace("/"), "/workspace")
        self.assertEqual(relay._add_workspace("file:///"), "file:///workspace")
        self.assertEqual(
            relay._add_workspace(relay._strip_workspace("/workspace")),
            "/workspace")
        self.assertEqual(
            relay._add_workspace(relay._strip_workspace("file:///workspace")),
            "file:///workspace")


class RewriteWalkTests(JailTestCase):
    def test_deep_walk_only_touches_string_leaves(self):
        obj = {"method": "openDiff",
               "params": {"paths": ["/workspace/a", "/workspace/b"],
                          "count": 2, "flag": True, "name": "x"}}
        out = relay.rewrite(obj, to_host=True)
        self.assertEqual(out["params"]["paths"], ["/a", "/b"])
        self.assertEqual(out["params"]["count"], 2)      # ints untouched
        self.assertEqual(out["params"]["flag"], True)    # bools untouched
        self.assertEqual(out["method"], "openDiff")      # non-path str untouched

    def test_keys_are_not_rewritten(self):
        out = relay.rewrite({"/workspace/k": "v"}, to_host=True)
        self.assertEqual(out, {"/workspace/k": "v"})     # dict key left alone


class MirrorLockfileTests(JailTestCase):
    def test_workspace_folders_prefixed_token_and_port_preserved(self):
        src = json.dumps({
            "pid": 99, "port": 12345, "ideName": "VS Code", "transport": "ws",
            "runningInWindows": False, "authToken": "secret-abc",
            "workspaceFolders": ["/home/u/proj"],
        })
        out = json.loads(relay.mirror_lockfile(src))
        self.assertEqual(out["workspaceFolders"], ["/workspace/home/u/proj"])
        self.assertEqual(out["authToken"], "secret-abc")   # verbatim
        self.assertEqual(out["port"], 12345)               # verbatim
        self.assertEqual(out["ideName"], "VS Code")        # non-path, untouched
        self.assertEqual(out["transport"], "ws")


class SyncMirrorTests(JailTestCase):
    def _dirs(self):
        host, jail = Path(self.tmpdir()), Path(self.tmpdir())
        return host, jail, mock.patch.multiple(
            relay, HOST_IDE_DIR=host, JAIL_IDE_DIR=jail)

    def _sync(self, served=None, seen=None):
        """Drive the production path: scan once, then mirror the served ports.

        `served=None` means "every discovered port" (the steady state where each
        port already has a live listener); `seen=None` is a fresh mtime cache.
        """
        sources = relay._scan_sources()
        relay.sync_mirror(sources,
                          set(sources) if served is None else served,
                          {} if seen is None else seen)
        return sources

    def test_port_from_filename_and_content_rewritten(self):
        host, jail, patched = self._dirs()
        (host / "25179.lock").write_text(json.dumps(
            {"authToken": "secret", "workspaceFolders": ["/home/u/proj"]}))
        with patched:
            self.assertEqual(set(self._sync()), {25179})
        out = json.loads((jail / "25179.lock").read_text())
        self.assertEqual(out["workspaceFolders"], ["/workspace/home/u/proj"])
        self.assertEqual(out["authToken"], "secret")

    def test_non_numeric_name_skipped(self):
        host, jail, patched = self._dirs()
        (host / "abc.lock").write_text("{}")
        with patched:
            self.assertEqual(set(self._sync()), set())
        self.assertEqual(list(jail.glob("*.lock")), [])

    def test_out_of_range_port_skipped(self):
        # A numeric but out-of-range name must not reach start_server, which
        # raises OverflowError (not OSError) on a bad port and would crash _serve.
        host, jail, patched = self._dirs()
        (host / "70000.lock").write_text("{}")
        with patched:
            self.assertEqual(set(self._sync()), set())
        self.assertEqual(list(jail.glob("*.lock")), [])

    def test_unchanged_lockfile_not_rewritten_again(self):
        # With a persistent mtime cache, a steady-state poll does not rewrite the
        # mirror.
        host, jail, patched = self._dirs()
        (host / "25179.lock").write_text(json.dumps({"authToken": "x"}))
        seen: "dict[str, float]" = {}
        with patched:
            self._sync(seen=seen)
            mirror = jail / "25179.lock"
            mtime = mirror.stat().st_mtime_ns
            self._sync(seen=seen)
            self.assertEqual(mirror.stat().st_mtime_ns, mtime)  # not rewritten

    def test_stale_mirror_dropped(self):
        host, jail, patched = self._dirs()
        (jail / "999.lock").write_text("{}")
        with patched:
            self._sync()
        self.assertEqual(list(jail.glob("*.lock")), [])

    def test_missing_mirror_recreated_despite_cached_mtime(self):
        # Self-heal: a concurrent sibling session sharing this dir can delete the
        # mirror out-of-band while `seen` still holds the (unchanged) source
        # mtime. The next sync must restore it from the existence check, not skip
        # it on the stale mtime cache.
        host, jail, patched = self._dirs()
        (host / "25179.lock").write_text(json.dumps({"authToken": "x"}))
        seen: "dict[str, float]" = {}
        with patched:
            self._sync(seen=seen)
            mirror = jail / "25179.lock"
            self.assertTrue(mirror.exists())
            mirror.unlink()              # sibling wipes the shared mirror
            self._sync(seen=seen)        # seen still caches the source mtime
        self.assertTrue(mirror.exists())  # restored, not left missing

    def test_served_restricts_published_mirror(self):
        # With `served`, only a port actually being listened on is advertised, so
        # the in-jail CLI never sees a lockfile for a port with no live relay.
        host, jail, patched = self._dirs()
        (host / "25179.lock").write_text(json.dumps({"authToken": "a"}))
        (host / "25180.lock").write_text(json.dumps({"authToken": "b"}))
        with patched:
            sources = relay._scan_sources()
            self.assertEqual(set(sources), {25179, 25180})    # both discovered
            relay.sync_mirror(sources, {25179}, {})
        self.assertTrue((jail / "25179.lock").exists())   # served -> published
        self.assertFalse((jail / "25180.lock").exists())  # not served -> not yet

    def test_served_prunes_mirror_when_port_stops_being_served(self):
        # A port whose listener went away (or never bound) must lose its mirror.
        host, jail, patched = self._dirs()
        (host / "25179.lock").write_text(json.dumps({"authToken": "a"}))
        seen: "dict[str, float]" = {}
        with patched:
            sources = relay._scan_sources()
            relay.sync_mirror(sources, {25179}, seen)
            self.assertTrue((jail / "25179.lock").exists())
            relay.sync_mirror(sources, set(), seen)  # no longer served
        self.assertFalse((jail / "25179.lock").exists())


class ScanSourcesTests(JailTestCase):
    def test_valid_ports_discovered_without_mirroring(self):
        host, jail = Path(self.tmpdir()), Path(self.tmpdir())
        (host / "25179.lock").write_text("{}")
        (host / "abc.lock").write_text("{}")    # non-numeric -> skipped
        (host / "70000.lock").write_text("{}")  # out of range -> skipped
        with mock.patch.multiple(relay, HOST_IDE_DIR=host, JAIL_IDE_DIR=jail):
            sources = relay._scan_sources()
        self.assertEqual(set(sources), {25179})
        self.assertEqual(sources[25179].name, "25179.lock")
        self.assertEqual(list(jail.glob("*.lock")), [])  # discovery writes nothing

    def test_missing_dir_is_empty(self):
        with mock.patch.object(relay, "HOST_IDE_DIR", Path(self.tmpdir()) / "no"):
            self.assertEqual(relay._scan_sources(), {})


class BindIpTests(JailTestCase):
    def test_no_subnet_is_none(self):
        # No subnet -> refuse-closed (None), never bind every interface.
        with mock.patch.dict(relay.os.environ, {"JAIL_NET_SUBNET": ""}):
            self.assertIsNone(relay._bind_ip())

    def test_garbage_subnet_is_none(self):
        with mock.patch.dict(relay.os.environ, {"JAIL_NET_SUBNET": "not-a-cidr"}):
            self.assertIsNone(relay._bind_ip())


def _read_back(raw):
    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        reader.feed_eof()
        return await relay._read_frame(reader)
    return asyncio.run(go())


class FrameCodecTests(JailTestCase):
    def _roundtrip(self, fin, opcode, payload, mask):
        return _read_back(relay.build_frame(fin, opcode, payload, mask))

    def test_unmasked_roundtrip(self):
        self.assertEqual(self._roundtrip(True, 0x1, b'{"a":1}', False),
                         (True, 0x1, b'{"a":1}'))

    def test_masked_frame_is_unmasked_on_read(self):
        self.assertEqual(self._roundtrip(True, 0x1, b"hello world", True),
                         (True, 0x1, b"hello world"))

    def test_16bit_length(self):
        payload = b"x" * 300
        self.assertEqual(self._roundtrip(True, 0x2, payload, False),
                         (True, 0x2, payload))

    def test_64bit_length(self):
        payload = b"y" * 70000
        self.assertEqual(self._roundtrip(False, 0x2, payload, True),
                         (False, 0x2, payload))

    def test_truncated_frame_returns_none(self):
        self.assertIsNone(_read_back(b"\x81"))  # one byte: header incomplete

    def test_over_cap_length_returns_none(self):
        # A declared length past MAX_FRAME is refused before any payload is read,
        # so a peer can't make us buffer an arbitrary blob. The refusal also logs
        # (so the teardown isn't silent); swallow that line here.
        raw = bytes([0x82, 0x7F]) + (relay.MAX_FRAME + 1).to_bytes(8, "big")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(_read_back(raw))


class RewriteMessageTests(JailTestCase):
    def test_to_host_strips_prefix(self):
        msg = json.dumps({"path": "/workspace/home/u/f.py"}).encode()
        out = json.loads(relay.rewrite_message(msg, to_host=True))
        self.assertEqual(out["path"], "/home/u/f.py")

    def test_to_container_adds_prefix(self):
        msg = json.dumps({"path": "/home/u/f.py"}).encode()
        out = json.loads(relay.rewrite_message(msg, to_host=False))
        self.assertEqual(out["path"], "/workspace/home/u/f.py")

    def test_non_json_passes_through(self):
        self.assertEqual(relay.rewrite_message(b"\x00\x01\x02", to_host=True),
                         b"\x00\x01\x02")

    def test_frame_without_slash_passes_through_verbatim(self):
        # No '/' byte -> no path or file:// to rewrite -> forwarded unchanged
        # (and byte-identical, sparing a parse+reserialize).
        msg = json.dumps({"jsonrpc": "2.0", "id": 1}).encode()
        self.assertEqual(relay.rewrite_message(msg, to_host=True), msg)
        self.assertEqual(relay.rewrite_message(msg, to_host=False), msg)

    def test_slash_but_no_path_returns_verbatim(self):
        # Has a '/' (so it parses) but no leaf actually moves -> the original
        # bytes are returned, not a reserialized copy (no dumps/encode).
        msg = json.dumps({"mime": "text/plain", "id": 1}).encode()
        self.assertEqual(relay.rewrite_message(msg, to_host=True), msg)
        self.assertEqual(relay.rewrite_message(msg, to_host=False), msg)


class DropExtensionsTests(JailTestCase):
    def test_extensions_header_removed(self):
        header = (b"GET / HTTP/1.1\r\nHost: ide-relay\r\n"
                  b"Sec-WebSocket-Extensions: permessage-deflate\r\n"
                  b"Sec-WebSocket-Key: abc\r\n\r\n")
        out = relay._drop_extensions(header)
        self.assertNotIn(b"permessage-deflate", out)
        self.assertIn(b"Sec-WebSocket-Key: abc", out)  # others untouched
        self.assertTrue(out.endswith(b"\r\n\r\n"))

    def test_header_without_extensions_unchanged(self):
        header = b"GET / HTTP/1.1\r\nHost: ide-relay\r\n\r\n"
        self.assertEqual(relay._drop_extensions(header), header)

    def test_obs_fold_continuation_also_dropped(self):
        # A value folded onto continuation lines (leading whitespace) must be
        # dropped with its header, or compression could negotiate past the strip
        # against the RSV-unaware codec.
        header = (b"GET / HTTP/1.1\r\nHost: ide-relay\r\n"
                  b"Sec-WebSocket-Extensions: permessage-deflate;\r\n"
                  b"\tclient_max_window_bits\r\n"
                  b"Sec-WebSocket-Key: abc\r\n\r\n")
        out = relay._drop_extensions(header)
        self.assertNotIn(b"permessage-deflate", out)
        self.assertNotIn(b"client_max_window_bits", out)  # continuation gone too
        self.assertIn(b"Sec-WebSocket-Key: abc", out)      # next header survives
        self.assertTrue(out.endswith(b"\r\n\r\n"))


class _CapWriter:
    """A minimal StreamWriter stand-in: records writes, no real socket."""

    def __init__(self):
        self.chunks: "list[bytes]" = []
        self.closed = False

    def write(self, data):
        self.chunks.append(bytes(data))

    async def drain(self):
        pass

    def close(self):
        self.closed = True


def _run_pump(raw, mask_out=False, to_host=True):
    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        reader.feed_eof()
        writer = _CapWriter()
        await relay._ws_pump(reader, writer, mask_out=mask_out, to_host=to_host)
        return writer
    return asyncio.run(go())


class WsPumpTests(JailTestCase):
    HS = b"GET / HTTP/1.1\r\nHost: ide-relay\r\n\r\n"

    def test_reassembles_fragmented_text(self):
        # A text message split across an initial + continuation frame is
        # reassembled and re-emitted as one frame.
        raw = (self.HS
               + relay.build_frame(False, 0x1, b'{"a":', False)
               + relay.build_frame(True, 0x0, b'1}', False))
        w = _run_pump(raw)
        body = b"".join(w.chunks[1:])  # chunks[0] is the handshake echo
        self.assertEqual(_read_back(body), (True, 0x1, b'{"a":1}'))

    def test_orphan_continuation_drops_connection(self):
        # A continuation (op 0x0) with no initiating data frame is malformed: the
        # connection is torn down rather than the stray bytes being forwarded.
        raw = self.HS + relay.build_frame(True, 0x0, b"x", False)
        with contextlib.redirect_stdout(io.StringIO()):
            w = _run_pump(raw)
        self.assertTrue(w.closed)
        self.assertEqual(w.chunks, [relay._drop_extensions(self.HS)])  # no frame

    def test_interleaved_data_frame_drops_connection(self):
        # A second initiating data frame before the first message's fin is
        # malformed; the stream is dropped, not silently merged.
        raw = (self.HS
               + relay.build_frame(False, 0x1, b"ab", False)
               + relay.build_frame(True, 0x1, b"cd", False))
        with contextlib.redirect_stdout(io.StringIO()):
            w = _run_pump(raw)
        self.assertTrue(w.closed)
        self.assertEqual(w.chunks, [relay._drop_extensions(self.HS)])  # no frame

    def test_reassembled_message_over_cap_drops_connection(self):
        # The per-frame MAX_FRAME cap is defeated by fragmentation, so the
        # reassembled total is capped too; a peer can't grow it without limit.
        with mock.patch.object(relay, "MAX_FRAME", 10):
            raw = (self.HS
                   + relay.build_frame(False, 0x1, b"abcdef", False)
                   + relay.build_frame(True, 0x0, b"ghijkl", False))
            with contextlib.redirect_stdout(io.StringIO()):
                w = _run_pump(raw)
        self.assertTrue(w.closed)
        self.assertEqual(w.chunks, [relay._drop_extensions(self.HS)])  # no frame

    def test_binary_frame_payload_is_rewritten(self):
        # A JSON payload framed as binary (0x2) still carries paths; it must be
        # rewritten like text, and re-emitted with its binary opcode preserved.
        payload = json.dumps({"path": "/workspace/home/u/f.py"}).encode()
        raw = self.HS + relay.build_frame(True, 0x2, payload, False)
        w = _run_pump(raw, to_host=True)
        fin, op, out = _read_back(b"".join(w.chunks[1:]))
        self.assertEqual(op, 0x2)  # opcode preserved
        self.assertEqual(json.loads(out)["path"], "/home/u/f.py")  # prefix stripped

    def test_non_101_response_relayed_not_parsed_as_frames(self):
        # A rejected upgrade (non-101) is an HTTP body, not WS frames: the relay
        # must forward it opaquely and close, never feed it to the frame reader.
        resp = (b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 6\r\n\r\n"
                b"denied")
        w = _run_pump(resp, to_host=False)
        self.assertTrue(w.closed)
        forwarded = b"".join(w.chunks)
        self.assertIn(b"401 Unauthorized", forwarded)  # rejection reached peer
        self.assertIn(b"denied", forwarded)            # body passed through raw
