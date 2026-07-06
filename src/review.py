"""Review the work a jailed session produced before it lands in the real repo.

A jail run that isolates the agent (see build_mounts) hands its work back as a
git bundle rather than scribbling on the user's working tree. This module turns
that bundle into a PR-style review: every ref it carries is fetched only under a
quarantine prefix, classified (purely additive vs. history-rewriting), and walked
through a three-state machine whose state *is* the ref namespace:

    refs/jail-review/unreviewed/<bundle>/...   imported, not yet looked at
    refs/jail-review/accepted/<bundle>/...      reviewed and accepted
    refs/heads/...                              applied to the user's branches

Acceptance is decoupled from merge: `accept` only moves refs between the two
quarantine states, and `apply` is the single step that ever touches a real
branch. Everything here runs on the host, where the repo's .git is writable
(inside the jail it is read-only), and shells out to git via _git(); user errors
go through jail_config.die().
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import resolve_claude_dir_base
import resolve_user
from jail_config import config_dir, die, read_config, user_config_dir

# The quarantine namespace. A bundle's refs never touch a live ref until apply;
# until then they live under <prefix>/<state>/<bundle-name>/<kind>/<ref>.
REF_PREFIX = "refs/jail-review"
# The ref classes a bundle can carry, each fetched into its own sub-namespace so
# a hostile bundle can't, say, smuggle a replace ref into the live refs/replace.
# Each class's live source ref is `refs/<kind>` (derived, see import_bundle).
REF_KINDS = ("heads", "tags", "replace", "notes")

# The host-only command surface, dispatched before docker is even required (the
# launcher routes these like `prune`). review is read-only; the rest mutate refs.
COMMANDS = ("review", "accept", "reject", "apply")

# The in-jail export stands up a synthetic branch (jail_workspace._DETACHED_BRANCH,
# with a `-N` suffix on a name clash) to carry a detached-HEAD commit into the
# bundle, and filters that name out of the session's end-state branch list
# (pending.end). The name is duplicated here rather than imported — jail_workspace
# is a standalone container script, like relay/forward — so the host can drop the
# same reserved names from the *base* branch set: otherwise a user branch that
# happens to share the reserved name is present in the base but filtered from
# pending.end, and would be mis-reported as deleted during the session.
_DETACHED_BRANCH = "jail-detached-head"


def _is_parking_branch(name: str) -> bool:
    """True for a jail detached-HEAD parking branch (`jail-detached-head`[-N]).

    Kept in sync with jail_workspace._is_parking_branch (see _DETACHED_BRANCH).
    """
    return bool(re.fullmatch(re.escape(_DETACHED_BRANCH) + r"(-\d+)?", name))


# git environment variables that redirect *where* git operates (GIT_DIR points
# the command at another repo, GIT_WORK_TREE at another tree, ...). Scrubbed from
# the child env so an inherited value — set by a surrounding git hook, a
# `rebase --exec`, direnv, etc. — can't override our explicit `-C <repo>` and make
# a ref edit or a reset land in the wrong .git.
_GIT_ENV_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_OBJECT_DIRECTORY",
                      "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_INDEX_FILE",
                      "GIT_COMMON_DIR", "GIT_NAMESPACE")


def _git(repo: str, *args: str, check: bool = True,
         stdin: "str | None" = None) -> "subprocess.CompletedProcess":
    """Run git in `repo`, capturing output; die() on failure unless check=False.

    Output is decoded with surrogateescape so untrusted diff/log bytes that are
    not valid UTF-8 round-trip instead of raising UnicodeDecodeError (they are
    re-encoded losslessly on the way out — see _page); the GIT_* redirection vars
    are dropped so the explicit `-C <repo>` always wins.
    """
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_REDIRECTS}
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, errors="surrogateescape",
        input=stdin, env=env,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        # git stderr can echo untrusted refname/object bytes (a bundle ref may
        # legally carry C1/control bytes), and die() writes its message straight
        # to the terminal — so strip terminal escapes here, matching the _sanitize
        # boundary every other output path already goes through.
        die(f"git {' '.join(args)} failed: {_sanitize(detail)}")
    return result


def _git_ok(repo: str, *args: str) -> bool:
    """True when git exits 0; for predicate commands like rev-parse/--is-ancestor."""
    return _git(repo, *args, check=False).returncode == 0


def require_repo(repo: str) -> None:
    """die() unless `repo` is inside a git working tree."""
    if not _git_ok(repo, "rev-parse", "--git-dir"):
        die(f"not a git repository: {repo}")


# ---------------------------------------------------------------------------
# Bundle naming: <session-name>-<sha1-of-bundle-bytes>
# ---------------------------------------------------------------------------

def _slug(name: str) -> str:
    """Reduce a session name to a ref/path-safe token.

    Like jail_identity()'s slug in spirit, but deliberately keeps dot/underscore/
    dash (a session id may carry them) while collapsing everything else; the
    result still survives as a single ref path component and a filename.
    """
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    # Collapse runs of dots: a literal ".." is rejected as a ref path component,
    # so a session id carrying one would otherwise make the whole import refspec
    # invalid and silently drop the capture.
    s = re.sub(r"\.{2,}", ".", s).strip("-._")
    return s or "session"


def bundle_name(session_name: "str | None", bundle_path: str) -> str:
    """Name a bundle `<session>-<sha1>`: the slugged session plus a content hash.

    The sha1 is over the bundle's own bytes, so two sessions that share a name
    (or a re-export of the same work) get distinct, stable, content-addressed
    names — and the name doubles as an integrity check on the artifact.
    """
    h = hashlib.sha1()
    with open(bundle_path, "rb") as f:   # stream: a session bundle can be large
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    base = _slug(session_name or "")   # _slug("") already falls back to "session"
    return f"{base}-{h.hexdigest()}"


# ---------------------------------------------------------------------------
# Ref-namespace helpers
# ---------------------------------------------------------------------------

def _state_refs(repo: str, state: str,
                name: "str | None" = None) -> "list[tuple[str, str]]":
    """(refname, sha) for every quarantined ref in `state`, optionally one bundle."""
    pattern = f"{REF_PREFIX}/{state}/"
    if name is not None:
        pattern += f"{name}/"
    out = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)",
               pattern).stdout
    refs = []
    for line in out.splitlines():
        refname, _, sha = line.partition(" ")
        if refname:
            refs.append((refname, sha))
    return refs


def _parse_ref(refname: str, state: str) -> "tuple[str, str, str]":
    """Split a quarantine ref into (bundle-name, kind, ref-rest).

    The bundle name is a single slugged component (no slashes), so the first
    segment after the state is the name, the next is the kind (heads/tags/...),
    and whatever remains is the original ref name — which for a branch like
    `feature/x` keeps its own slashes intact.
    """
    rest = refname[len(f"{REF_PREFIX}/{state}/"):]
    name, _, tail = rest.partition("/")
    kind, _, ref_rest = tail.partition("/")
    return name, kind, ref_rest


def bundle_list(repo: str, state: str) -> "list[str]":
    """The distinct bundle names present in `state`, in first-seen order."""
    seen: "list[str]" = []
    for refname, _ in _state_refs(repo, state):
        name, _, _ = _parse_ref(refname, state)
        if name and name not in seen:
            seen.append(name)
    return seen


def resolve_bundle(repo: str, state: str, partial: str) -> str:
    """Resolve a user-typed name (or unique prefix) to a full bundle name, or die()."""
    names = bundle_list(repo, state)
    if partial in names:
        return partial
    matches = [n for n in names if n.startswith(partial)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        die(f"no {state} bundle matching {partial!r}"
            + (f"; have: {', '.join(names)}" if names else ""))
    die(f"{partial!r} is ambiguous in {state}: {', '.join(matches)}")


# ---------------------------------------------------------------------------
# Import: fetch a bundle's refs into the unreviewed quarantine
# ---------------------------------------------------------------------------

def import_bundle(repo: str, bundle_path: str, name: str,
                  fatal: bool = True) -> bool:
    """Verify a bundle and fetch all its refs under unreviewed/<name>/.

    `bundle verify` first proves the repo already holds the bundle's prerequisite
    objects (it should: they are the snapshot the jail cloned from), so the import
    can't leave a partial graph. The glob refspecs land every ref class in its own
    quarantine sub-namespace; a class the bundle lacks simply matches nothing.

    Returns True on success. With `fatal=False` a bad or incomplete bundle returns
    False instead of die()ing — used on the teardown path, where a missing
    prerequisite must not abort the launcher mid-cleanup.
    """
    if _git(repo, "bundle", "verify", bundle_path, check=fatal).returncode != 0:
        return False
    refspecs = [f"refs/{kind}/*:{REF_PREFIX}/unreviewed/{name}/{kind}/*"
                for kind in REF_KINDS]
    # The bundle is authored by the sandboxed (untrusted) agent, so the fetch is
    # hardened three ways:
    #   --no-tags               without it `git fetch` also auto-follows the
    #                           bundle's tags straight into the live refs/tags/,
    #                           escaping the quarantine; the explicit refspec above
    #                           is then the only route a tag may take.
    #   --no-recurse-submodules the bundle's commits can carry a hostile
    #                           .gitmodules; on-demand recursion would drive
    #                           submodule transport (SSRF, or RCE via ext::) at
    #                           import time. We never want import to touch the
    #                           network or submodules.
    #   -c fetch.fsckObjects    reject malformed/oversized objects rather than
    #                           indexing them straight into the real object store.
    ok = _git(repo, "-c", "fetch.fsckObjects=true", "fetch", "--no-tags",
              "--no-recurse-submodules", bundle_path, *refspecs,
              check=fatal).returncode == 0
    if not ok:
        # A multi-refspec fetch can create some refs before failing; drop the whole
        # namespace so a failed import never leaves a half-populated quarantine
        # that no review command would surface or be able to clean up. Non-fatal:
        # this also runs on the fatal=False teardown path.
        _delete_state_refs(repo, name, fatal=False)
    return ok


# ---------------------------------------------------------------------------
# Spool handshake: seed a review run, then adopt the bundle it dropped
# ---------------------------------------------------------------------------

def branch_tips(repo: str) -> "dict[str, str]":
    """{branch-name: sha} for every local branch in `repo`.

    Keyed by %(refname:lstrip=2) — the ref name minus refs/heads/ — not
    %(refname:short), which disambiguates to "heads/<name>" when a tag shares the
    branch's name. The plain name is what _parse_ref yields from the quarantine
    refspec, so analyze()'s bases.get(branch) must match it. Thin wrapper over
    _refs_snapshot (which keys its branch map identically) so the two can't drift.
    """
    return _refs_snapshot(repo)[0]


def _refs_snapshot(repo: str) -> "tuple[dict[str, str], set]":
    """One for-each-ref pass over branches + tags: (branch_tips, commit_set).

    branch_tips is {branch-name: sha} keyed like branch_tips()/lstrip=2; commit_set
    is the peeled commit of every branch and tag plus HEAD — the delta-exclusion
    existing_commits returns. Computing both here lets prepare() fork git once
    instead of twice on the launch path.

    Scoped to refs/heads + refs/tags (+ HEAD): refs/remotes, refs/stash, and a
    prior run's refs/jail-review/* are NOT commits "the user already has" and must
    not widen the exclusion (a stale quarantine tip would otherwise mask a later
    session's byte-identical work as 'nothing new').
    """
    out = _git(repo, "for-each-ref",
               "--format=%(refname) %(objecttype) %(objectname) "
               "%(*objecttype) %(*objectname)",
               "refs/heads/", "refs/tags/").stdout
    tips: "dict[str, str]" = {}
    commits = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        refname, otype, oname = parts[0], parts[1], parts[2]
        # Filter to commit objects: a tag can point at a tree/blob, whose sha fed
        # to `rev-list/bundle --not` would error and fail the capture. Peel an
        # annotated tag to its commit; skip a non-commit lightweight tag.
        if len(parts) >= 5 and parts[3] == "commit":   # annotated tag -> commit
            commits.add(parts[4])
        elif otype == "commit":                        # branch / lightweight tag
            commits.add(oname)
        if refname.startswith("refs/heads/"):
            tips[refname[len("refs/heads/"):]] = oname
    head = head_sha(repo, "HEAD")  # peels HEAD with ^{commit}; None if unborn
    if head:
        commits.add(head)
    return tips, commits


def existing_commits(repo: str) -> "set":
    """Every commit the user already has at session start, for the delta exclusion.

    The peeled commit of every branch and tag plus HEAD. The in-container
    `bundle create --all` would otherwise re-capture a pre-existing tag's commit,
    or a detached-HEAD commit (which the new jail-detached-head branch makes
    reachable), as if it were the session's new work. Branch tips alone (the
    manifest base) don't cover those, so the exclusion set is broader.
    """
    return _refs_snapshot(repo)[1]


def prepare(ctx: "dict") -> None:
    """Seed the spool before a review run and reset the throwaway gitdir.

    Captures the real repo's branch tips for finalize()'s manifest (ctx["base"],
    used to classify divergence) and writes the broader delta-exclusion set to
    pending.base, so the in-container export bundles only genuinely new history.
    Both come from a single for-each-ref pass (_refs_snapshot). Wipes and
    recreates the gitdir so the session always starts from the *current* real
    .git: a fresh derivation each run is what stops the shadow .git from drifting
    against the host.
    """
    spool = Path(ctx["spool"])
    spool.mkdir(parents=True, exist_ok=True)
    gitdir = Path(ctx["gitdir"])
    shutil.rmtree(gitdir, ignore_errors=True)
    # rmtree refuses to recurse a symlink, so a prior run that swapped `gitdir`
    # for one (the spool is jail-writable) survives it. Unlink the symlink itself,
    # mirroring the handshake-file loop below: exists() follows the link, so a
    # *dangling* symlink would read as absent, slip past the guard, and then make
    # the mkdir raise FileExistsError against the leftover symlink name.
    if gitdir.is_symlink():
        gitdir.unlink()
    if gitdir.exists():
        # rmtree swallowed a failure (e.g. files a prior run wrote as a different
        # container uid the host user can't remove): fail loudly instead of
        # reusing a stale gitdir or crashing with a bare FileExistsError below.
        die(f"could not reset the review gitdir at {gitdir}; remove it and retry")
    gitdir.mkdir(parents=True)
    # Clear any handshake files a crashed earlier run reusing this (pid-keyed)
    # spool left behind, so finalize() can neither adopt a stale pending.bundle
    # nor read a stale pending.exported marker as this run's outcome. pending.base
    # is unlinked too — not just to clear stale data but because the write below
    # follows a symlink: a hostile session that leaked its spool (a teardown
    # rmtree that couldn't remove a container-uid gitdir) could have swapped
    # pending.base for a symlink to an arbitrary host file; unlinking first
    # neutralises it so write_text can't be redirected through it.
    for leftover in ("pending.bundle", "pending.base", "pending.end",
                     "pending.exported", "pending.failed"):
        (spool / leftover).unlink(missing_ok=True)
    ctx["base"], commits = _refs_snapshot(ctx["repo"])
    (spool / "pending.base").write_text(
        "".join(f"{sha}\n" for sha in sorted(commits)))


def session_name(store: "str | Path", container_workdir: str) -> "str | None":
    """Best-effort Claude session id for a workdir, read from the per-user store.

    Mirrors the entrypoint's project-slug rule (the *container* workdir with every
    non-alphanumeric character replaced by '-') and returns the most recent session file
    stem under it, or None. The bundle name falls back to "session" when None, so
    a run is always nameable even if this can't pin a session.

    The store's projects/ dir is bind-mounted read-write into the jail, so the
    session id this returns is only an *advisory label*: the agent can drop a
    chosen-name, future-mtime .jsonl to influence the bundle-name prefix. _slug
    blocks path traversal/injection, and the name's trailing content sha1
    (bundle_name) plus the untrusted-agent warning at `accept` carry the actual
    integrity — the prefix is never trusted, only the human-read diff and the hash.
    """
    slug = re.sub(r"[^A-Za-z0-9]", "-", container_workdir)
    project = Path(store) / "projects" / slug
    if not project.is_dir():
        return None

    def _mtime(p: "Path") -> float:
        # glob-then-stat can race a session file being removed between the two
        # (this runs on teardown, after the container is gone); a vanished file
        # sorts last rather than raising and aborting the capture.
        try:
            return p.stat().st_mtime
        except OSError:
            return -1.0
    sessions = sorted(project.glob("*.jsonl"), key=_mtime, reverse=True)
    return sessions[0].stem if sessions else None


def _plain_file(p: "Path") -> bool:
    """True when `p` is a regular file and NOT a symlink.

    The per-run spool is bind-mounted read-write into the jail, so a hostile
    session could swap a handshake file for a symlink to make the host follow it
    to an arbitrary target. Every spool marker the host reads goes through this,
    so a symlink is treated as 'absent' rather than followed.
    """
    return p.is_file() and not p.is_symlink()


def _warn_masked(repo: str, name: str, masked: "tuple | list") -> None:
    """Warn if the bundle's new commits touch a path that was masked in the jail.

    A `hidden` path (the auto-masked .claude-jail.json, or a user `hidden` entry)
    reads as 0 bytes inside the jail. Review mode is the first time the agent can
    commit, so a blanket `git add -A` can capture that empty file; applying the
    bundle would then overwrite the user's real file with nothing. apply still
    shows it in the diff, but flag it loudly here so a clobber isn't missed.
    """
    if not masked:
        return
    heads = [sha for refname, sha in _state_refs(repo, "unreviewed", name)
             if _parse_ref(refname, "unreviewed")[1] == "heads"]
    changed: "set[str]" = set()
    if heads:
        # One log walk over every head tip at once (not one fork per branch, on the
        # teardown path): files touched by commits unique to this bundle, i.e.
        # reachable from any head tip but not from any live branch/tag.
        # -c core.quotePath=false: without it git double-quotes and octal-escapes
        # any non-ASCII path (`"caf\303\251.txt"`), which would never string-match
        # the raw rels in `masked` — silently skipping the clobber warning for
        # exactly the non-ASCII `hidden` files it exists to flag. The rels round-
        # trip via _git's surrogateescape decode.
        out = _git(repo, "-c", "core.quotePath=false", "log", "--name-only",
                   "--pretty=format:", *heads, *_NOT_LIVE, check=False).stdout
        changed.update(p for p in out.splitlines() if p.strip())
    hit = sorted(set(masked) & changed)
    if hit:
        print("Review: WARNING — captured commits modify path(s) masked in the "
              "jail (committed as empty); applying would overwrite your real "
              f"file(s): {', '.join(hit)}", file=sys.stderr)


def _warn_ref_only_changes(spool: "Path", base_branches: "dict") -> None:
    """Warn about branches added/deleted with no new commit (a ref-only change).

    A bundle only carries new *commits*, so a branch created at an already-known
    commit — or deleted — produces no bundle and would otherwise vanish silently.
    Compare the session's end-state branch names (pending.end) against the base
    the host recorded and name the difference so the user can act on it.
    """
    end_file = spool / "pending.end"
    if not _plain_file(end_file):
        return
    # surrogateescape: pending.end is written container-side from raw git output,
    # so a branch name with non-UTF-8 bytes must round-trip rather than crash the
    # strict-UTF-8 default read.
    end_names = {ln.strip() for ln
                 in end_file.read_text(errors="surrogateescape").splitlines()
                 if ln.strip()}
    # Drop jail parking branches from both sides: export filters them out of
    # pending.end, so a real branch sharing the reserved name would otherwise read
    # as a spurious deletion (and a jail-created one as a spurious addition).
    base_names = {b for b in (base_branches or {}) if not _is_parking_branch(b)}
    end_names = {b for b in end_names if not _is_parking_branch(b)}
    added = sorted(end_names - base_names)
    deleted = sorted(base_names - end_names)
    notes = ([f"added {', '.join(added)}"] if added else []) \
        + ([f"deleted {', '.join(deleted)}"] if deleted else [])
    if notes:
        print("Review: the session changed branches without adding commits ("
              + _sanitize("; ".join(notes)) + "); ref-only changes are not "
              "captured as a reviewable bundle — re-create them by hand if wanted.",
              file=sys.stderr)


def finalize(real_repo: str, spool: "str | Path", base_branches: "dict",
             session: "str | None", masked: "tuple | list" = ()) -> "str | None":
    """Adopt the bundle a finished container dropped in the spool, on the host.

    The container writes pending.bundle (+ pending.end) into the per-run spool
    (`.../review/<jail_id>`, which is also bound into the jail). The named
    artifacts, though, land one level up in the shared `.../review` dir — exactly
    where `dispatch` reads them, and *outside* the dir bound into the next
    session, so review/reject can find them and a later jailed agent can't reach a
    prior run's bundles. Here on the host we name it `<session>-<sha1>`, fold the
    setup base and end-state into its manifest, import it into the unreviewed
    quarantine, and clear the pending files. `masked` lists jail-masked rels to
    warn about (see _warn_masked). Returns the bundle name, or None when the
    session produced nothing (or its bundle could not be imported).
    """
    spool = Path(spool)
    pending = spool / "pending.bundle"
    # The per-run spool is bind-mounted read-write into the jail, so a hostile
    # session could swap a handshake file for a symlink to make the host read or
    # import an arbitrary path. These are plain files the in-jail export writes;
    # refuse to follow a symlink rather than act on its target.
    if pending.is_symlink():
        print("Review: pending.bundle is a symlink (unexpected); refusing to "
              "follow it.", file=sys.stderr)
        return None
    if not pending.is_file():
        # No bundle. Three distinct cases, each with its own signal:
        #   pending.failed  — export ran and *failed* (bundle create errored):
        #                     work was made but NOT captured — say so loudly.
        #   no pending.exported — export never completed (session killed before
        #                     capture): work may be uncaptured — warn.
        #   pending.exported only — export ran and found nothing new: quiet, but a
        #                     ref-only change (branch created at an existing commit
        #                     or deleted) can't ride a bundle, so surface that.
        failed = spool / "pending.failed"
        if _plain_file(failed):
            # surrogateescape: the container writes git's (possibly non-UTF-8)
            # stderr here, so read it losslessly rather than crash the strict read.
            detail = _sanitize(failed.read_text(errors="surrogateescape").strip())
            print("Review: WARNING — the in-jail bundle export FAILED"
                  + (f": {detail}" if detail else "")
                  + "; the session's commits were NOT captured for review.",
                  file=sys.stderr)
        elif not _plain_file(spool / "pending.exported"):
            print("Review: the in-jail export did not run to completion (session "
                  "interrupted before capture?); any commits the session made may "
                  "not have been captured for review.", file=sys.stderr)
        else:
            _warn_ref_only_changes(spool, base_branches)
        return None
    store = spool.parent  # the host-only review store dispatch reads from (a
                          # sibling of the .claude bind, not inside it — see
                          # _spool); prepare() already created it via the spool.
    name = bundle_name(session, str(pending))

    def _clear_pending() -> None:
        for leftover in ("pending.base", "pending.end", "pending.exported",
                         "pending.failed"):
            (spool / leftover).unlink(missing_ok=True)

    # Import first, from the pending artifact, and only publish the named bundle +
    # manifest once the refs are safely quarantined. Teardown runs after the
    # container is gone, so a bad bundle must not die() the launcher mid-cleanup
    # (fatal=False); reordering import ahead of the rename also means a failed
    # import never strands an orphaned manifest/bundle that no review command lists.
    if not import_bundle(real_repo, str(pending), name, fatal=False):
        failed = store / f"{name}.bundle.failed"
        pending.replace(failed)
        _clear_pending()
        print(f"Review: bundle {name!r} could not be imported (missing "
              f"prerequisites?); kept at {failed} for manual inspection.",
              file=sys.stderr)
        return None
    pending.replace(store / f"{name}.bundle")
    _warn_masked(real_repo, name, masked)
    end_file = spool / "pending.end"
    # pending.end is written by the in-jail export, so end_heads is session-supplied
    # and the deletion hazard analyze() derives from it is advisory, not verified
    # (apply never deletes a real branch, so a forged value can only mislead, not
    # destroy). The base branches come from prepare() on the host and are trusted.
    # A *missing* pending.end (export killed between writing the bundle and the
    # branch list) is None — "no end-state data, skip the deletion check" — not []
    # ("the session ended with zero branches"), which would flag every base branch
    # as deleted. A symlink (the rw spool is jail-writable) is treated as missing.
    end_heads = (sorted(line.strip() for line in
                        end_file.read_text(errors="surrogateescape").splitlines()
                        if line.strip())
                 if _plain_file(end_file) else None)
    manifest = {"branches": dict(base_branches), "end_heads": end_heads}
    (store / f"{name}.manifest.json").write_text(json.dumps(manifest, indent=2)
                                                 + "\n")
    _clear_pending()
    return name


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _is_ancestor(repo: str, a: str, b: str) -> bool:
    """True when commit `a` is an ancestor of `b` (or equal) — git's ff test."""
    return _git_ok(repo, "merge-base", "--is-ancestor", a, b)


def _rev_parse(repo: str, ref: str, commit: bool = True) -> "str | None":
    """Resolve `ref` to an object name, or None when it doesn't resolve.

    `commit` peels to the commit (`^{commit}`); without it the ref's own object is
    kept, so a tag re-point is compared by the identity for-each-ref reports rather
    than the tagged commit.
    """
    spec = f"{ref}^{{commit}}" if commit else ref
    r = _git(repo, "rev-parse", "--verify", "--quiet", spec, check=False)
    return r.stdout.strip() or None


def head_sha(repo: str, ref: str) -> "str | None":
    """The commit a ref resolves to, or None when it doesn't exist."""
    return _rev_parse(repo, ref, commit=True)


def _ref_target(repo: str, ref: str) -> "str | None":
    """The object a ref points at *directly* (no commit deref), or None."""
    return _rev_parse(repo, ref, commit=False)


def count(repo: str, *revs: str) -> int:
    """`git rev-list --count <revs...>` as an int (0 on an empty/invalid range)."""
    r = _git(repo, "rev-list", "--count", *revs, check=False)
    return int(r.stdout.strip() or 0) if r.returncode == 0 else 0


def _ahead_behind(repo: str, left: str, right: str) -> "tuple[int, int] | None":
    """(left-only, right-only) commit counts via one `rev-list --left-right`, or
    None when the range can't be computed.

    `left-only` is commits reachable from `left` but not `right`; `right-only` the
    reverse — so for (current, incoming) these are (dropped, added). One git fork
    replaces both `rev-list --count` ranges *and* both `--is-ancestor` probes
    classify() used: an ancestor relation is exactly one side being 0. Returns
    None (not (0, 0)) on an errored/malformed range: (0, 0) is a legitimate
    result (two equal tips) that classify() reads as a fast-forward, so an error
    must stay distinguishable rather than masquerade as one.
    """
    r = _git(repo, "rev-list", "--count", "--left-right", f"{left}...{right}",
             check=False)
    parts = r.stdout.split()
    if r.returncode != 0 or len(parts) != 2:
        return None
    return int(parts[0]), int(parts[1])


# rev-list/log selectors for "commits not already on any real branch or tag";
# the quarantine refs live outside refs/heads|tags, so they are excluded too.
_NOT_LIVE = ("--not", "--branches", "--tags")


def classify(repo: str, branch: str, b_sha: str,
             base_sha: "str | None" = None) -> "dict":
    """Classify what landing `b_sha` would do to refs/heads/<branch>.

    `base_sha` is the snapshot the jail cloned from (the setup manifest, added in
    phase 2). With it we can tell a true rewrite from a benign concurrent
    divergence — the user advancing their own branch while the jail ran. Without
    it we fall back conservatively: any non-fast-forward update is reported as a
    rewrite, since we can't prove the dropped commits weren't the user's.

    Returns a dict: class, added/dropped counts, and the current/incoming shas.
    """
    u = head_sha(repo, f"refs/heads/{branch}")
    info = {"branch": branch, "incoming": b_sha, "current": u,
            "added": 0, "dropped": 0}
    if u is None:
        info["class"] = "new"
        info["added"] = count(repo, b_sha, *_NOT_LIVE)
        return info
    if u == b_sha:
        info["class"] = "unchanged"
        return info
    # One rev-list yields both side counts; the ff/behind tests are exactly these
    # counts (u is an ancestor of b_sha iff nothing is dropped; b_sha an ancestor
    # of u iff nothing is added), so no separate --is-ancestor fork is needed.
    ab = _ahead_behind(repo, u, b_sha)
    if ab is None:
        # The relationship couldn't be computed (unresolvable rev / bad range):
        # classify conservatively as a rewrite so apply is gated behind
        # --allow-rewrite rather than sliding through as a benign fast-forward.
        info["class"] = "rewrite"
        return info
    info["dropped"], info["added"] = ab
    if info["dropped"] == 0:
        info["class"] = "fast-forward"
        return info
    if info["added"] == 0:
        # The incoming tip is already contained in what the user has — stale work.
        info["class"] = "behind"
        return info
    # Genuine divergence: each side has commits the other lacks.
    if base_sha and base_sha != u and _is_ancestor(repo, base_sha, u) \
            and _is_ancestor(repo, base_sha, b_sha):
        # Both advanced from the cloned base: the user moved on independently.
        info["class"] = "divergent"
    else:
        info["class"] = "rewrite"
    return info


# ---------------------------------------------------------------------------
# Analysis of a whole bundle (branches + hazards)
# ---------------------------------------------------------------------------

def analyze(repo: str, state: str, name: str,
            manifest: "dict | None" = None,
            refs: "list[tuple[str, str]] | None" = None) -> "dict":
    """Classify every branch in a quarantined bundle and collect ref hazards.

    Hazards are the ways a bundle rewrites history *without* moving a branch tip,
    which a tip-by-tip classification would miss: a replace ref (silently
    substitutes one object for another), a tag re-pointed at a different object
    (tags are meant to be immutable), or notes changes. Branch deletions can't be
    expressed by a bundle at all and need the setup manifest (phase 2).

    The deletion hazard is deliberately surfaced even though its evidence
    (manifest end_heads) is session-reported and therefore not trustworthy — a
    hostile session could forge end_heads to hide it. It is kept as an *advisory*
    hazard (labelled "session-reported") rather than dropped, because the cost of
    a forged value is only a missed warning: apply never deletes a real branch, so
    nothing destructive rides on it. The base branches it compares against come
    from prepare() on the host and are trusted.
    """
    bases = (manifest or {}).get("branches", {})
    branches = []
    hazards: "list[str]" = []
    # `refs` lets a caller that already scanned the namespace (review_overview)
    # pass this bundle's refs in, so analyze() doesn't re-run for-each-ref per
    # bundle; default None means fetch them here.
    if refs is None:
        refs = _state_refs(repo, state, name)
    for refname, sha in refs:
        _, kind, ref_rest = _parse_ref(refname, state)
        if kind == "heads":
            branches.append(classify(repo, ref_rest, sha, bases.get(ref_rest)))
        elif kind == "replace":
            hazards.append(f"replace ref for {ref_rest}: rewrites history as "
                           f"viewed, without moving any branch")
        elif kind == "tags":
            existing = _ref_target(repo, f"refs/tags/{ref_rest}")
            if existing is not None and existing != sha:
                hazards.append(f"tag {ref_rest} re-pointed "
                               f"({existing[:12]} -> {sha[:12]}); tags should be "
                               f"immutable")
        elif kind == "notes":
            hazards.append(f"notes update under {ref_rest}")
    # A branch present at setup but gone from the session's end state was deleted
    # — a bundle can't carry that fact, so it surfaces only via the manifest's
    # end_heads. That list is session-reported (the in-jail export writes it), so
    # this hazard is advisory; it can only over- or under-report, never cause a
    # deletion, since apply never removes a real branch.
    end_heads = (manifest or {}).get("end_heads")
    if end_heads is not None:
        end_set = set(end_heads)
        for b in sorted(bases):
            # Skip jail parking branches: export filters them out of end_heads, so
            # a real branch sharing the reserved name would read as a false deletion.
            if b not in end_set and not _is_parking_branch(b):
                hazards.append(f"branch {b} deleted during the session "
                               f"(session-reported)")
    branches.sort(key=lambda b: b["branch"])
    return {"name": name, "branches": branches, "hazards": hazards}


def _has_rewrite(report: "dict") -> bool:
    """True when any branch rewrites history or the bundle carries a hazard."""
    return bool(report["hazards"]) or any(
        b["class"] == "rewrite" for b in report["branches"])


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

_LABEL = {
    "new": "new branch", "fast-forward": "fast-forward", "unchanged": "unchanged",
    "behind": "behind (already have it)", "divergent": "divergent (needs merge)",
    "rewrite": "REWRITE",
}


def summary_line(b: "dict") -> str:
    """One-line `<branch>: <class> (+a -d)` summary for a branch."""
    tail = ""
    if b["added"] or b["dropped"]:
        tail = f" (+{b['added']} -{b['dropped']})"
    # Flag the synthetic parking branch: it isn't a branch the user made but a
    # jail stand-in carrying commits that were on a detached HEAD, so applying it
    # creates a `jail-detached-head` branch in the real repo. Naming that here
    # keeps it from reading as a mysterious "new branch" at the review gate.
    if _is_parking_branch(b["branch"]):
        tail += "  [jail parking branch — detached-HEAD work; apply creates it]"
    return f"  {b['branch']}: {_LABEL[b['class']]}{tail}"


def review_overview(repo: str, manifest_of: "callable") -> str:
    """List every unreviewed bundle with a per-branch classification summary."""
    # One scan of the unreviewed namespace, grouped by bundle (first-seen order),
    # so analyze() doesn't re-run for-each-ref once per bundle.
    by_name: "dict[str, list[tuple[str, str]]]" = {}
    for refname, sha in _state_refs(repo, "unreviewed"):
        bname, _, _ = _parse_ref(refname, "unreviewed")
        if bname:
            by_name.setdefault(bname, []).append((refname, sha))
    if not by_name:
        return "No unreviewed bundles."
    blocks = []
    for name, refs in by_name.items():
        report = analyze(repo, "unreviewed", name, manifest_of(name), refs=refs)
        lines = [name + ("  [!] rewrites history" if _has_rewrite(report) else "")]
        lines += [summary_line(b) for b in report["branches"]]
        lines += [f"  [!] {h}" for h in report["hazards"]]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# Git's canonical empty tree, so a branch that shares no history with anything
# live still diffs against *something* — every file shows up as an addition.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _fork_point(repo: str, tip: str) -> "str | None":
    """The commit a new branch forked from: the boundary just outside the commits
    in `tip` not yet on any live branch or tag. None for orphan work."""
    out = _git(repo, "rev-list", "--boundary", tip, *_NOT_LIVE, check=False).stdout
    for line in out.splitlines():
        if line.startswith("-"):
            return line[1:]
    return None


def review_detail(repo: str, name: str, manifest: "dict | None") -> str:
    """A PR-style view of one bundle: per-branch commit list, diff, range-diff,
    hazards.

    The diff is a `git diff` of the net change the branch would introduce —
    against the user's current tip for an existing branch, or against the point a
    new branch forked from — so a reviewer reads the code, not just the commit
    subjects. A rewrite additionally gets a range-diff, which shows how the
    rewritten commits differ from the originals.

    Git is always run with --color=never: the diff content is untrusted and git
    does not escape control sequences embedded in it, so no git-emitted colour is
    ever trusted through the pipe. Colour is instead regenerated host-side, from
    the scrubbed text's own structure, at display time (see _page / _colorize).
    """
    tint = "--color=never"
    report = analyze(repo, "unreviewed", name, manifest)
    out = [f"Bundle: {name}", ""]
    for b in report["branches"]:
        out.append(summary_line(b).strip())
        u, inc = b["current"], b["incoming"]
        note = None
        if b["class"] == "new":
            left = _fork_point(repo, inc)
            if left is None and not b["added"]:
                # The tip is already reachable from a live branch/tag — there is
                # nothing new. Diffing against the empty tree would paint the whole
                # repo as additions, contradicting the (+0) summary.
                log_range = None
                note = "  (already contained in your history; nothing new to show)"
            else:
                log_range = [inc, *_NOT_LIVE]
                left = left or _EMPTY_TREE
        elif b["class"] == "divergent":
            # A divergent branch is *merged*, not replaced, so diff from the
            # merge-base (the three-dot `u...inc`): a two-dot `u inc` would paint
            # the user's own concurrent commits as deletions of their work.
            log_range = [f"{u}..{inc}"]
            left = _git(repo, "merge-base", u, inc,
                        check=False).stdout.strip() or u
        elif b["class"] in ("fast-forward", "rewrite"):
            log_range = [f"{u}..{inc}"]
            left = u
        else:
            log_range = left = None
        if log_range is not None:
            out.append(_git(repo, "log", "--oneline", "--no-decorate", tint,
                            *log_range, check=False).stdout.rstrip())
            out.append(_git(repo, "diff", tint, left, inc,
                            check=False).stdout.rstrip())
        if note:
            out.append(note)
        if b["class"] == "rewrite":
            out.append("  range-diff (what the rewrite changed):")
            rd = _git(repo, "range-diff", tint, f"{u}...{inc}", check=False)
            if rd.returncode == 0 and rd.stdout.strip():
                out.append(rd.stdout.rstrip())
            else:
                # range-diff needs a common base; an unrelated/orphan rewrite has
                # none. Show a note rather than git's raw error text.
                out.append("  (range-diff unavailable — no common base with your "
                           "branch; see the diff above)")
        out.append("")
    if report["hazards"]:
        out.append("Hazards:")
        out += [f"  [!] {h}" for h in report["hazards"]]
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Ref transactions
# ---------------------------------------------------------------------------

def _update_refs(repo: str, commands: "list[tuple]", check: bool = True) -> None:
    """Apply a batch of (op, ref, new, old) ref edits in one atomic transaction.

    Uses `update-ref -z --stdin` so the whole move either lands or doesn't, and so
    ref names with awkward characters survive (NUL-delimited fields). `check=False`
    lets a caller on a must-not-die() path (the fatal=False rollback) swallow a
    failure.
    """
    payload = []
    for op, ref, new, old in commands:
        if op == "create":
            payload.append(f"create {ref}\0{new}\0")
        elif op == "delete":
            payload.append(f"delete {ref}\0{old or ''}\0")
        elif op == "update":
            payload.append(f"update {ref}\0{new}\0{old or ''}\0")
        else:
            raise AssertionError(f"unknown ref op {op!r}")
    if payload:
        _git(repo, "update-ref", "-z", "--stdin", stdin="".join(payload),
             check=check)


def _delete_state_refs(repo: str, name: str, state: str = "unreviewed",
                       fatal: bool = True) -> None:
    """Delete every quarantine ref of one bundle in `state` (rollback/cleanup).

    `fatal=False` for the import-rollback path, which runs inside a fatal=False
    teardown finalize() where a delete failure must not die() the launcher.
    """
    refs = _state_refs(repo, state, name)
    _update_refs(repo, [("delete", r, None, sha) for r, sha in refs], check=fatal)


def _require_refs(repo: str, state: str, name: str) -> "list[tuple[str, str]]":
    """The quarantined refs of `name` in `state`, or die() when there are none."""
    refs = _state_refs(repo, state, name)
    if not refs:
        die(f"no {state} bundle {name!r}")
    return refs


def accept(repo: str, name: str, manifest: "dict | None" = None) -> "dict":
    """Move every ref of a bundle from unreviewed to accepted, atomically.

    `manifest` (when known) lets the returned report classify divergence against
    the setup base, so the post-accept summary matches what `review` showed
    instead of conservatively flagging every divergence as a rewrite.
    """
    refs = _require_refs(repo, "unreviewed", name)
    # Pass the refs we just fetched so analyze() doesn't re-run for-each-ref.
    report = analyze(repo, "unreviewed", name, manifest, refs=refs)
    # Bundle names are content hashes, so the same session re-exported (then
    # re-imported) reuses this name. Clear any accepted/<name>/ refs a prior accept
    # left behind (apply didn't land them) up front — in its own transaction, since
    # a single update-ref batch can't both delete and re-create the same ref — so
    # the creates below can't collide and abort the move.
    _delete_state_refs(repo, name, "accepted")
    commands = []
    for refname, sha in refs:
        accepted = refname.replace("/unreviewed/", "/accepted/", 1)
        commands.append(("create", accepted, sha, None))
        commands.append(("delete", refname, None, sha))
    _update_refs(repo, commands)
    return report


def reject(repo: str, name: str, spool: "Path | None" = None) -> None:
    """Discard an unreviewed bundle: delete its refs and remove its artifact."""
    refs = _require_refs(repo, "unreviewed", name)
    _update_refs(repo, [("delete", r, None, sha) for r, sha in refs])
    if spool is not None:
        bundle = spool / f"{name}.bundle"
        bundle.unlink(missing_ok=True)
        (spool / f"{name}.manifest.json").unlink(missing_ok=True)
    else:
        # No resolvable store (no $HOME / claude_dir_base), so the on-disk
        # artifacts can't be located; the refs are gone but say the files may
        # linger rather than leak them silently.
        print(f"Review: {name!r} refs removed, but the review store location could "
              "not be resolved, so its .bundle/.manifest.json (if any) were left "
              "on disk; set claude_dir_base or $HOME and reject again to remove "
              "them.", file=sys.stderr)


def _head_branch(repo: str) -> "str | None":
    """The ref HEAD points at (refs/heads/<x>), or None when detached."""
    r = _git(repo, "symbolic-ref", "--quiet", "HEAD", check=False)
    return r.stdout.strip() or None


def _worktree_clean(repo: str) -> bool:
    """True when the working tree and index have nothing uncommitted."""
    return not _git(repo, "status", "--porcelain").stdout.strip()


def _operation_in_progress(repo: str) -> "str | None":
    """The name of an unfinished git operation in `repo`, or None.

    A merge/cherry-pick/revert/rebase left mid-way would make apply's merge/ff/reset
    run atop a half-finished state (a conflicted merge from a prior divergent apply,
    say); checking only MERGE_HEAD would miss the cherry-pick/revert/rebase cases
    where `reset --hard` could silently discard the in-progress operation.
    """
    gitdir = _git(repo, "rev-parse", "--git-dir").stdout.strip() or ".git"
    base = Path(repo) / gitdir
    for marker, label in (("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"),
                          ("REVERT_HEAD", "revert"), ("rebase-merge", "rebase"),
                          ("rebase-apply", "rebase")):
        if (base / marker).exists():
            return label
    return None


def _drops_new_work(repo: str, info: "dict", base_sha: "str | None") -> bool:
    """True if applying this rewrite would discard commits the user added since the
    session base — work reachable from the current tip but from neither the
    incoming tip nor the cloned base.

    classify() is recomputed at apply time, so its old-value CAS always matches
    the current tip and can't catch a branch the user advanced between `accept`
    and `apply`; the `is_head` path's `reset --hard` has no guard at all. This
    compares against the trusted manifest base instead. Without a base (no
    manifest) we can't distinguish new work from the rewritten history, so return
    False and defer to the existing --allow-rewrite gate. With a base that no
    longer resolves (pruned/gc'd between accept and apply) we can't compute the
    delta either — fail *closed* (return True) so a destructive reset --hard is
    refused rather than running blind on a swallowed error.
    """
    if not base_sha:
        return False
    r = _git(repo, "rev-list", "--count", info["current"], "--not",
             info["incoming"], base_sha, check=False)
    if r.returncode != 0:
        return True
    return int(r.stdout.strip() or 0) > 0


def _backup_replaced(repo: str, name: str, branch: str,
                     old_sha: "str | None") -> "str | None":
    """Save a branch's pre-rewrite tip under the quarantine namespace before an
    apply overwrites it with `reset --hard`.

    That reset has no old-value guard, and _drops_new_work fails open when there's
    no manifest base for this branch — so committed work it discards would survive
    only in the reflog, which a later gc can drop. A `refs/jail-review/replaced/`
    ref keeps those commits reachable and recoverable. Best-effort: a failed
    backup must not abort the apply. Returns a recovery note, or None.
    """
    if not old_sha:
        return None
    backup = f"{REF_PREFIX}/replaced/{name}/{branch}"
    if _git(repo, "update-ref", backup, old_sha, check=False).returncode != 0:
        return None
    return (f"    (previous tip {old_sha[:12]} kept at {backup}; "
            f"`git update-ref -d {backup}` once you're sure)")


def apply_bundle(repo: str, name: str, allow_rewrite: bool = False,
                 manifest: "dict | None" = None) -> "list[str]":
    """Integrate an accepted bundle's branches into the user's real branches.

    Additive branches (new / fast-forward) land straight away; the checked-out
    branch goes through `merge --ff-only` so its working tree follows. A divergent
    branch needs a real merge and only the checked-out one is attempted, leaving
    conflicts for the user. A rewrite is refused without allow_rewrite, and even
    then is never forced over a dirty checked-out branch. Each branch that lands
    has its accepted ref cleared; the rest stay put. Returns human-readable lines.
    """
    refs = _require_refs(repo, "accepted", name)
    # An unfinished merge/cherry-pick/revert/rebase (e.g. a conflicted divergent
    # apply left half-done) would make the merge/ff/reset below run atop a
    # half-finished state — and the dispatch loop would carry it into the next
    # bundle. Refuse until it's resolved.
    busy = _operation_in_progress(repo)
    if busy:
        die(f"the working tree has an unfinished {busy}; resolve it (or abort it) "
            "before applying")
    bases = (manifest or {}).get("branches", {})
    head = _head_branch(repo)
    log: "list[str]" = []
    done: "list[tuple]" = []

    def _cas(refname, target, sha, old, ok_msg, moved_msg):
        """update-ref with an old-value guard, but graceful: a mismatch (the branch
        moved since classify()) logs and keeps the bundle rather than die()ing the
        whole apply mid-loop and leaving partial state. `refname` is passed
        explicitly (not closed over) so the helper is self-contained."""
        if _git(repo, "update-ref", target, sha, old, check=False).returncode == 0:
            log.append(ok_msg)
            done.append(("delete", refname, None, sha))
        else:
            log.append(moved_msg)

    for refname, sha in refs:
        _, kind, ref_rest = _parse_ref(refname, "accepted")
        if kind != "heads":
            # apply only lands branches; clear the reviewed non-branch ref so the
            # bundle resolves instead of lingering in accepted forever. The object
            # is NOT put on any live ref, so a commit reachable only through this
            # tag/note/replace stays only in the retained .bundle — say so, and
            # point at fetching it by hand, rather than implying it landed.
            log.append(f"  {kind}/{ref_rest}: skipped (apply lands only branches; "
                       f"kept only in the retained bundle — fetch it by hand to keep)")
            done.append(("delete", refname, None, sha))
            continue
        branch = ref_rest
        target = f"refs/heads/{branch}"
        is_head = head == target
        info = classify(repo, branch, sha, bases.get(branch))
        cls = info["class"]
        if cls in ("unchanged", "behind"):
            log.append(f"  {branch}: {cls}, nothing to do")
            done.append(("delete", refname, None, sha))
        elif cls == "new":
            if is_head:
                # "new" + is_head means the checked-out branch is unborn (no commit
                # yet): ff-merge so the working tree is populated, not just the ref
                # moved — a bare update-ref would leave every committed file showing
                # as a staged deletion, and a commit would then delete the work.
                if _git(repo, "merge", "--ff-only", sha,
                        check=False).returncode == 0:
                    log.append(f"  {branch}: created")
                    done.append(("delete", refname, None, sha))
                else:
                    log.append(f"  {branch}: create blocked (uncommitted changes?) "
                               f"— resolve and re-apply; kept in accepted")
            else:
                # Empty old-value: create only if the branch still doesn't exist, so
                # a branch that appeared since classify() isn't silently clobbered.
                _cas(refname, target, sha, "", f"  {branch}: created",
                     f"  {branch}: a branch named {branch!r} appeared since review; "
                     f"kept in accepted")
        elif cls == "fast-forward":
            if is_head:
                # Let `merge --ff-only` itself decide: it succeeds with harmless
                # untracked files present and only refuses when uncommitted changes
                # actually block the ff (a plain _worktree_clean check would be too
                # strict). Keep the bundle accepted on refusal rather than die().
                if _git(repo, "merge", "--ff-only", sha,
                        check=False).returncode == 0:
                    log.append(f"  {branch}: fast-forwarded (+{info['added']})")
                    done.append(("delete", refname, None, sha))
                else:
                    log.append(f"  {branch}: fast-forward blocked (uncommitted "
                               f"changes?) — resolve and re-apply; kept in accepted")
            else:
                _cas(refname, target, sha, info["current"],
                     f"  {branch}: fast-forwarded (+{info['added']})",
                     f"  {branch}: moved since review; kept in accepted")
        elif cls == "divergent":
            if not is_head:
                log.append(f"  {branch}: divergent — check it out and merge "
                           f"{refname}; kept in accepted")
            elif _git(repo, "merge", "--no-edit", sha,
                      check=False).returncode == 0:
                # --no-edit: use the default merge message without opening $EDITOR,
                # which would otherwise block an interactive `claude-jail apply` on
                # a tty (git opens the editor for a real merge commit by default).
                log.append(f"  {branch}: merged")
                done.append(("delete", refname, None, sha))
            elif _operation_in_progress(repo) == "merge":
                # The merge started and hit conflicts (MERGE_HEAD present). The tree
                # is now mid-merge; applying the remaining branches on top would
                # compound the mess and the entry guard only runs once — so stop
                # here and leave the rest accepted for a re-apply after resolving.
                log.append(f"  {branch}: merge has conflicts — resolve and commit "
                           f"(or `git merge --abort`); kept in accepted")
                log.append("  (stopping here; resolve the conflict, then re-run "
                           "apply for any remaining branches)")
                break
            else:
                # git refused before starting (uncommitted changes), no merge state.
                log.append(f"  {branch}: merge blocked by uncommitted changes — "
                           f"commit/stash and re-apply; kept in accepted")
        elif cls == "rewrite":
            base = bases.get(branch)
            if allow_rewrite and base is None:
                # No trusted manifest base (the store base was unresolved, or the
                # manifest is missing), so _drops_new_work below can't tell commits
                # the user added since review from the rewritten history and fails
                # open — the reset --hard / force-update proceeds guarded only by
                # --allow-rewrite. Warn so that silent loss of post-accept work
                # isn't a surprise.
                log.append(f"  {branch}: WARNING — no review manifest for this "
                           f"bundle; cannot check for commits you added since "
                           f"review (resolve $HOME / claude_dir_base to re-enable "
                           f"that guard)")
            if not allow_rewrite:
                log.append(f"  {branch}: REWRITE — pass --allow-rewrite to apply; "
                           f"kept in accepted")
            elif _drops_new_work(repo, info, base):
                # The branch advanced since the session base with commit(s) the
                # rewrite doesn't contain — likely work the user added after
                # accepting. Refuse rather than reset --hard / force over it.
                log.append(f"  {branch}: REWRITE would discard commit(s) you added "
                           f"since review — refusing; re-review or move them first; "
                           f"kept in accepted")
            elif is_head and not _worktree_clean(repo):
                log.append(f"  {branch}: REWRITE on dirty checked-out branch — "
                           f"refusing; commit/stash or check out elsewhere")
            elif is_head:
                # Preserve the pre-rewrite tip before the guard-less reset --hard,
                # so any committed work it discards (the fail-open _drops_new_work
                # case: no manifest base for this branch) stays reachable, not just
                # in a gc-able reflog.
                note = _backup_replaced(repo, name, branch, info["current"])
                # check=False + log-and-keep, like every other branch path: a
                # failing reset --hard (an untracked file blocking the overwrite, a
                # stale index.lock) must not die() mid-loop and strand the `done`
                # deletions already queued for earlier branches unflushed.
                if _git(repo, "reset", "--hard", sha, check=False).returncode == 0:
                    log.append(f"  {branch}: rewritten (hard reset)")
                    if note:
                        log.append(note)
                    done.append(("delete", refname, None, sha))
                else:
                    log.append(f"  {branch}: rewrite (hard reset) failed — resolve "
                               f"and re-apply; kept in accepted")
            else:
                _cas(refname, target, sha, info["current"],
                     f"  {branch}: rewritten (force-updated)",
                     f"  {branch}: moved since review; kept in accepted")
    _update_refs(repo, done)
    return log


# ---------------------------------------------------------------------------
# Launcher dispatch
# ---------------------------------------------------------------------------

def _spool(base: str, jail_user: str) -> Path:
    """The host-only review store: named bundle artifacts and manifests.

    Deliberately a *sibling* of the per-user config dir
    (`.claude-jail-<user>-review`), never `.claude-jail-<user>/review`: the latter
    is the directory docker-compose bind-mounts read-write into the jail at
    /home/claude/.claude, so a quarantine kept inside it would be readable and
    rewritable by the very session under review (and by every other run sharing the
    user). The per-run handshake spool the jail does write to is bound on its own
    at /cvs/spool and lives under this dir keyed per run.
    """
    cfg = user_config_dir(Path(base), jail_user)
    return cfg.parent / f"{cfg.name}-review"


def per_run_spool(base: str, jail_user: str, jail_id: str, pid: int) -> Path:
    """The per-run handshake spool (`<store>/<jail_id>-<pid>`): the throwaway gitdir
    plus pending.* files. Keyed by pid so two concurrent runs of the same repo
    can't collide; the launcher removes it on teardown and sweeps dead ones."""
    return _spool(base, jail_user) / f"{jail_id}-{pid}"


def sweep_stale_spools(base: str, jail_user: str, jail_id: str,
                       keep: "str | None" = None) -> None:
    """Best-effort: remove `<jail_id>-<pid>` spool dirs whose pid is no longer alive.

    Pid-keying (per_run_spool) keeps concurrent runs apart but means a crashed run
    leaks its dir (gitdir clone included). Sweep dead-pid siblings each launch so
    they don't accumulate; a live or unknown pid is left untouched, and `keep` (the
    current run's dir) is never removed.
    """
    store = _spool(base, jail_user)
    try:
        entries = list(store.glob(f"{jail_id}-*"))
    except OSError:
        return
    for d in entries:
        if d.name == keep or not d.is_dir():
            continue
        suffix = d.name[len(jail_id) + 1:]
        # isdecimal(), not isdigit(): isdigit() is also True for characters like
        # superscript '²'/'³' that int() then rejects with ValueError (neither
        # OSError nor OverflowError), which would abort the whole launch. A per-run
        # spool is only ever created with an ASCII pid, so a non-decimal suffix is
        # not ours to reap.
        if not suffix.isdecimal():
            continue
        try:
            os.kill(int(suffix), 0)
        except ProcessLookupError:
            shutil.rmtree(d, ignore_errors=True)   # dead pid: reclaim it
        except (OSError, OverflowError):
            # alive / not ours to signal (OSError), or an implausibly large
            # all-digit suffix that overflows C pid_t (OverflowError, which is NOT
            # an OSError subclass) — leave it rather than abort the whole launch.
            pass


def _manifest_loader(spool: Path):
    """A name -> manifest dict lookup, reading <spool>/<name>.manifest.json."""

    def load(name: str) -> "dict | None":
        path = spool / f"{name}.manifest.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None
    return load


# Escape/control scrubbing for untrusted diff content (see _page). OSC strings
# (ESC ] ... BEL or ST) and CSI sequences are matched whole; _CTRL_RE drops raw
# control bytes — backspace, carriage return, vertical tab, the 0x80-0x9f C1
# range (0x9b is an 8-bit CSI), DEL — that move the cursor or overwrite text on a
# terminal, keeping only tab and newline. _sanitize strips *every* escape — no
# git-emitted sequence is ever trusted as "just colour"; colour is instead
# regenerated from scratch by _colorize, over text this has already scrubbed.
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# Full ECMA-48 CSI: parameter bytes 0x30-0x3F, intermediates 0x20-0x2F, final
# 0x40-0x7E. The narrower [0-9;?] class missed ':' '<' '=' '>', letting some
# sequences slip through untouched.
_CSI_RE = re.compile(r"\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]")
# Control bytes to drop (keeping \t and \n). The \udc80-\udc9f range catches C1
# control bytes (0x80-0x9f, e.g. 0x9b 8-bit CSI) that arrived as invalid UTF-8 and
# were decoded by _git's surrogateescape to U+DC80-U+DC9F — they'd otherwise
# round-trip back to raw C1 bytes on _write and reach the terminal.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\udc80-\udc9f]")  # all but \t \n


def _sanitize(text: str) -> str:
    """Neutralise terminal control sequences in untrusted text.

    OSC strings, CSI/cursor/clear sequences, raw control bytes, and every stray
    ESC are all removed, so a hostile diff can neither move the cursor, clear the
    screen, nor overwrite what the reviewer reads. Nothing escapes — colour
    included: there is deliberately no git escape we trust to be "just colour",
    since a control byte wedged inside an otherwise-SGR sequence could be
    reassembled into a live cursor/clear escape once the byte is stripped. Colour
    is added back afterwards by _colorize, generated host-side from the structure
    of this already-scrubbed text rather than carried through from git (see _page).
    """
    text = _OSC_RE.sub("", text)
    return _CTRL_RE.sub("", _CSI_RE.sub("", text).replace("\x1b", ""))


def _write(text: str) -> None:
    """Write `text` to stdout, re-encoding _git's surrogateescape bytes losslessly.

    Falls back to a plain str write when stdout has no binary buffer (a StringIO
    under test / redirect_stdout).
    """
    buf = getattr(sys.stdout, "buffer", None)
    if buf is None:
        sys.stdout.write(text)
        return
    sys.stdout.flush()
    buf.write(text.encode("utf-8", "surrogateescape"))
    buf.flush()


def _print(text: str = "") -> None:
    """Like print(), but for dispatch lines that may embed untrusted ref/branch
    names: strip control sequences and re-encode surrogateescape bytes, so a
    non-UTF-8 refname can neither crash on a strict-encode nor relay raw escapes."""
    _write(_sanitize(text) + "\n")


# ---------------------------------------------------------------------------
# Host-side re-colouring
#
# git runs --color=never and _sanitize then strips every escape, so no colour
# survives the pipe. We add it back here the safe way: _colorize re-derives the
# colour from the diff's own structure and emits its OWN SGR codes over the
# already-scrubbed (ESC-free) text — so every escape in the result is one of the
# fixed codes below, never a byte that came from the untrusted diff. This is the
# recolour-after-sanitising design the old _page TODO called for.
# ---------------------------------------------------------------------------

# git's default diff palette (cf. `git config color.diff.*`), as SGR parameters.
_C_META = "1"        # bold — file headers (diff --git, index, ---/+++, mode, ...)
_C_FRAG = "36"       # cyan — @@ hunk headers
_C_OLD = "31"        # red — removed lines
_C_NEW = "32"        # green — added lines
# _C_COMMIT and _C_HAZARD are two independent roles that happen to share git's
# yellow; kept as separate names so either can be re-themed without the other.
_C_COMMIT = "33"     # yellow — abbreviated sha in `log --oneline` / range-diff rows
_C_HAZARD = "33"     # yellow — a [!] hazard/rewrite note in the summary view
_RESET = "\x1b[0m"

# A line that opens a diff file section. Only tested when NOT inside a hunk body
# (those lines are keyed off in_hunk first), so a removed line whose content is
# literally "--- x", or an added "+++ y", is never mistaken for a file header.
_DIFF_META_RE = re.compile(
    r"(?:diff --git |diff --cc |diff --combined |index |--- |\+\+\+ |"
    r"old mode |new mode |deleted file mode |new file mode |"
    r"copy from |copy to |rename from |rename to |"
    r"similarity index |dissimilarity index |Binary files )")
# `git log --oneline` row: an abbreviated sha then a space and a non-empty
# subject. The `(?= \S)` lookahead requires that subject, so a bare hex token, or
# a hex-looking bundle name padded with two spaces before a `[!]` note (both of
# which lack a ` <subject>` right after the hex), is not mistaken for a commit sha.
_COMMIT_RE = re.compile(r"[0-9a-f]{7,40}(?= \S)")
# @@ -a,b +c,d @@[ section heading]: colour through the closing @@ (frag) and
# leave the trailing heading plain, as git does. `@@+` also matches a combined
# diff's `@@@` header; its leading-@ count gives the +/- column count (_colorize).
_HUNK_RE = re.compile(r"(@@+ .*? @@+)(.*)")
# `git range-diff` commit-pair row: `<n>:  <sha> <op> <n>:  <sha> <subject>`,
# where either side may be `-:  -------` when a commit is on only one side. The
# two abbreviated shas (groups 3 and 7) get tinted; the nested diff-of-diffs body
# under each row is left uncoloured — reproducing git's range-diff palette exactly
# would need a fragile diff-of-a-diff parser, so we colour only the navigation.
_RANGEDIFF_RE = re.compile(
    r"(\d+|-)(:\s+)([0-9a-f]{7,40}|-+)(\s+[=<>!]\s+)"
    r"(\d+|-)(:\s+)([0-9a-f]{7,40}|-+)(.*)$")


def _sgr(code: str, text: str) -> str:
    """Wrap `text` in an SGR `code` plus a reset. Per-line, so colour can't bleed
    across lines in the pager."""
    return f"\x1b[{code}m{text}{_RESET}"


def _rangediff_row(m: "re.Match") -> str:
    """Tint the two abbreviated shas of a `git range-diff` commit-pair row.

    Rebuilt from the _RANGEDIFF_RE groups (which cover the whole line), wrapping
    each sha token — but not the `-------` placeholder a one-sided row uses — in
    the commit colour; every other byte is copied verbatim, so stripping the SGR
    reproduces the input.
    """
    g = m.groups()
    def sha(tok: str) -> str:
        return tok if tok[0] == "-" else _sgr(_C_COMMIT, tok)
    return g[0] + g[1] + sha(g[2]) + g[3] + g[4] + g[5] + sha(g[6]) + g[7]


def _colorize(text: str) -> str:
    """Re-colour already-sanitised diff text with our own SGR codes.

    A structure-aware recolour of git's --color=never output. The input has been
    through _sanitize and so carries no ESC byte; every escape in the result is
    therefore one of the fixed _C_* codes above — none is derived from the
    untrusted diff. Colours follow git's diff defaults so the view reads like a
    normal `git diff`; context lines are left uncoloured, exactly as git leaves
    them. Purely additive: strip the SGR codes back out and the text is unchanged.
    """
    out: "list[str]" = []
    in_hunk = False
    hunk_cols = 1                            # +/- marker columns; 2+ in a combined diff
    for line in text.split("\n"):
        if in_hunk:
            prefix = line[:hunk_cols]        # one marker column per diff parent
            if "+" in prefix:
                out.append(_sgr(_C_NEW, line)); continue
            if "-" in prefix:
                out.append(_sgr(_C_OLD, line)); continue
            if line[:1] in (" ", "\\", ""):  # context / "\ No newline" / blank
                out.append(line); continue
            in_hunk = False                  # anything else ends the hunk body
        m = _HUNK_RE.match(line)
        if m:                                # a real @@..@@ header; a stray "@@ prose"
            in_hunk = True                   # has no closing @@ and won't match here,
            frag = m.group(1)                # so it can't spuriously open a hunk body
            hunk_cols = len(frag) - len(frag.lstrip("@")) - 1   # @@ -> 1, @@@ -> 2, ...
            out.append(_sgr(_C_FRAG, frag) + m.group(2))
        elif _DIFF_META_RE.match(line):
            out.append(_sgr(_C_META, line))
        elif line.lstrip().startswith("[!]"):
            out.append(_sgr(_C_HAZARD, line))
        else:
            rd = _RANGEDIFF_RE.match(line)
            c = None if rd else _COMMIT_RE.match(line)
            if rd:                           # range-diff commit-pair row: tint its shas
                out.append(_rangediff_row(rd))
            elif c:                          # log --oneline row: colour the leading sha
                out.append(_sgr(_C_COMMIT, c.group(0)) + line[c.end():])
            else:
                out.append(line)
    return "\n".join(out)


def _color_enabled() -> bool:
    """Whether to add colour. Honours the NO_COLOR convention (no-color.org): any
    non-empty NO_COLOR disables it."""
    return not os.environ.get("NO_COLOR")


def _page(text: str, paginate: bool) -> None:
    """Emit `text`, through `less` when `paginate`, else straight to stdout.

    The diff content is untrusted and git does not escape control sequences
    embedded in it (a hostile bundle can carry cursor-moves / screen-clears /
    overwrites), so the text is sanitised here before it can reach a terminal —
    _page, not the pager, is the security boundary. Every git-emitted escape is
    stripped, colour included: keeping SGR while dropping the rest once let a
    control-byte-split CSI (e.g. ESC '[' DEL '2' 'J') be reassembled into a live
    clear/cursor escape after the wedged byte was stripped — past the pager, the
    thing this guard exists to stop.

    Colour is added back only *after* that scrub, and only for the terminal: on
    the paginate path _colorize re-derives it from the now escape-free text's own
    diff structure and emits our own SGR codes, so the only escapes less ever sees
    are ones we generated, never anything attacker-derived. That is why `less -R`
    (which renders SGR raw) is safe here even though the input is untrusted: the
    scrub already removed every foreign escape. $PAGER / $GIT_PAGER are still not
    honoured (an inherited `PAGER=cat` would relay raw bytes), LESS is cleared so
    an inherited `LESS=-r` can't widen -R into full raw mode, and LESSSECURE shuts
    off less's own shell/file escapes. When piped (paginate is False) the text is
    written straight out with no colour, like git's --color=auto; if less is
    missing the same post-scrub text is written out but still carries whatever
    colour was added above — paginate implies a tty, where our own SGR renders.
    """
    safe = _sanitize(text)
    if not paginate:
        _write(safe)                       # piped: no colour, matching git
        return
    if _color_enabled():
        safe = _colorize(safe)
    env = {**os.environ, "LESS": "", "LESSSECURE": "1"}
    try:
        # -R: render our SGR colour raw. Safe only because `safe` is post-scrub —
        # its sole escapes are the SGR codes _colorize just added (less -R also
        # passes OSC-8 links raw, but _sanitize removed every OSC and _colorize
        # emits none), so -R can interpret nothing that came from the diff.
        subprocess.run(["less", "-R", "-F", "-X"], env=env,
                       input=safe.encode("utf-8", "surrogateescape"))
    except OSError:
        _write(safe)                       # less absent: our coloured (tty-safe) text


# git versions before this lack the checkout path-traversal hardening
# (CVE-2021-21300, fixed in 2.30.1 / 2.31.0). apply checks out the agent's
# untrusted tree into the real working tree, so an older git is warned about.
_SAFE_CHECKOUT_GIT = (2, 30, 1)


def _git_version(repo: str) -> "tuple[int, int, int] | None":
    """The host git version as (major, minor, patch), or None if unparseable."""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?",
                  _git(repo, "--version", check=False).stdout)
    return tuple(int(x) for x in m.groups(default="0")) if m else None


def _confirm_accept(name: str) -> bool:
    """Strongly warn that a bundle is untrusted agent output before accepting it.

    Accept is the human's first explicit "I take this on" step, so the warning
    lands here. On a tty it requires an explicit y/N; a non-interactive caller
    (a script, a test) still gets the warning but proceeds, so automation isn't
    broken by a prompt that can't be answered.
    """
    print(
        "\nReview: WARNING — you are about to ACCEPT a bundle produced by the\n"
        "        sandboxed, UNTRUSTED agent. Its commits, file contents and any\n"
        "        embedded .gitmodules/hooks are attacker-controlled; accepting\n"
        "        marks it for `apply`, which writes it into your real branches\n"
        "        and working tree. Make sure you have read the actual diff\n"
        f"        (`claude-jail review {name}`) before you do.\n", file=sys.stderr)
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return True
    try:
        return input(f"Accept {name}? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def dispatch(command: str, command_args: "list[str]", config_path: str,
             user_flag: "str | None", base_flag: "str | None") -> int:
    """Run a host-only review command against the repo the config anchors.

    Resolves just enough config to find the repo (the config's own directory) and
    the per-user spool, without requiring docker or a launchable jail — the same
    light footprint as `prune`.
    """
    data = read_config(config_path)
    jail_user = resolve_user.resolve(data, user_flag, config_path)
    base = resolve_claude_dir_base.resolve(data, base_flag, config_path,
                                           must_exist=False)
    # Deliberately no parse_roots() here: these are host-only, read-only commands
    # that act solely on the config's own repo (below) and never use the roots.
    # Validating them would die() if an *unrelated* second root was since deleted
    # or unmounted — blocking even `apply` of an already-quarantined bundle on an
    # otherwise-inspectable repo. read_config already rejects a malformed config,
    # and require_repo covers the one directory these commands touch.
    repo = str(config_dir(config_path))
    require_repo(repo)
    spool = _spool(base, jail_user) if base else None
    load_manifest = _manifest_loader(spool) if spool else (lambda _n: None)

    allow_rewrite = "--allow-rewrite" in command_args
    list_only = "--list" in command_args
    names = [a for a in command_args if not a.startswith("-")]

    if command == "review":
        # Page the diff (like git) when interactive; the content is sanitised
        # either way (no colour is kept — see _page).
        interactive = sys.stdout.isatty()
        # An explicit name, or --list, picks the view; otherwise a lone pending
        # bundle is shown in full (the common case — review what just came back),
        # and zero or several fall back to the summary list.
        if not names and not list_only:
            pending = bundle_list(repo, "unreviewed")
            names = pending if len(pending) == 1 else []
        if names:
            name = resolve_bundle(repo, "unreviewed", names[0])
            text = review_detail(repo, name, load_manifest(name))
        else:
            text = review_overview(repo, load_manifest) + "\n"
        _page(text, interactive)
        return 0
    if command == "accept":
        if not names:
            die("accept needs a bundle name")
        name = resolve_bundle(repo, "unreviewed", names[0])
        if not _confirm_accept(name):
            _print(f"Left {name} unreviewed.")
            return 0
        report = accept(repo, name, load_manifest(name))
        _print(f"Accepted {name}.")
        if _has_rewrite(report):
            _print("  [!] rewrites history — review before you apply.")
        return 0
    if command == "reject":
        if not names:
            die("reject needs a bundle name")
        name = resolve_bundle(repo, "unreviewed", names[0])
        reject(repo, name, spool)
        _print(f"Rejected {name}.")
        return 0
    if command == "apply":
        targets = ([resolve_bundle(repo, "accepted", n) for n in names]
                   or bundle_list(repo, "accepted"))
        if not targets:
            _print("No accepted bundles to apply.")
            return 0
        ver = _git_version(repo)
        # Fail closed on an unparseable version (ver is None): an exotic/vendor git
        # whose banner we can't read is exactly the one that might predate the
        # CVE-2021-21300 checkout hardening, so warn rather than stay silent.
        if ver is None or ver < _SAFE_CHECKOUT_GIT:
            have = ".".join(map(str, ver)) if ver is not None else "an unknown version"
            print("Review: WARNING — your git is " + have
                  + "; versions before " + ".".join(map(str, _SAFE_CHECKOUT_GIT))
                  + " lack the checkout path-traversal fix (CVE-2021-21300). "
                  "apply checks out the agent's untrusted tree into your working "
                  "tree, so a crafted commit could write outside it — upgrade git "
                  "before applying untrusted bundles.", file=sys.stderr)
        for name in targets:
            # apply log lines embed branch names taken verbatim from the bundle,
            # so route them through the sanitising _print, not raw print().
            _print(f"Applying {name}:")
            for line in apply_bundle(repo, name, allow_rewrite, load_manifest(name)):
                _print(line)
        return 0
    raise AssertionError(f"review.dispatch: unhandled command {command!r}")
