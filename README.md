# claude-jail

2026/10/19: Today, I successfully fired my last two remaining synapses, and understood how "Claude Connectors" work.
 - This is all a mess. What even is going on here. Why are we doing this.
 - Is this the future? All of our API calls inspected by the thing we offloaded our brains to?
 - The various MCP mechanisms and tooling can bypass so many of the protections.
 - I need time to figure things out in detail.

e.g., as of writing, I can't prevent the following:
 - the `claude` binary makes an HTTP call to `claude.com` (needed for... claude to work).
 - that call is "Do something with the Atlassian Connector".
 - that something is "Write `%content%` to the .git repository that is being `claude-jail`ed".

tl;dr: as of today, the potential blast radius is as large as the organization's Connectors.

---

Run Claude Code inside a locked-down Docker container.

Requires Docker v23+ (I think?) and Compose v2 (the plugin invoked via `docker compose`, not the binary `docker-compose`).
Requires Python 3, with no other dependencies.
Very git centric. I have no genuine experience with other VCS to check for correctness.

Still in active development; bug reports, feature requests, and PRs are most welcome!

## AI usage disclosure

`claude-jail` has mostly been built with `Claude` itself; my time is, bluntly, better spent elsewhere.

As it stands, **it is essentially slopware**. Maybe I'll spend the time to rewrite it properly, maybe not. The ever-evolving feature-set, a consequence of figuring out what this tool needs and wants, makes it unlikely in the near future.

Its .git has always been quarantined. I trust it enough that the `claude` CLI is uninstalled from my host systems.

**Use at your own risk.**

## Security model (so far)

Because most of Claude Code features rely on arbitrary code execution, we try to reduce
the attack surface as much as possible, while still offering convenience for day-to-day
operations:
- Filesystem protection: bypasses Claude's sandbox feature and relies on Docker's instead.
  - VCS directories are read-only by default.
    - Opt-in "review" mode where modifications to .git are bundled
      after container's teardown and available for human review.
    - `gh` is bundled in the jail by default, but without credentials. If adding any,
      care must be taken that you don't give it access to its own repo, assuming it
      is hosted on GitHub.
        - The stated goal for including `gh` is for more-efficient read-only access to GitHub.
        - There's one (1) guard against letting `gh` push to a protected .git.
          There are so many more ways to bypass it. Please be careful.
  - For convenience, `/tmp` is a persisted volume that is shared per-workspace,
    between jail instances.
- Network isolation: the jail container sits on an `internal` Docker network.
  All egress is forced through a [Squid](https://www.squid-cache.org/)
  proxy.
  - The proxy filters egress by destination. The policy is
    **default-deny**: with no `egress` config, only `*.anthropic.com` and
    `*.claude.com` are reachable. Every request is logged to a per-jail volume.
  - DNS resolution is forced through an [Unbound](https://www.nlnetlabs.nl/projects/unbound/)
    resolver. It enforces the same deny/allow list as Squid's. Every lookup is logged
    to a per-jail volume. For convenience, CNAMEs are followed even if not
    explicitely allowed.
- Additional packages are declared, and pre-baked in the image via a configuration file.
  - Read-only for packages stores is implemented, but should not be relied upon.
    Claude is able to run anything, so this won't stop, just fires errors and delay.
- Opt-in exception: `--ide` opens a channel from the sandbox to a running code
  editor on the host.
  - It watches the host's `~/.claude/ide` lockfile directory, therefore republishing
    *every* advertised editor window.
  - I have no trust in this feature. I will not spend time improving this feature.
  - I don't know why you would want your IDE to be connected to a jailed
    Claude, but I'm not your mom. Which means I can judge you for it.
  - You have been properly warned.

## Invocation

Everything goes through `claude-jail`:

```sh
claude-jail [--user <name>] [--config <path>] [--claude-dir-base <dir>] [--subnet <cidr>] [--mem-limit <size>] [--gh-token-file <path>] [--ide] [--system-prompt <text>]... [--system-prompt-file <path>]... [COMMAND] [args...]
```

- `--user <name>` (`-u`): a config namespace. Claude's config and credentials
  persist on the host in `<claude-dir-base>/.claude-jail-<name>/` and
  `<claude-dir-base>/.claude-jail-<name>.json` (`<claude-dir-base>` defaults to
  `$HOME`), created on first run. Use different names to keep separate
  identities/logins.
- `--config <path>` (`-c`): the jail config file. Defaults to
  `./.claude-jail.json`.
- `--claude-dir-base <dir>`: the host base directory for the per-user store above.
  Overrides the `claude_dir_base` config key; defaults to `$HOME`.
- `--subnet <cidr>`: override the jail's internal Docker subnet (e.g.
  `10.123.45.0/24`). Must be a private (RFC1918) range, `/26` or larger.
- `--mem-limit <size>`: cap the jail container's memory (e.g. `8g`, `512m`).
  Overrides the `mem_limit` config key; unlimited by default.
- `--gh-token-file <path>`: a host file holding one fine-grained GitHub token
  (`github_pat_...`), passed to the in-jail `gh` CLI as `GH_TOKEN`. Overrides the
  `gh_token_file` config key. Resolved like `--system-prompt-file`. See
  [GitHub CLI](#github-cli).
- `--ide`: bridge the in-jail `/ide` to a code editor running on the host.
  Off by default.
- `--system-prompt <text>`: append an extra system prompt (inline literal text)
  to the jail's built-in one, after the config's `system_prompts`. Repeatable.
- `--system-prompt-file <path>`: append an extra system prompt read from a file.
  A relative path resolves against the config file's directory, an absolute path
  is used as-is, and the read never follows a symlink. You supply it (not the
  agent), so unlike an in-jail config's own `system_prompts` file it is trusted to
  live anywhere on the host. Repeatable.
- `[COMMAND] [args...]`: one of the commands below. With no command, `run` is assumed.

## Commands

Run from the directory that holds your `.claude-jail.json` (or point `-c` at it).

- `new`: starts a fresh session. Anything after the command is forwarded to `claude`;
  use `--` to pass a leading-dash argument:

  ```sh
  claude-jail new
  claude-jail new -- -p "summarize this repo"
  ```

- `run` (the default, assumed when no command is given): like `new`, but resumes
  the last session in this project if there is one:

  ```sh
  claude-jail run
  claude-jail
  claude-jail -- --help
  claude-jail run -- -p "review the last commit"
  ```

- `build`: build the sandbox images (needed once, or to update the claude
  package / side-services). Accepts `--no-cache` and `--pull`:

  ```sh
  cd ~/code/myproject
  claude-jail build --no-cache
  ```

- `down`: tear this project's containers down (volumes are kept; use `-v` to drop them too).
- `logs [service]` / `ps`: inspect the containers (e.g. `logs squid` for the HTTP
  egress log, `logs dns` for the DNS query log, ...).
- `compose -- <args>`: runs a raw `docker compose` command against the jail's project:

  ```sh
  claude-jail compose -- logs -f squid
  claude-jail compose -- run --rm -e FOO=bar claude
  ```
- `prune`: remove the content-addressed images (`claude-jail-<digest>`) left
  behind when a `packages` list or a `uid`/`gid` changes.

`-c` lets you launch from anywhere:

```sh
claude-jail -c ~/code/myproject/.claude-jail.json
```

`--dind` uses a different image and container configuration,
adding Docker-in-Docker capabilities.
Mostly meant for development, but you may have a use for it.
Not as thoroughly vetted as the rest of the code, **use at your own risk**.

```sh
claude-jail --dind new
```

### Review

When a session runs with `review` on, the commits come back as a quarantined
git bundle instead of modifying the actual history:

- `review`: summarise the pending bundles, each with a per-branch classification
  (new / fast-forward / divergent / rewrite) and any hazards (a re-pointed tag, a
  `replace` ref, a branch the session deleted):

  ```sh
  claude-jail review --list     # List the available bundles
  claude-jail review <bundle>   # `git diff` of a specific bundle
  claude-jail review            # one pending bundle -> its diff; several -> the list
  ```

- `accept <bundle>` / `reject <bundle>`: accept a bundle for landing / discard
  it.

- `apply [bundle...]`: Merge one or more accepted bundles into the actual tree.
  A history rewrite is refused unless `--allow-rewrite` is passed:

  ```sh
  claude-jail apply                           # every accepted bundle
  claude-jail apply <bundle> --allow-rewrite
  ```

## Configuration

Example:

```json
{
  "user": "me",
  "default_mode": "plan",
  "system_prompts": { "path": "CLAUDE_JAIL_PROMPT.md" },
  "egress": { "default": "deny", "allowed": ["github.com", "pypi.org", "10.0.0.0/8"] },
  "packages": {
    "apt": ["jq", "ripgrep"],
    "pip": ["requests==2.31.0", "ruff"],
    "npm": ["typescript", "prettier@^3"]
  },
  "roots": [
    {
      "path": ".",
      "read_only": ["config/secrets.yml", "production/"],
      "hidden": [".env", "private/notes"]
    },
    { "path": "../shared-lib", "read_only": ["."] },
    "../scratch"
  ]
}
```

Available keys:
- `user`: the config namespace. The `--user` flag overrides it; with neither
  set, it defaults to the host's `$USER`/`$USERNAME` environment variable.
- `uid` / `gid`: the numeric ids the in-container user is built with. They
  default to the host user's own (`id -u` / `id -g`), so files the agent writes
  through the mounts land owned by you. Like `packages`, they fold into the image
  tag, so changing either mints a fresh image (rebuilt on first use); clean the
  old one with `claude-jail prune`.
- `claude_dir_base`: the host base directory for the per-user store
  (`<claude_dir_base>/.claude-jail-<user>/` and
  `<claude_dir_base>/.claude-jail-<user>.json`).
  The `--claude-dir-base` flag overrides it; with neither set, it defaults to `$HOME`.
  A relative value is resolved against the config file's directory, `~` is
  expanded, and the directory must already exist when launching the jail. No jail
  root may be it or a directory containing it (either would expose the credential
  store); a root *inside* it, such as a project under `$HOME`, is fine.
- `roots`: the directories to jail, each bind-mounted (read-write by default) at
  `/workspace/<abs path>`. A list whose entries are either a string path or an
  object `{ "path", "read_only", "hidden" }`. A relative `path` is resolved
  against the config file's directory; an absolute one is taken as-is. Omit
  `roots` to jail the config file's own directory.
  - The session starts in the config file's directory, mounted under `/workspace`.
    Should the config's directory not be part of the roots, that directory will be empty.
  - `read_only` (per root): paths relative to that root, bind-mounted read-only.
    Visible inside the jail but writes fail at the filesystem level. `"read_only": ["."]`
    to make the whole root read-only.
  - `hidden` (per root): paths relative to that root, masked to empty. A hidden
    directory mounts as an empty read-only volume; a hidden file is masked with
    a read-only empty file.
- `default_mode`: the permission mode Claude starts in, forwarded to
  `claude --permission-mode`.
- `mem_limit`: the jail container's memory cap: a positive integer with an
  optional single-letter unit `b`, `k`, `m` or `g` (`"8g"`, `"512m"`, or a byte
  count). Two-letter forms like `"8gb"` are rejected. Unlimited when absent. The
  `--mem-limit` flag overrides it.
- `gh_token_file`: the path of a host file holding the project's GitHub token, as
  for `--gh-token-file` (which overrides it). Refused when the config lives inside
  the jail; set it in an external `--config` or pass the flag.
- `gh_warn_ssh_aliases`: `false` silences the launch warning about ssh remotes
  that aren't on github.com (see [GitHub CLI](#github-cli)); the write check
  is unchanged. Defaults to `true`. `false` is refused when the config lives
  inside the jail.
- `egress`: the network egress policy, enforced by the Squid proxy (HTTP) and
  the Unbound resolver (DNS). An object:
  - `default`: `"deny"` (the default) or `"allow"`.
  - With `default: "deny"`, `allowed` lists the hosts that may be reached;
    everything else is blocked. With `default: "allow"`, `denied` lists the hosts
    to block; everything else is reached. (The list that doesn't match `default`
    is rejected.)
  - A host pattern is a domain or a CIDR:
    - a domain (`example.com`) matches the host **and every subdomain**
      (`api.example.com`, `a.b.example.com`); a leading `*.` (`*.example.com`)
      is accepted as a synonym. A bare TLD or single label (`com`, `localhost`)
      is rejected, so a pattern can never open a whole TLD.
    - a CIDR (`1.2.3.4/32`, `10.0.0.0/8`, IPv6 too) matches that address or
      range. A bare IP without a prefix is rejected — write `/32` (`/128` for IPv6).
  - `*.anthropic.com` and `*.claude.com` are always reachable, so the jail can always reach the API.
- `packages`: extra packages to install into this jail's image, grouped by
  manager (installed at build time, since a running jail has no egress to the
  package mirrors).
  - `apt`: Debian package names, added on top of the image's fixed set.
  - `pip`: Python distributions. An entry is a PyPI name with optional extras
    and version specifiers — pins, ranges, and wildcards (`requests==2.31.0`,
    `django<5`, `numpy==1.26.*`, `uvicorn[standard]`). It is a permissive
    filter, not a full PEP 508 validator: URL/VCS installs and environment
    markers (`pkg; python_version<"3"`) are rejected outright, and anything
    else malformed is caught by `uv` at build time.
    - They are installed with [`uv`](https://github.com/astral-sh/uv)
      into a `--system-site-packages` venv; root-owned (so read-only to the
      unprivileged agent in the container), prepended to `PATH`.
    - The venv is read-only and `--user` installs are disabled.
  - `npm`: Node packages. An entry is an npm name with an optional `@scope/`
    prefix and an optional `@version` range.
    - They are installed with the image's bundled [`yarn`](https://yarnpkg.com)
      into a root-owned tree, with its bin dir prepended to `PATH` and the
      modules on `NODE_PATH` (so the CLI tools run and the modules `require()`).
  - Note that the package sets are folded into the image tag, so changing a list
    builds a fresh image and leaves the previous one behind. Clean with
    `claude-jail prune`.
- `system_prompts`: an extra system prompt, appended to the jail's built-in one.
  A segment is either inline text or a file path, and you may pass one or a list of them:
  - inline: `"system_prompts": "Prefer pnpm over npm in this repo."`
  - file: `"system_prompts": { "path": "CLAUDE_PROMPT.md" }`
  - list (segments joined with a blank line, in order):
    `"system_prompts": ["Prefer pnpm over npm.", { "path": "CLAUDE_PROMPT.md" }]`

Notes:
- `user` must be a bare word (a letter, then letters/digits/`-`/`_`).
- Egress is default-deny: without an `egress` key the jail can reach only
  `*.anthropic.com` and `*.claude.com`. A domain pattern matches the host and
  every subdomain; a CIDR is matched by range. A prefix must be provided.
- The VCS metadata dirs `.git`, `.hg`, and `.svn` in every root are always
  read-only. Under git-review `.git` becomes a writable throwaway shadow (its real
  history stays read-only elsewhere).
- Each root must be an existing directory; roots may not be nested in or
  duplicate one another, nor be the filesystem root `/`, the config store
  directory (`claude_dir_base`, default `$HOME`), or a directory containing it —
  that store holds your credentials, so a root must never expose it.
- `.claude-jail.json` in every root is hidden inside the container,
  including the active config wherever it sits, even if it doesn't exist yet.
- A config path that names something inside a root but escapes via a symlink is
  a hard error.
- A `system_prompts.path` file is read on the host at launch (its text is
  injected into the prompt, never mounted), relative to the config file's
  directory. The read never follows a symlink. For a config inside the
  jail the file must resolve inside some root; for a config outside
  the jail it is trusted and may live anywhere.
- Per-root `read_only`/`hidden` paths must be relative and stay inside their
  root. When a path is listed under both, `hidden` wins.
- A `read_only` entry of `"."` (the root itself) makes the whole root
  read-only; the per-path `read_only` list and the always-on VCS read-only rules
  become redundant and are dropped. `hidden` may not name the root itself.
- A missing `read_only` path is skipped; a missing `hidden` path is a hard error.
- A missing or empty `system_prompts.path` file is a hard error.
- An explicit `--permission-mode` on the command line wins.

### GitHub CLI

`gh` has no persisted login in the jail. Each session gets its credentials from
`--gh-token-file` (or `gh_token_file`):

- Use a [fine-grained token](https://github.com/settings/personal-access-tokens)
  limited to the repositories and permissions the session needs. Classic tokens
  (`ghp_...`) are rejected.
- The token is read on the host at launch and only ever reaches the container
  through its environment: nothing is written to the per-user store or `/tmp`.
  Anything running in the jail can read it, and it shows in `docker inspect`.
- The launch is refused if the token can write to any github.com remote of the
  repo each root is in (and of every repo around that one): the read-only `.git`
  only guards local history, and `Contents: write` would let the agent push to
  or rewrite those repositories through GitHub. The launcher checks each one
  from the host with a ref update that can never succeed, and also refuses when
  GitHub's answer is inconclusive (bad token, rate limit, network error). Other
  write permissions (pull requests, workflows, issues) are currently not checked.
  - Repos *inside* a root (a root like `~/gits` that isn't itself in a repo, or
    a repo's submodules) are not looked for, so their remotes are not checked.
  - An ssh remote on any host other than github.com is not checked either:
    git doesn't expand `~/.ssh/config` aliases, so `github-work:o/r` can't be
    told apart from a non-GitHub host. The launch warns about each one; set
    `gh_warn_ssh_aliases` to `false` to silence the warning.
  - **This is just a safeguard against one possible misconfig, not a security feature.**
- The egress policy must allow `github.com` and `api.github.com`.

```sh
claude-jail --gh-token-file ~/.config/claude-jail/tokens/myproject
```

## "Why `claude-jail` instead of OpenShell?"

I believe that one should not rely on an alpha sandboxing tool that requires a mermaid diagram to understand. On another note, back-and-forth interactions (the main selling point of Claude Code) with OpenShell is awkward.

It's probably a very good tool for many people, but for this use case, I'd prefer some
good old Python puppeting a sandbox which, if broken, would be a good candidate for the CVE of the decade.
