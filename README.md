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

When the automatic prefix rule is not enough, use explicit project mappings. Mappings are stored in:

```text
$CLAUDE_SYNC_DIR/.claude-sync-projects
```

Each line maps a local Claude project directory name to a canonical synced project:

```text
-Users-alice-develop-myapp -myapp
-home-bob-code-myapp -myapp
```

`claude-sync link` honors those mappings before using the default prefix-based project name.

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

Requires Bash 3.2+ (the macOS system bash works) and the CLI for your chosen backend:

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

The first `sync` against an empty remote skips the pull and just pushes, so a
brand-new private repo needs no manual first commit.

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
claude-sync match        preview, apply, or set project mappings
claude-sync agents       install/remove/status managed agent instructions
claude-sync env          install/remove/status/print managed shell env block
claude-sync unlink       remove data-directory-owned symlinks only
claude-sync clean        remove broken symlinks from ~/.claude/projects
claude-sync status       show backend, projects, link health, and changes
claude-sync help         show usage
```

## Project Matching

Use `match` when the same project has different local Claude directory names across machines, or when you want to map a long local path-derived name to a shorter canonical project.

Preview suggested mappings without changing anything:

```bash
claude-sync match --dry-run
```

Run interactively (prompts per project; previews automatically when not attached to a terminal):

```bash
claude-sync match
```

Apply all suggestions from the current machine:

```bash
claude-sync match --auto
```

Apply one explicit mapping:

```bash
claude-sync match -Users-alice-develop-myapp:-myapp
```

`match` copies local memory into the canonical project with no clobbering, writes the mapping file, and replaces the local memory directory with a symlink to the canonical memory directory.

## Agent Instructions

`claude-sync` can detect supported agent tools and merge a managed instruction block that reminds them to use `claude-sync` for durable config and memory changes.

Supported targets:

| Tool | Instruction target |
|---|---|
| Claude Code | `$CLAUDE_SYNC_DIR/CLAUDE.md` |
| Codex | `~/.codex/AGENTS.md` |

Install or update the managed instruction block:

```bash
claude-sync agents install
```

Check what is installed:

```bash
claude-sync agents status
```

Remove only the managed instruction block:

```bash
claude-sync agents remove
```

## Shell Env Block

`claude-sync env install` writes the current sync configuration to a marked block in your default shell profile. `remove` deletes only that marked block.

```bash
claude-sync env install
claude-sync env status
claude-sync env remove
```

To inspect the block without editing files:

```bash
claude-sync env print
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
- For the git backend, refuses to run when the data directory is nested inside
  another git repository (so `git add -A` can never stage files of an
  unrelated enclosing repo) or sitting on a detached HEAD.
- Backs up existing local Claude files before replacing them with symlinks.
- Removes symlinks only when their target is actually inside the configured data directory.
- Preserves MCP server `.venv/` directories as device-local state.
- Excludes conversation logs and `.venv/` directories from git/object-storage sync.
- Validates object-storage archives before extraction: absolute paths, `..`
  traversal, symlink members, and hardlink members are all rejected.

## Uninstall

```bash
claude-sync unlink        # remove managed symlinks only
claude-sync unlink --env  # also remove managed shell env blocks
claude-sync agents remove # remove managed agent instructions
rm -rf ~/.local/share/claude-sync
rm ~/.local/bin/claude-sync
```

The data directory at `$CLAUDE_SYNC_DIR` is left intact.

## License

[MIT](LICENSE)
