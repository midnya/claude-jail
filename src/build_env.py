"""Build the environment for a claude-jail run from .claude-jail.json.

build_mounts.py owns the filesystem keys (read_only / hidden); this module owns
the rest of the config — the settings that shape how claude is launched.
exports() returns the {NAME: value} mapping for the recognised settings (empty
when none is set), which claude-jail sets in the environment and
docker-compose.yml forwards into the container; service_lines() returns the
settings rendered straight into the compose override instead. Calls die() with a message on
stderr if the config is malformed or a value is invalid. Shared
parsing/validation helpers live in jail_config.py.

Settings:
  default_mode -> CLAUDE_JAIL_PERMISSION_MODE
      The permission mode claude starts in, applied by the entrypoint as
      `claude --permission-mode`. The set of valid modes belongs to claude,
      which rejects an unknown one at startup and lists the choices, so we only
      check it is a bare word — both to keep this decoupled from claude's list
      and so the value is safe to export and interpolate without quoting.
  mem_limit -> compose override `mem_limit:`
      ✦ The agent container's memory cap, as docker's byte value: an integer
      with an optional b/k/m/g suffix ("8g"). Unset means unlimited. The
      --mem-limit flag overrides it. Not an env var, so nothing inherited from
      the shell can set it.
  gh_token_file -> GH_TOKEN (via JAIL_GH_TOKEN)
      ✦ A host file holding one fine-grained GitHub token, read by the launcher
      and handed to the in-jail gh CLI through the environment only. Honoured
      only from a config outside the jail: an in-jail config is agent-written, so
      its path could aim the read at any host file. The --gh-token-file flag
      overrides it. refuse_writable_remotes() then refuses the launch when the
      token can write to any GitHub remote of a jail root.
"""
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from jail_config import BARE_WORD, die, read_regular_file, trusted_host_path

MEM_LIMIT = re.compile(r"\A[1-9][0-9]*[bkmg]?\Z", re.IGNORECASE)
# ✦ Fine-grained PATs only; classic tokens (ghp_) can't be scoped per repository.
GH_TOKEN = re.compile(r"\Agithub_pat_[A-Za-z0-9_]+\Z")
GITHUB_HOSTS = frozenset({"github.com", "www.github.com", "ssh.github.com"})
GITHUB_REPO = re.compile(r"\A/?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?\Z")
# git's scp-like syntax ([user@]host:path) applies only with no slash before the colon.
SCP_URL = re.compile(r"\A(?:[^@/:]+@)?([^/:]+):(.*)\Z")
GITHUB_API = "https://api.github.com"
PROBE_REF = "heads/claude-jail-write-probe"
REDIRECTS = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 3


def default_mode(data: "dict", config_file: str) -> "str | None":
    """The claude --permission-mode value from default_mode, or None."""
    mode = data.get("default_mode")
    if mode is None:
        return None
    if not isinstance(mode, str) or not BARE_WORD.match(mode):
        die(f"'default_mode' in {config_file} must be a bare word "
            f"(letter, then letters/digits/'-'/'_'); got {mode!r}")
    return mode


def mem_limit(data: "dict", config_file: str,
              flag: "str | None" = None) -> "str | None":
    """✦ The docker memory cap (--mem-limit, else mem_limit), or None."""
    if flag is not None:
        value, source = flag, "--mem-limit"
    else:
        value, source = data.get("mem_limit"), f"'mem_limit' in {config_file}"
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)) \
            or not MEM_LIMIT.match(str(value)):
        die(f"{source} must be a positive size like \"8g\" or \"512m\"; "
            f"got {value!r}")
    return str(value)


def gh_token(data: "dict", config_file: str, cfg_dir: str,
             config_in_jail: bool, flag: "str | None" = None,
             read: bool = True) -> "str | None":
    """✦ The token from --gh-token-file, else gh_token_file, or None.

    Paths expand `~`, resolve against `cfg_dir` and never through a symlink. `read=False`
    validates the setting without touching the file, for commands that don't
    launch a session.
    """
    key = data.get("gh_token_file")
    key_source = f"'gh_token_file' in {config_file}"
    if key is not None and config_in_jail:
        die(f"{key_source} is refused: the config is inside the jail, so the agent "
            "controls the path; use --gh-token-file or an external --config")
    rel, source = (flag, "--gh-token-file") if flag is not None \
        else (key, key_source)
    if rel is None:
        return None
    if not isinstance(rel, str) or not rel:
        die(f"{source} must be a non-empty path")
    if not read:
        return None
    rel = os.path.expanduser(rel)
    path = trusted_host_path(cfg_dir, rel, source)
    try:
        token = read_regular_file(path, f"{source}: {rel}").strip()
    except (OSError, UnicodeDecodeError) as e:
        die(f"{source}: could not read {rel}: {e}")
    if not GH_TOKEN.match(token):
        die(f"{source}: {rel} must hold exactly one fine-grained GitHub token "
            "(github_pat_...)")
    return token


def remote_urls(root_dir: str) -> "list[tuple[str, str]]":
    """✦ (remote name, url) for every fetch and push url of the repo at root_dir.

    `git remote get-url` applies `url.*.insteadOf`, so these are the urls git
    really talks to. A root that isn't in a git repo has none; any other git
    failure dies rather than silently skipping the check.
    """
    def git(*args):
        try:
            r = subprocess.run(["git", "-C", root_dir, *args], capture_output=True,
                               text=True, env={**os.environ, "LC_ALL": "C"})
        except OSError as e:
            die(f"--gh-token-file needs git to check {root_dir}'s remotes: {e}")
        if r.returncode != 0:
            if "not a git repository" in r.stderr:
                return None
            die(f"could not list the git remotes of {root_dir}: {r.stderr.strip()}")
        return r.stdout.splitlines()

    names = git("remote")
    if names is None:
        return []
    return [(name, url) for name in names
            for push in ([], ["--push"])
            for url in git("remote", "get-url", *push, "--all", name) or []]


def github_repo(url: str, where: str) -> "str | None":
    """✦ The "owner/repo" a github.com remote url names, or None if not GitHub."""
    if "://" in url:
        parts = urllib.parse.urlsplit(url)
        host, path = parts.hostname, parts.path
    else:
        m = SCP_URL.match(url)
        if not m:
            return None
        host, path = m[1], m[2]
    if (host or "").lower() not in GITHUB_HOSTS:
        return None
    m = GITHUB_REPO.match(path)
    if not m:
        # The url itself may embed credentials, so name the remote, not the url.
        die(f"{where} is on GitHub but its owner/repo could not be parsed")
    return f"{m[1]}/{m[2]}"


def _probe_write(repo: str, token: str) -> "tuple[int, dict]":
    """✦ (HTTP status, headers) of a ref update that can never succeed.

    Moving a ref to the all-zero sha is rejected whatever the ref, so nothing
    changes; what tells write access apart is the rejection: 422 once the token
    passed the Contents:write check, 403/404 when it didn't.
    """
    url = f"{GITHUB_API}/repos/{repo}/git/refs/{PROBE_REF}"
    for _hop in range(MAX_REDIRECTS + 1):
        req = urllib.request.Request(
            url, method="PATCH",
            data=b'{"sha": "' + b"0" * 40 + b'", "force": false}',
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "claude-jail"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, dict(resp.headers)
        except urllib.error.HTTPError as e:
            status, headers = e.code, dict(e.headers)
        # A renamed or transferred repo answers with a redirect, which urllib won't
        # follow for a PATCH. Follow it ourselves, but only within the API host so
        # the token never leaves it.
        location = {k.lower(): v for k, v in headers.items()}.get("location")
        if status not in REDIRECTS or not location:
            return status, headers
        url = urllib.parse.urljoin(url, location)
        if not url.startswith(GITHUB_API + "/"):
            return status, headers
    return status, headers


def refuse_writable_remotes(token: str, roots: "list") -> None:
    """✦ Die, loudly, if `token` can write to any GitHub remote of `roots`.

    The read-only .git only guards local history: a token with Contents:write lets
    the agent push to (or rewrite) the jail's own repos from inside. Fails closed:
    anything but a clear yes/no from GitHub also refuses the launch.
    """
    repos = {}
    for root in roots:
        for name, url in remote_urls(root.dir):
            where = f"{root.dir}: remote '{name}'"
            repo = github_repo(url, where)
            if repo:
                repos.setdefault(repo.lower(), (repo, where))
    writable = []
    for repo, where in repos.values():
        try:
            status, headers = _probe_write(repo, token)
        except OSError as e:
            die(f"could not check the GitHub token's access to {repo} ({where}): "
                f"{e}; refusing to start")
        headers = {k.lower(): v for k, v in headers.items()}
        throttled = "retry-after" in headers \
            or headers.get("x-ratelimit-remaining") == "0"
        if status == 422 or 200 <= status < 300:
            writable.append(f"!!   {repo}  ({where})")
        elif status not in (403, 404) or throttled:
            die(f"could not check the GitHub token's access to {repo} ({where}): "
                f"GitHub answered HTTP {status}; refusing to start")
    if writable:
        bar = "!" * 78
        die("\n".join([
            "", bar,
            "!! REFUSING TO START: the GitHub token can WRITE to this jail's repos:",
            *writable,
            "!!",
            "!! The jail's .git is read-only, but with Contents:write the agent could",
            "!! push to or rewrite these repositories through GitHub instead.",
            "!! Give the token read-only Contents access, or drop these repositories",
            "!! from it, then relaunch.",
            bar]))


def exports(data: "dict", config_file: str) -> "dict[str, str]":
    """The {NAME: value} env mapping for every recognised setting that is set."""
    # (env var, value) for each recognised setting; None means "not set".
    settings = [
        ("CLAUDE_JAIL_PERMISSION_MODE", default_mode(data, config_file)),
    ]
    return {name: value for name, value in settings if value is not None}


def service_lines(data: "dict", config_file: str,
                  mem_limit_flag: "str | None" = None) -> "list[str]":
    """✦ The `claude` service keys for the compose override, indented to fit."""
    mem = mem_limit(data, config_file, mem_limit_flag)
    return [f"    mem_limit: {mem}"] if mem else []
