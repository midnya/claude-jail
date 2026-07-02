#!/usr/bin/env python3
"""In-container git-review plumbing: attach a throwaway .git, then bundle it.

Runs inside the jail (the entrypoint calls `jail-workspace init` before claude
and `jail-workspace export` after) when review isolation is on. It is the half
of the feature that must run container-side, because the writable .git borrows
objects from the real repo via an alternate at /cvs/git — a *container* path that
only resolves here.

  init   The working tree is a live two-way mount; its .git is an empty writable
         volume shadowing the host's real .git. Turn that volume into a clone of
         the read-only original at /cvs/git: init, point an alternate at the
         original's objects (borrow, don't copy), fetch its refs, then set HEAD
         and the index to the original's HEAD *without* touching the live working
         tree — so the user's uncommitted changes still show up as diffs. Every
         submodule gitdir is rebuilt the same way (the live submodule worktrees
         carry a .git file into the shadow .git/modules, empty otherwise).

  export Bundle every commit the session added (excluding the history the user
         already has, listed in the spool's pending.base) into the host-mounted
         spool, plus the end-state branch list so the host can spot deletions.

Standalone and stdlib-only, like relay/forward; configured entirely by env.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

# Review mode is the first time the agent can commit (real .git was read-only), so
# the throwaway repos need an identity or commits fail; a pre-existing one wins.
_DEFAULT_IDENTITY = (("user.name", "claude-jail"),
                     ("user.email", "claude-jail@localhost"))

# When the original is on a detached HEAD, the session is parked on this branch so
# its commits ride a ref `bundle create --all` and the heads/* import refspec carry
# — a detached HEAD reaches neither, and the work would vanish from review.
_DETACHED_BRANCH = "jail-detached-head"

# git environment variables that redirect *where* git operates. Scrubbed from the
# child env (mirrors review.py._GIT_ENV_REDIRECTS) so an inherited value can't
# point init/export's ref, index, or `bundle create` ops at the wrong store — our
# explicit `-C <workdir>` / `--git-dir` always wins.
_GIT_ENV_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_OBJECT_DIRECTORY",
                      "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_INDEX_FILE",
                      "GIT_COMMON_DIR", "GIT_NAMESPACE")


def _run(*args: str, check: bool = True,
         stdin: "str | None" = None) -> "subprocess.CompletedProcess":
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_REDIRECTS}
    result = subprocess.run(["git", *args], capture_output=True, text=True,
                            errors="surrogateescape", input=stdin, env=env)
    if check and result.returncode != 0:
        _eprint(f"jail-workspace: git {' '.join(args)} failed: "
                f"{result.stderr.strip()}")
        sys.exit(1)
    return result


# Terminal-control scrubbing for untrusted git output before it reaches stderr.
# During an interactive `docker compose run` stderr *is* the host terminal, and
# the export diagnostics run after claude's TUI is gone (the entrypoint's EXIT
# trap), so a refname/path carrying C1/OSC/CSI bytes (git refnames may legally
# hold C1 controls) would otherwise relay a live cursor-move/screen-clear escape
# straight to the host — the very injection review.py's host-side _sanitize stops.
# Mirrors review.py's regexes; kept local since this is a standalone container
# script (stdlib-only, like relay/forward).
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI_RE = re.compile(r"\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\udc80-\udc9f]")  # keep \t \n


def _sanitize(text: str) -> str:
    """Strip OSC/CSI/cursor escapes and raw control bytes (keeping tab/newline)."""
    text = _OSC_RE.sub("", text)
    return _CTRL_RE.sub("", _CSI_RE.sub("", text).replace("\x1b", ""))


def _eprint(msg: str) -> None:
    """Write a diagnostic to stderr, control-sanitised and surrogateescape-safe.

    A non-UTF-8 refname/path decoded by _run's surrogateescape must round-trip
    rather than crash a strict-UTF-8 write; _sanitize first removes any terminal
    escapes so untrusted git output can't drive the host terminal.
    """
    text = _sanitize(msg)
    buf = getattr(sys.stderr, "buffer", None)
    if buf is None:                       # a StringIO under test / redirect
        sys.stderr.write(text + "\n")
        return
    sys.stderr.flush()
    buf.write((text + "\n").encode("utf-8", "surrogateescape"))
    buf.flush()


def _write_text(path: "Path", text: str) -> None:
    """Write git-derived text to a file, surrogateescape-safe.

    _run decodes git output — and os.environ / .git file contents are read —
    with surrogateescape, so a non-UTF-8 ref name, object-store path, or
    diagnostic must be re-encoded the same way. Path.write_text would re-encode
    strict UTF-8 and raise UnicodeEncodeError — aborting `init` (the shadow-.git
    setup writes) or, on the export path, being swallowed by the entrypoint's
    `jail-workspace export || true` and silently dropping the whole capture (no
    bundle, no pending.end, no warning). write_bytes round-trips the bytes
    losslessly instead.
    """
    path.write_bytes(text.encode("utf-8", "surrogateescape"))


def _is_parking_branch(name: str) -> bool:
    """True for a jail detached-HEAD parking branch (`jail-detached-head`[-N]).

    A detached HEAD (at init, or entered mid-session) is parked on such a branch so
    its commits ride a refs/heads/* ref the bundle and import both carry. The name
    is a jail artifact, not a user branch, so it is filtered out of the end-state
    branch list the host diffs to flag ref-only (add/delete) changes — otherwise it
    would surface as a spurious "added jail-detached-head".
    """
    return bool(re.fullmatch(re.escape(_DETACHED_BRANCH) + r"(-\d+)?", name))


def _attach(gitdir: str, original: str) -> bool:
    """Point an already-created gitdir at the read-only `original`.

    Borrows the original's objects (an alternate, not a copy), fetches its refs,
    matches HEAD, and gives the throwaway repo an identity. Returns True when HEAD
    resolves to a commit, so the caller can set the index to it.
    """
    original = str(original).rstrip("/")
    if "\n" in original or "\r" in original:
        # A newline in the path would inject a second line into the alternates
        # file (git reads one object-dir per line); refuse rather than borrow from
        # an attacker-chosen path. Caller falls back to a standalone repo.
        return False
    info = Path(gitdir) / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    # Borrow the original's objects — plus the original's *own* alternates when it
    # is itself a `clone --reference` repo, so an object that lives only in the
    # referenced store is still reachable. A referenced store outside the jail's
    # mounts can't be reached here; warn (rather than silently produce an
    # all-untracked tree with a later bundle failure) so the gap is visible.
    extra = ""
    orig_alt = Path(original) / "objects" / "info" / "alternates"
    if orig_alt.is_file():
        base_objs = Path(original) / "objects"
        for raw in orig_alt.read_text(errors="surrogateescape").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            p = Path(line)
            if not p.is_absolute():
                p = (base_objs / p).resolve()   # alternates may be relative to objects/
            extra += f"{p}\n"
            if not p.is_dir():
                _eprint(
                    "jail-workspace: WARNING: the original borrows objects from "
                    f"{p}, which is not reachable inside the jail; some history "
                    "may be missing from the captured bundle.")
    # surrogateescape write: `original` comes from os.environ (surrogateescape-
    # decoded) and `extra` from the original's alternates file, so a non-UTF-8
    # object-store path must round-trip rather than crash init on a strict encode.
    _write_text(info / "alternates", f"{original}/objects\n{extra}")
    # A shallow original: copy its boundary so history traversal stops cleanly at
    # the grafted edge instead of chasing parents the shallow clone never fetched.
    orig_shallow = Path(original) / "shallow"
    if orig_shallow.is_file():
        (Path(gitdir) / "shallow").write_text(orig_shallow.read_text())
    g = ("--git-dir", str(gitdir))
    # Park HEAD on a name the original is unlikely to have, so fetching every
    # branch normally never targets the (checked-out) current branch. --update-
    # head-ok then makes even a real `__jail_init__` branch in the original safe:
    # the fetch updates that ref without refusing, and HEAD is re-pointed below.
    _run(*g, "symbolic-ref", "HEAD", "refs/heads/__jail_init__")
    # check=False: a single unfetchable ref in the original (a branch pointing at
    # a missing/pruned object — fsck corruption, an interrupted filter-repo) would
    # otherwise sys.exit and abort the whole session, even though the same repo is
    # perfectly usable in a normal read-only-.git run. Fetch best-effort and warn
    # rather than fail closed; the refs that did transfer are still usable and the
    # export bundles whatever ended up reachable.
    fetch = _run(*g, "fetch", "-q", "--no-write-fetch-head", "--update-head-ok",
                 original, "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*",
                 check=False)
    if fetch.returncode != 0:
        _eprint(
            "jail-workspace: WARNING: fetching refs from the original repo failed "
            f"({fetch.stderr.strip()}); the shadow repo may be missing some "
            "branches/tags and the captured bundle may be incomplete.")
    # The original's HEAD is normally `ref: <name>` (on a branch) or a raw object
    # id (detached). Parse defensively: a malformed HEAD must leave the __jail_init__
    # sentinel and return False (caller skips the index reset) instead of crashing
    # init — an unborn/garbage HEAD is not worth aborting the whole session for.
    # surrogateescape: a checked-out branch with non-UTF-8 bytes makes HEAD
    # (`ref: refs/heads/<bytes>`) invalid UTF-8; a strict read would raise
    # UnicodeDecodeError — a ValueError the `except OSError` never catches — and
    # crash init, aborting the whole session. Round-trip the bytes so
    # symbolic-ref below re-points HEAD to the real branch name instead.
    try:
        head = (Path(original) / "HEAD").read_text(errors="surrogateescape").strip()
    except OSError:
        head = ""
    if head.startswith("ref:"):
        target = head[4:].strip()
        if target:
            _run(*g, "symbolic-ref", "HEAD", target)
    elif head:
        _run(*g, "update-ref", "--no-deref", "HEAD", head, check=False)
    for key, default in _DEFAULT_IDENTITY:
        if not _run(*g, "config", "--get", key, check=False).stdout.strip():
            _run(*g, "config", key, default)
    return _run(*g, "rev-parse", "--verify", "--quiet", "HEAD",
                check=False).returncode == 0


def _free_branch(workdir: str, base: str) -> str:
    """`base`, or `base-2`, `base-3`, ... — the first name with no existing branch.

    So parking a detached HEAD never force-moves a real branch the original happens
    to have under the reserved name."""
    name, n = base, 2
    while _run("-C", workdir, "show-ref", "--verify", "--quiet",
               f"refs/heads/{name}", check=False).returncode == 0:
        name, n = f"{base}-{n}", n + 1
    return name


def _park_detached_head(workdir: str, repoint: bool) -> None:
    """Park a detached HEAD on a fresh jail-detached-head[-N] branch, if needed.

    A detached HEAD's commit rides only the HEAD ref, which `bundle create --all`
    and the heads/* import refspec both skip — so its work would vanish from
    review. Creating a refs/heads/* ref at it (the free-name pick avoids force-
    moving a real branch the shadow already has under the reserved name) fixes
    that. With `repoint`, HEAD is also moved onto the new branch so mid-session
    commits land there (init, at start); without, HEAD is left detached (export, a
    last-moment safety net). No-op when HEAD is already on a branch or unborn.
    Shared by init and export so the two can't drift. All calls are best-effort:
    a park failure degrades the capture, it must not abort the whole session.
    """
    if _run("-C", workdir, "symbolic-ref", "-q", "HEAD",
            check=False).stdout.strip():
        return                                    # already on a branch
    if _run("-C", workdir, "rev-parse", "--verify", "-q", "HEAD^{commit}",
            check=False).returncode != 0:
        return                                    # unborn / unresolvable HEAD
    branch = _free_branch(workdir, _DETACHED_BRANCH)
    _run("-C", workdir, "branch", branch, "HEAD", check=False)
    if repoint:
        _run("-C", workdir, "symbolic-ref", "HEAD", f"refs/heads/{branch}",
             check=False)


def init(workdir: str, original: str) -> None:
    """Make `workdir`'s empty .git a reference clone of `original`, submodules too."""
    gitdir = str(Path(workdir) / ".git")
    _run("-C", workdir, "init", "-q")
    if _attach(gitdir, original):
        # Set the index to HEAD but leave the live working tree alone, so the
        # host's uncommitted/untracked files keep showing as the changes they are.
        # check=False (like the _attach fetch): a partial object graph the fetch
        # tolerated could make reset fail, and that must degrade the shadow rather
        # than abort the whole session, since the same repo opens fine in a normal
        # read-only-.git run.
        _run("-C", workdir, "reset", "--mixed", "-q", "HEAD", check=False)
        # If the original was on a detached HEAD, _attach reproduced that, so the
        # agent would commit onto a detached HEAD whose work review would drop.
        # Park it on a branch (a collision with a branch the user creates in the
        # real repo *during* the session is caught later by apply's old-value CAS).
        _park_detached_head(workdir, repoint=True)
    # Submodule gitdirs only need rebuilding when the repo actually has submodules.
    # Gate on .gitmodules (the same signal `submodule init` uses): without it git
    # can't have checked a submodule out, so there is no live .git pointer left
    # dangling to fix — and a submodule-free repo is spared the full-index scan
    # (_gitlink_paths' `ls-files --stage`) on the blocking pre-claude path.
    if (Path(workdir) / ".gitmodules").is_file():
        _attach_submodules(workdir, original)
        # Register submodule urls/paths from .gitmodules into the fresh .git/config
        # so `git submodule` (status/update/foreach) sees them as initialized.
        _run("-C", workdir, "submodule", "init", check=False)


def _gitlink_paths(worktree: "Path") -> "list[str]":
    """Tracked submodule paths (index gitlinks, mode 160000) under `worktree`.

    One `ls-files --stage` call rather than walking the tree for `.git` files —
    `rglob('.git')` would stat every entry under node_modules/.venv/build just to
    find a handful of pointers, blocking the entrypoint before claude starts.
    """
    out = _run("-C", str(worktree), "ls-files", "--stage", "-z", check=False).stdout
    paths = []
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        if meta.split(" ", 1)[0] == "160000":   # gitlink = a tracked submodule
            paths.append(path)
    return paths


def _attach_submodules(workdir: str, original: str) -> None:
    """Rebuild every submodule gitdir under the shadow .git/modules, recursively.

    See _rebuild_submodules; this is the top-level entry (the superproject's
    modules dir mirrors the original's).
    """
    _rebuild_submodules(Path(workdir), Path(workdir) / ".git" / "modules",
                        Path(original) / "modules")


def _submodule_name(worktree_root: "Path", path: str) -> str:
    """The .gitmodules name of the submodule checked out at `path`.

    git keys .git/modules by submodule *name*, which may differ from the checkout
    *path*; the canonicalize branch of _rebuild_submodules needs the name to find
    the matching original gitdir. Falls back to the path when .gitmodules has no
    matching entry (an unconfigured gitlink), which is no worse than before.
    """
    r = _run("-C", str(worktree_root), "config", "-f", ".gitmodules",
             "--get-regexp", r"^submodule\..*\.path$", check=False)
    for line in r.stdout.splitlines():
        key, _, value = line.partition(" ")
        if value == path and key.startswith("submodule.") and key.endswith(".path"):
            return key[len("submodule."):-len(".path")]
    return path


def _rebuild_submodules(worktree_root: "Path", shadow_modules: "Path",
                        orig_modules: "Path") -> None:
    """Rebuild the gitdir of every submodule directly under `worktree_root`, then
    recurse into each so nested submodules are rebuilt too.

    Driven by the index's gitlinks, whose live `.git` pointer files have dangling
    targets that make `git status` abort. Each gitdir is recreated where the
    pointer expects it (with `core.worktree` derived from the actual worktree);
    when the matching original gitdir is present it is reference-cloned like the
    superproject, else a valid *empty* repo is left so status still doesn't abort.
    A pointer that escapes the shadow modules dir (an absolute/relocated gitdir
    from an older git) is *canonicalized* into the shadow layout rather than
    skipped, so its dangling link is fixed instead of left to abort status. Nested
    submodules (a submodule that itself has submodules) are rebuilt by recursing
    into the submodule's own worktree/modules dir. (Submodule *commits* still
    aren't captured by the superproject bundle — only their gitlink is.)
    """
    # Resolve so a symlinked component anywhere under /workspace can't make the
    # relative_to() check spuriously reject a legitimate (relative) pointer.
    shadow_resolved = shadow_modules.resolve()
    for path in _gitlink_paths(worktree_root):
        gitfile = worktree_root / path / ".git"
        if not gitfile.is_file():
            continue  # submodule not checked out — no dangling pointer to fix
        # surrogateescape: a submodule .git pointer path with non-UTF-8 bytes must
        # round-trip rather than crash init on a strict read.
        spec = gitfile.read_text(errors="surrogateescape").strip()
        if not spec.startswith("gitdir:"):
            continue
        pointer = spec.split(":", 1)[1].strip()
        if "\n" in pointer or "\r" in pointer:
            # .strip() only trims the ends; an interior newline would survive into
            # the alternates file and the core.worktree config value (two-line
            # injection). A real gitdir path has none, so skip this pointer.
            continue
        worktree = gitfile.parent
        target = (worktree / pointer).resolve()
        try:
            rel = target.relative_to(shadow_resolved)
        except ValueError:
            # The live pointer is absolute or escapes the shadow modules dir (an
            # older git, a relocated/linked gitdir). Skipping it would leave the
            # dangling pointer that aborts the superproject status, so instead
            # canonicalize it into the shadow layout and rewrite the gitfile to
            # match (the live tree is a writable mount). Key it by the submodule
            # NAME, not the checkout path: git stores .git/modules by name, so the
            # original-gitdir lookup below (orig_modules / rel) must use the name or
            # it looks in the wrong place and skips the reference clone when a
            # submodule's name differs from its path.
            name = _submodule_name(worktree_root, path)
            target = (shadow_modules / name).resolve()
            rel = Path(name)
            # Resolve the worktree (like `target`) before the relpath: a symlinked
            # path component would otherwise make the gitdir pointer span a
            # resolved/unresolved boundary and get the wrong number of `../`
            # segments, re-breaking the very dangling pointer this rewrite fixes.
            # (core.worktree below resolves both sides for the same reason.)
            _write_text(
                gitfile,
                f"gitdir: {os.path.relpath(str(target), str(worktree.resolve()))}\n")
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            g = ("--git-dir", str(target))
            # Best-effort (check=False), like the superproject's _attach fetch: a
            # single failure here must degrade this submodule to an empty repo
            # (still enough to stop `git status` aborting), not sys.exit and take
            # the whole pre-claude session down over one bad submodule gitdir.
            _run("init", "-q", "--bare", str(target), check=False)
            _run(*g, "config", "core.bare", "false", check=False)
            # Resolve the worktree like `target` before the relpath, so a symlinked
            # path component can't compute the gitdir->worktree link across a
            # resolved/unresolved boundary and point somewhere wrong.
            _run(*g, "config", "core.worktree",
                 os.path.relpath(str(worktree.resolve()), str(target)), check=False)
            orig_mod = orig_modules / rel
            if (orig_mod / "objects").is_dir():
                if _attach(str(target), str(orig_mod)):
                    _run("-C", str(worktree), "reset", "--mixed", "-q", "HEAD",
                         check=False)
            else:
                # No original to borrow from: an empty repo (unborn HEAD) is enough
                # to stop the dangling pointer from aborting status.
                for key, default in _DEFAULT_IDENTITY:
                    _run(*g, "config", key, default, check=False)
        # Recurse: this submodule may itself contain submodules, whose gitdirs live
        # under its own modules dir; without this their pointers stay dangling and
        # `git status` inside this submodule aborts.
        _rebuild_submodules(worktree, target / "modules", orig_modules / rel / "modules")


def _original_commit_tips(original: str) -> "list[str]":
    """Commit tips of the read-only original's branches+tags+HEAD, as an exclusion
    floor for export.

    Read straight from /cvs/git *at export time* (not the host-written
    pending.base), so two failure modes are closed: a commit the user landed on
    the real repo *during* the session — present in the original but absent from
    the pre-session snapshot — is still excluded rather than mis-attributed to the
    agent; and a session that tampered with the jail-writable pending.base can't
    smuggle the whole pre-existing history into the bundle, because /cvs/git is
    read-only and its tips always exclude it. Peeled to commits (an annotated tag
    -> its commit, a non-commit tag skipped) so no non-commit sha reaches
    `bundle --not` and errors the capture.
    """
    r = _run("--git-dir", original, "for-each-ref",
             "--format=%(objecttype) %(objectname) %(*objecttype) %(*objectname)",
             "refs/heads/", "refs/tags/", check=False)
    tips: "list[str]" = []
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[2] == "commit":   # annotated tag -> commit
                tips.append(parts[3])
            elif len(parts) >= 2 and parts[0] == "commit":  # branch / lightweight tag
                tips.append(parts[1])
    # A detached-HEAD original: its HEAD commit isn't under refs/heads, so add it
    # explicitly or a user commit made on that detached HEAD would look like agent
    # work.
    h = _run("--git-dir", original, "rev-parse", "--verify", "-q", "HEAD^{commit}",
             check=False)
    if h.returncode == 0 and h.stdout.strip():
        tips.append(h.stdout.strip())
    return tips


def export(workdir: str, spool: str, original: "str | None" = None) -> int:
    """Bundle the session's new commits (delta vs the exclusion floor) into the spool.

    Returns 0 when a bundle was written, 1 when there was nothing new (an empty
    bundle is a no-op, not an error), 2 on a real failure. The exclusion is the
    union of the host-seeded pending.base and the read-only original's current
    tips (_original_commit_tips) so a concurrent host commit or a tampered
    pending.base can't distort the delta.
    """
    spool_dir = Path(spool)
    # Park a HEAD detached *during* the session (git checkout --detach, a bisect,
    # `git checkout <sha>`) onto a branch, so its commits ride a refs/heads/* ref
    # that `bundle create --all` records and review.import_bundle fetches. A bare
    # detached HEAD is written into the bundle only under the HEAD ref — which the
    # host's heads/tags/replace/notes refspecs never pull — so the work would be
    # bundled yet unreviewable while export still reported success. A branch ref is
    # enough (HEAD need not move), so the live working tree is untouched. (init
    # already handles a HEAD detached *at start*; this covers a mid-session detach.)
    _park_detached_head(workdir, repoint=False)
    # The end-state branch list, written on every completed run (new commits or
    # not) — lstrip=2 (plain branch name, matching review.branch_tips) so the
    # host compares like keys even when a tag shares a name. Written even on the
    # nothing-new path so finalize can still spot a ref-only change (a branch
    # added/deleted with no new commit) that a bundle can't carry. Jail parking
    # branches are dropped: they are artifacts standing in for a detached HEAD, not
    # user branches, so they must not read as a spurious "added <branch>".
    end = _run("-C", workdir, "for-each-ref", "--format=%(refname:lstrip=2)",
               "refs/heads/").stdout
    end = "".join(f"{ln}\n" for ln in end.splitlines()
                  if ln.strip() and not _is_parking_branch(ln.strip()))
    _write_text(spool_dir / "pending.end", end)
    base = spool_dir / "pending.base"
    exclude = ([line.strip() for line in base.read_text().splitlines()
                if line.strip()] if base.is_file() else [])
    if original:
        exclude += _original_commit_tips(original)
    bundle = spool_dir / "pending.bundle"
    # Exclusions go on stdin as `^<sha>` lines, not `--not <sha>...` on argv: a
    # repo with very many refs would otherwise build a command line past ARG_MAX
    # (E2BIG), and that OSError — swallowed by the entrypoint's `export || true` —
    # would silently drop the whole capture. `--stdin` scales without that wall.
    # dict.fromkeys dedups (base and original tips overlap) while keeping a stable
    # order; the string is empty when there's nothing to exclude, so git gets EOF
    # immediately rather than blocking on the real stdin.
    not_stdin = "".join(f"^{sha}\n" for sha in dict.fromkeys(exclude))
    # Decide "nothing new" by asking whether any ref reaches a commit the user
    # doesn't already have, rather than string-matching git's "empty bundle"
    # message (which is localised and could change). Only an exit 0 with empty
    # output means "nothing new": a *non-zero* rev-list (e.g. a base sha that
    # doesn't resolve in the shadow) also prints nothing, and treating that as
    # "nothing new" would silently drop the session's commits — so fall through to
    # bundle create, which surfaces a real failure loudly.
    probe = _run("-C", workdir, "rev-list", "-1", "--all", "--stdin",
                 check=False, stdin=not_stdin)
    # The pending.exported marker (finalize uses it to tell a completed run from a
    # session killed before capture) is written only *here*, after the steps that
    # can abort — not at export entry. A mid-export abort (e.g. the for-each-ref
    # above sys.exit()ing) then leaves no marker, which finalize reports as
    # 'interrupted (may not have captured)', instead of the marker making a real
    # failure look like the benign nothing-new case.
    if probe.returncode == 0 and not probe.stdout.strip():
        bundle.unlink(missing_ok=True)
        (spool_dir / "pending.exported").write_text("")   # completed: nothing new
        return 1
    result = _run("-C", workdir, "bundle", "create", str(bundle), "--all",
                  "--stdin", check=False, stdin=not_stdin)
    if result.returncode != 0 or not bundle.is_file():
        bundle.unlink(missing_ok=True)
        # There is new history (checked above), so any failure here is real
        # (corrupt object, disk full, ...) and would otherwise silently drop the
        # session's commits — make it loud (2) and record it for finalize(). No
        # pending.exported: finalize checks pending.failed first, so the loud path
        # wins regardless.
        detail = result.stderr.strip()
        _eprint("jail-workspace: git bundle create failed: " + detail)
        _write_text(spool_dir / "pending.failed", detail + "\n")
        return 2
    (spool_dir / "pending.exported").write_text("")       # completed: bundle written
    return 0


def main(argv: "list[str]") -> int:
    if len(argv) != 1 or argv[0] not in ("init", "export"):
        sys.exit("usage: jail-workspace {init|export}")
    workdir = os.environ.get("JAIL_WORKDIR")
    if not workdir:
        sys.exit("jail-workspace: JAIL_WORKDIR is not set")
    if argv[0] == "init":
        original = os.environ.get("JAIL_GIT_ORIGINAL")
        if not original:
            sys.exit("jail-workspace: JAIL_GIT_ORIGINAL is not set")
        init(workdir, original)
        return 0
    spool = os.environ.get("JAIL_REVIEW_SPOOL")
    if not spool:
        sys.exit("jail-workspace: JAIL_REVIEW_SPOOL is not set")
    # The read-only original (/cvs/git) is an exclusion floor the session can't
    # tamper with; optional so a caller without it still exports off pending.base.
    original = os.environ.get("JAIL_GIT_ORIGINAL")
    return export(workdir, spool, original)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
