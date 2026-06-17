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
claude-jail [--user <name>] [--config <path>] [docker compose args...]
```

- `--user <name>` (`-u`): a config namespace. Claude's config and credentials
  persist on the host in `~/.claude-jail-<name>/` and `~/.claude-jail-<name>.json`,
  created on first run. Use different names to keep separate identities/logins.
- `--config <path>` (`-c`): the jail config file. Defaults to
  `./.claude-jail.json`.
- `[docker compose args...]`: forwarded verbatim to `docker compose`. Use `--` to explicitely pass the rest of the command to compose.

## Commands

Run from the directory that holds your `.claude-jail.json` (or point `-c` at it).

Build the image (needed once, or when you wish to update the claude package):

```sh
cd ~/code/myproject
claude-jail --user me build --no-cache
```

Start an interactive session:

```sh
claude-jail --user me run --rm claude-jail
```

Any extra args after the service name are passed straight through to `claude`:

```sh
claude-jail --user me run --rm claude-jail --help
claude-jail --user me run --rm claude-jail -p "summarize this repo"
```

With a `user` key set in the config, the `--user` flag can be omitted.

`-c` lets you launch from anywhere:

```sh
claude-jail -c ~/code/myproject/.claude-jail.json run --rm claude-jail
```

## Configuration

It defines the jail's **roots**, the directories
bind-mounted read-write, each with its own filesystem rules.

Example:

```json
{
  "user": "me",
  "default_mode": "plan",
  "system_prompts": { "path": "CLAUDE_JAIL_PROMPT.md" },
  "roots": [
    {
      "path": ".",
      "read_only": ["config/secrets.yml", "production/"],
      "hidden": [".env", "private/notes"]
    },
    "../shared-lib"
  ]
}
```

Available keys:
- `user`: the config namespace. The `--user` flag overrides it; one of the two must be set.
- `roots`: the directories to jail, each bind-mounted read-write at
  `/workspace/<abs path>`. A list whose entries are either a string path or an
  object `{ "path", "read_only", "hidden" }`. A relative `path` is resolved
  against the config file's directory; an absolute one is taken as-is. Omit
  `roots` to jail the config file's own directory.
  - The session starts in the config file's directory, mounted under `/workspace`.
    Should the config's directory not be part of the roots, that directory will be empty.
  - `read_only` (per root): paths relative to that root, bind-mounted read-only.
    Visible inside the jail but writes fail at the filesystem level.
  - `hidden` (per root): paths relative to that root, masked to empty. A hidden
    directory mounts as an empty read-only volume; a hidden file is masked with
    a read-only empty file.
- `default_mode`: the permission mode Claude starts in, forwarded to
  `claude --permission-mode`.
- `system_prompts`: an extra system prompt, appended to the jail's built-in one.
  A segment is either inline text or a file path, and you may pass one or a list of them:
  - inline: `"system_prompts": "Prefer pnpm over npm in this repo."`
  - file: `"system_prompts": { "path": "CLAUDE_PROMPT.md" }`
  - list (segments joined with a blank line, in order):
    `"system_prompts": ["Prefer pnpm over npm.", { "path": "CLAUDE_PROMPT.md" }]`

Notes:
- `user` must be a bare word (a letter, then letters/digits/`-`/`_`).
- `.git` in every root is always read-only.
- Each root must be an existing directory; roots may not be nested in or
  duplicate one another, nor be the filesystem root `/`, your home directory,
  or a directory containing it.
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
- A missing `read_only` path is skipped; a missing `hidden` path is a hard error.
- A missing or empty `system_prompts.path` file is a hard error.
- An explicit `--permission-mode` on the command line wins.
