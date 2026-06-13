# claude-jail

Run Claude Code inside a locked-down Docker container.  
Requires Docker v23+ (I think?) and Compose v2 (the plugin invoked via `docker compose`, not the binary `docker-compose`).  
Bug reports, feature requests, and PRs are most welcome!

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

Everything goes through `run.sh`:

```sh
./run.sh <directory> <user> [docker compose args...]
```

- `<directory>`: the path to jail; bind-mounted in read-write at `/workspace/<directory>`.
- `<user>`: a config namespace. Claude's config and credentials persist on
  the host in `~/.claude-jail-<user>/` and `~/.claude-jail-<user>.json`, created
  on first run. Use different names to keep separate identities/logins.
- `[docker compose args...]`: forwarded verbatim to `docker compose`.

## Commands

Build the image (needed once, or when you wish to update the claude package):

```sh
./run.sh ~/code/myproject me build --no-cache
```

Start an interactive session:

```sh
./run.sh ~/code/myproject me run --rm claude-jail
```

Any extra args after the service name are passed straight through to `claude`:

```sh
./run.sh ~/code/myproject me run --rm claude-jail --help
./run.sh ~/code/myproject me run --rm claude-jail -p "summarize this repo"
```

## Configuration

Config lives in `.claude-jail.json`, placed at the root of the jailed project.

Example:

```json
{
  "read_only": ["config/secrets.yml", "production/"],
  "hidden": [".env", "private/notes"]
}
```

Available keys:
- `read_only`: bind-mounted read-only. Visible inside the jail but writes
  fail at the filesystem level.
- `hidden`: contents masked to empty. A hidden directory mounts as an empty
  read-only volume; a hidden file is masked with a read-only empty file.

Notes:
- `.git` and `.claude-jail.json` itself are always read-only.
- Paths must be relative and stay inside the jail (no absolute paths, no `..`).
- When a path is listed under both keys, `hidden` wins.
- A missing `read_only` path is skipped; a missing `hidden` path is a hard error.
