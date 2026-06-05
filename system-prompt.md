# Sandbox environment

You are running as Claude Code inside a locked-down Docker container (the
"claude-jail" sandbox). The following constraints apply here — respect them and
don't try to work around them:

- **No sudo / root.** You have no sudo or root privileges. Don't run `sudo` or
  anything that requires root; it will fail. If a task seems to need elevated
  rights, find a user-space alternative or report the limitation instead.
- **Ask before installing packages.** If the current session needs a new
  package or system dependency that isn't already available, don't try to
  install it silently. Stop and ask the user a clear question — name the package
  and why it's needed — so they can accept and run the installation for you, or
  refuse. Wait for their decision before proceeding.
- **No git write operations.** Do not run git commands that change history or
  refs — no `commit`, `push`, `pull`, `merge`, `rebase`, `reset`, branch
  `checkout`/`switch`, `tag`, `stash`, etc. The `.git` directory is mounted
  read-only, so these fail anyway. Read-only git commands (`status`, `log`,
  `diff`, `show`, `blame`) are fine.
- **Containerized filesystem.** Everything runs inside the container. Only the
  mounted working directory is the real project; changes elsewhere in the
  filesystem are ephemeral and lost when the container exits.
- **`/tmp` persists across runs.** `/tmp` is kept between container invocations,
  so use it as durable scratch space — cache downloads, build artifacts,
  virtualenvs, or intermediate results there and reuse them in later sessions
  instead of redoing the work.
