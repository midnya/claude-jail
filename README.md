# claude-jail

Run Claude Code inside a locked-down Docker container.  
Requires Docker v23+ (I think?) and Compose v2 (the plugin invoked via `docker compose`, not the binary `docker-compose`).  
Still in active development; bug reports, feature requests, and PRs are most welcome!

## AI usage disclosure

`claude-jail` has partly been built with `Claude` itself. Its .git has always been quarantined.

## Security model (so far)

- Filesystem protection: bypasses Claude's sandbox feature and relies on Docker's instead.
  - For convenience, `/tmp` is a persisted volume that is shared per-workspace,
    between jail instances.
- Network isolation: the jail container sits on an `internal` Docker network.
  All egress is forced through a [Squid](https://www.squid-cache.org/)
  proxy.
 - (For now) the proxy applies no destination ACLs; it allows all requests
    and logs them. The logs persist in a per-jail volume. Future work will
    allow ACLs.

## Invocation

Everything goes through `claude-jail`:

```sh
./claude-jail [--user <name>] [--config <path>] <directory> [docker compose args...]
```

- `--user <name>` (`-u`): a config namespace. Claude's config and credentials
  persist on the host in `~/.claude-jail-<name>/` and `~/.claude-jail-<name>.json`,
  created on first run. Use different names to keep separate identities/logins.
- `--config <path>` (`-c`): the jail config file. Defaults to
  `<directory>/.claude-jail.json`.
- `<directory>`: the path to jail; bind-mounted in read-write at `/workspace/<directory>`.
- `[docker compose args...]`: forwarded verbatim to `docker compose`.

## Commands

Build the image (needed once, or when you wish to update the claude package):

```sh
./claude-jail --user me ~/code/myproject build --no-cache
```

Start an interactive session:

```sh
./claude-jail --user me ~/code/myproject run --rm claude-jail
```

Any extra args after the service name are passed straight through to `claude`:

```sh
./claude-jail --user me ~/code/myproject run --rm claude-jail --help
./claude-jail --user me ~/code/myproject run --rm claude-jail -p "summarize this repo"
```

With a `user` key set in `.claude-jail.json`, the `--user` flag can be omitted:

```sh
./claude-jail ~/code/myproject run --rm claude-jail
```

## Configuration

Config lives in `.claude-jail.json` at the root of the jailed project, or
wherever you point `--config` (`-c`).

Example:

```json
{
  "user": "me",
  "read_only": ["config/secrets.yml", "production/"],
  "hidden": [".env", "private/notes"],
  "default_mode": "plan",
  "system_prompt": { "path": "CLAUDE_JAIL_PROMPT.md" }
}
```

Available keys:
- `user`: the config namespace. The `--user` flag overrides it; one of the two must be set.
- `read_only`: bind-mounted read-only. Visible inside the jail but writes
  fail at the filesystem level.
- `hidden`: contents masked to empty. A hidden directory mounts as an empty
  read-only volume; a hidden file is masked with a read-only empty file.
- `default_mode`: the permission mode Claude starts in, forwarded to
  `claude --permission-mode`.
- `system_prompt`: an extra system prompt, appended to the jail's built-in one.
  A segment is either inline text or a file path, and you may pass one or a list of them:
  - inline: `"system_prompt": "Prefer pnpm over npm in this repo."`
  - file: `"system_prompt": { "path": "CLAUDE_PROMPT.md" }`
  - list (segments joined with a blank line, in order):
    `"system_prompt": ["Prefer pnpm over npm.", { "path": "CLAUDE_PROMPT.md" }]`



Notes:
- `user` must be a bare word (a letter, then letters/digits/`-`/`_`).
- `.git` is always read-only.
- An active config file inside the jail (the default `.claude-jail.json`, or a
  `--config` path under the jail) is hidden inside the container.
- A `--config` path outside the jail is never mounted at all; the agent
  doesn't see it.
- A path that names something inside the jail but escapes via a
  symlink is a hard error.
- A `path` file is read on the host at launch (its text is injected into the
  prompt, never mounted). For a config inside the jail the path is jail-relative
  and must stay inside the jail; for a `--config` outside the jail it is read
  relative to that config file and may live anywhere.
- Paths must be relative and stay inside the jail.
- When a path is listed under both keys, `hidden` wins.
- A missing `read_only` path is skipped; a missing `hidden` path is a hard error.
- A missing or empty `system_prompt.path` file is a hard error.
- An explicit `--permission-mode` on the command line wins.