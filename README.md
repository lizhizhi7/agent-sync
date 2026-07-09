# claude-sync

Sync your [Claude Code](https://docs.anthropic.com/en/docs/claude-code) config and project memory across devices.

Claude Code keeps settings, custom skills, MCP servers, and per-project memory locally under `~/.claude/`. `claude-sync` keeps the syncable subset in a private local data directory, links that directory back into `~/.claude/`, and can move the data through git, S3, GCS, or a local-only backend.

## What Gets Synced

| Item | Synced |
|---|---|
| `settings.json` | Yes |
| `keybindings.json` | Yes |
| Global `CLAUDE.md` | Yes |
| Custom skills, `skills/` | Yes |
| Custom MCP server code, `mcp-servers/` | Yes, except device-local `.venv/` |
| Project memory, `projects/*/memory/` | Yes |
| Conversation logs, cache, sessions, telemetry | No |

## How It Works

Claude Code derives project directory names in `~/.claude/projects/` from absolute paths:

```text
~/.claude/projects/-Users-alice-dev-myapp/       # macOS
~/.claude/projects/-home-alice-dev-myapp/        # Linux
```

Those names differ across devices because `$HOME` differs. `claude-sync` stores each project under a canonical name in the data directory, then creates device-specific symlinks on each machine.

For example:

```text
$CLAUDE_SYNC_DIR/projects/-myapp/memory/
~/.claude/projects/-Users-alice-dev-myapp/memory -> $CLAUDE_SYNC_DIR/projects/-myapp/memory
```

Top-level config files, skills, and MCP server source files are also symlinked from `~/.claude/` into the data directory. Existing local files are backed up under `~/.claude/backups/claude-sync-*` before being replaced by symlinks.

## Install

One-line installer:

```bash
curl -fsSL https://raw.githubusercontent.com/lizhizhi7/claude-sync/main/install.sh | bash
```

Or clone manually:

```bash
git clone https://github.com/lizhizhi7/claude-sync.git ~/.local/share/claude-sync
ln -sfn ~/.local/share/claude-sync/bin/claude-sync ~/.local/bin/claude-sync
```

Requires Bash and the CLI for your chosen backend:

| Backend | Required CLI |
|---|---|
| `git` | `git` |
| `s3` | AWS CLI, `aws` |
| `gcs` | Google Cloud CLI, `gcloud` |
| `local` | none |

## Quick Start: Git Backend

Create a private git repo for your config data. It can contain settings, project memory, and secrets from MCP server configuration, so do not make it public.

```bash
claude-sync init ~/dotfiles/claude-config
export CLAUDE_SYNC_DIR=~/dotfiles/claude-config
git -C ~/dotfiles/claude-config remote add origin git@github.com:you/claude-config.git
claude-sync link
claude-sync sync
```

On another device:

```bash
git clone git@github.com:you/claude-config.git ~/dotfiles/claude-config
export CLAUDE_SYNC_DIR=~/dotfiles/claude-config
claude-sync pull
```

## Object Storage Backends

Object storage backends keep the same local data directory, but store its syncable content as one archive object. This avoids broad file syncing with fragile include/exclude rules.

S3:

```bash
export CLAUDE_SYNC_BACKEND=s3
export CLAUDE_SYNC_STORAGE_URI=s3://my-private-bucket/claude-sync
export CLAUDE_SYNC_DIR=~/dotfiles/claude-config
claude-sync init "$CLAUDE_SYNC_DIR"
claude-sync sync
```

GCS:

```bash
export CLAUDE_SYNC_BACKEND=gcs
export CLAUDE_SYNC_STORAGE_URI=gs://my-private-bucket/claude-sync
export CLAUDE_SYNC_DIR=~/dotfiles/claude-config
claude-sync init "$CLAUDE_SYNC_DIR"
claude-sync sync
```

By default, the archive object is named `claude-sync-data.tar.gz`. Override it with `CLAUDE_SYNC_STORAGE_OBJECT`.

Object storage has no merge/conflict resolution. Treat one device as the active writer, or use explicit `pull` before editing and `push` afterward.

## Commands

```text
claude-sync init [dir]   initialize a data directory
claude-sync              sync through the configured backend + link
claude-sync push         push local data to the backend
claude-sync pull         pull backend data + link
claude-sync link         set up or refresh symlinks
claude-sync unlink       remove data-directory-owned symlinks only
claude-sync clean        remove broken symlinks from ~/.claude/projects
claude-sync status       show backend, projects, link health, and changes
claude-sync help         show usage
```

## Configuration

| Env var | Meaning |
|---|---|
| `CLAUDE_SYNC_DIR` | Private local data directory. Required except for `init` and `help`. |
| `CLAUDE_SYNC_BACKEND` | `git`, `s3`, `gcs`, or `local`. Default: `git`. |
| `CLAUDE_SYNC_STORAGE_URI` | `s3://bucket/prefix` or `gs://bucket/prefix` for object storage. |
| `CLAUDE_SYNC_STORAGE_OBJECT` | Archive object name. Default: `claude-sync-data.tar.gz`. |
| `CLAUDE_SYNC_WORKDIR` | Parent directory under `$HOME` to strip from canonical project names. |
| `CLAUDE_SYNC_REMOTE` | Git remote name. Default: `origin`. |

### Workdir Example

If all projects live under `~/develop/`, set:

```bash
export CLAUDE_SYNC_WORKDIR=develop
```

Then `~/develop/myapp` is stored as `projects/-myapp` instead of `projects/-develop-myapp`.

## Privacy And Security

Your data directory can contain secrets and sensitive work context:

- `settings.json` may include MCP server tokens or credentials.
- Project memory may include private repository, customer, or product details.
- Custom skills and global instructions may include internal workflows.

Use a private git repo or private object storage bucket. `claude-sync` does not encrypt or redact data.

Hardening built into the tool:

- Refuses to operate on data directories without the `.claude-sync-data` marker.
- Refuses to use a data directory inside `~/.claude/`.
- Backs up existing local Claude files before replacing them with symlinks.
- Removes symlinks only when their target is actually inside the configured data directory.
- Preserves MCP server `.venv/` directories as device-local state.
- Excludes conversation logs and `.venv/` directories from git/object-storage sync.

## Development

Run checks before publishing changes:

```bash
bash -n bin/claude-sync install.sh tests/run.sh
tests/run.sh
shellcheck bin/claude-sync install.sh tests/run.sh
```

The GitHub Actions workflow runs ShellCheck on pull requests.

## Uninstall

```bash
claude-sync unlink
rm -rf ~/.local/share/claude-sync
rm ~/.local/bin/claude-sync
```

The data directory at `$CLAUDE_SYNC_DIR` is left intact.

## License

[MIT](LICENSE)
