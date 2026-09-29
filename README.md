# agent-sync

Sync your coding-agent config and project memory across devices.

Coding agents keep settings, custom skills, MCP servers, and per-project memory locally under a home directory of their own — `~/.claude/` for [Claude Code](https://docs.anthropic.com/en/docs/claude-code), `~/.codex/` for Codex. `agent-sync` keeps the syncable subset in one private local data directory, links that directory back into each agent's home, and can move the data through git, S3, GCS, or a local-only backend.

## Agents

One data directory feeds several agents. Each agent declares where its home is and which assets it accepts; anything it has no equivalent for is skipped rather than forced.

| Agent | Home | Instructions | Settings | Keybindings | Skills | MCP servers | Project memory |
|---|---|---|---|---|---|---|---|
| `claude` | `~/.claude` | `CLAUDE.md` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `tclaude` | `~/.tclaude` | `CLAUDE.md` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `codex` | `~/.codex` | `AGENTS.md` | — | — | ✅ | — | via `agent-sync memory` |

Codex keeps its settings in `config.toml` (TOML, and MCP servers are registered inside that same file rather than as a directory). Its native `~/.codex/memories/` content is generated state rather than a curated project-memory interface, so `agent-sync` leaves it device-local. The shared instruction block tells Codex to load the canonical curated memory through `agent-sync memory` instead.

Only `claude` is enabled by default. Opt the others in explicitly — linking your global instructions into another agent's home changes that agent's behavior in every session, so it is never done on autodetection alone:

```bash
export AGENT_SYNC_AGENTS=claude,tclaude,codex
agent-sync link
```

Or per invocation with `-a`:

```bash
agent-sync -a claude,codex link
```

See what is available on this machine, and what each agent would receive:

```bash
agent-sync agents
```

## What Gets Synced

The data directory holds one agent-neutral copy of each asset. It is linked into each enabled agent's home under whatever name that agent expects.

| In the data directory | Synced |
|---|---|
| `instructions.md` | Yes — as `CLAUDE.md` / `AGENTS.md` |
| `settings.json` | Yes |
| `keybindings.json` | Yes |
| `skills/` | Yes |
| `mcp-servers/` | Yes, except device-local `.venv/` |
| `projects/*/memory/` | Yes |
| Conversation logs, cache, sessions, telemetry | No |

Claude Code and tclaude share one `settings.json`. If they ever need to diverge, give each its own file rather than adding a merge layer.

## How It Works

Claude Code and tclaude derive project directory names from absolute paths:

```text
~/.claude/projects/-Users-alice-dev-myapp/       # macOS
~/.claude/projects/-home-alice-dev-myapp/        # Linux
```

Those names differ across devices because `$HOME` differs. `agent-sync` stores each project under a canonical name in the data directory, then creates device-specific symlinks on each machine:

```text
$AGENT_SYNC_DIR/projects/-myapp/memory/
~/.claude/projects/-Users-alice-dev-myapp/memory -> $AGENT_SYNC_DIR/projects/-myapp/memory
```

Both agents use the identical naming scheme, so one mapping file covers both.

Codex loads required guidance through `AGENTS.md`. Because the shared project memories may be much larger than Codex's instruction-file budget, `agent-sync` does not copy them into the global file or into Codex's generated memory store. Instead, run this inside a repository:

```bash
agent-sync memory
```

The command resolves the repository (including linked git worktrees), prints its canonical `MEMORY.md` index, and lists the paths of detail files. `agent-sync instructions install` adds a managed global instruction requiring agents without native project-memory access to do this at the start of repository work.

Top-level config files, skills, and MCP server source files are symlinked the same way. Existing local files are backed up under `<agent home>/backups/agent-sync-*` before being replaced by symlinks.

When the automatic prefix rule is not enough, use explicit project mappings. Mappings are stored in:

```text
$AGENT_SYNC_DIR/.agent-sync-projects
```

Each line maps a local project directory name to a canonical synced project:

```text
-Users-alice-develop-myapp -myapp
-home-bob-code-myapp -myapp
```

`agent-sync link` honors those mappings before using the default prefix-based project name.

## Install

One-line installer:

```bash
curl -fsSL https://raw.githubusercontent.com/lizhizhi7/agent-sync/main/install.sh | bash
```

Or clone manually:

```bash
git clone https://github.com/lizhizhi7/agent-sync.git ~/.local/share/agent-sync
ln -sfn ~/.local/share/agent-sync/bin/agent-sync ~/.local/bin/agent-sync
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
agent-sync init ~/dotfiles/agent-config
export AGENT_SYNC_DIR=~/dotfiles/agent-config
git -C ~/dotfiles/agent-config remote add origin git@github.com:you/agent-config.git
agent-sync link
agent-sync sync
```

The first `sync` against an empty remote skips the pull and just pushes, so a
brand-new private repo needs no manual first commit.

On another device:

```bash
git clone git@github.com:you/agent-config.git ~/dotfiles/agent-config
export AGENT_SYNC_DIR=~/dotfiles/agent-config
agent-sync pull
```

## Object Storage Backends

Object storage backends keep the same local data directory, but store its syncable content as one archive object. This avoids broad file syncing with fragile include/exclude rules.

S3:

```bash
export AGENT_SYNC_BACKEND=s3
export AGENT_SYNC_STORAGE_URI=s3://my-private-bucket/agent-sync
export AGENT_SYNC_DIR=~/dotfiles/agent-config
agent-sync init "$AGENT_SYNC_DIR"
agent-sync sync
```

GCS:

```bash
export AGENT_SYNC_BACKEND=gcs
export AGENT_SYNC_STORAGE_URI=gs://my-private-bucket/agent-sync
export AGENT_SYNC_DIR=~/dotfiles/agent-config
agent-sync init "$AGENT_SYNC_DIR"
agent-sync sync
```

By default, the archive object is named `agent-sync-data.tar.gz`. Override it with `AGENT_SYNC_STORAGE_OBJECT`.

Object storage has no merge/conflict resolution. Treat one device as the active writer, or use explicit `pull` before editing and `push` afterward.

## Commands

```text
agent-sync init [dir]    initialize a data directory
agent-sync               sync through the configured backend + link
agent-sync push          push local data to the backend
agent-sync pull          pull backend data + link
agent-sync link          set up or refresh symlinks for every enabled agent
agent-sync agents        list known agents, homes, and the assets they accept
agent-sync memory [dir]  print a repository's canonical memory index
agent-sync match         preview, apply, or set project mappings
agent-sync instructions  install/remove/status the managed instruction block
agent-sync env           install/remove/status/print managed shell env block
agent-sync skill         list/add/diff/update skills vendored from other repos
agent-sync unlink        remove data-directory-owned symlinks only
agent-sync clean         remove broken project memory symlinks
agent-sync status        show backend, agents, projects, link health, and changes
agent-sync help          show usage
```

Every command also accepts `-p PREFIX` / `--prefix` to override the
auto-detected device prefix (derived from `$HOME`), `-w DIR` / `--workdir`
as a one-off alternative to `AGENT_SYNC_WORKDIR`, and `-a LIST` / `--agents`
as a one-off alternative to `AGENT_SYNC_AGENTS`.

`link`, `unlink`, `clean`, `match`, and `status` act only on enabled agents.
Anything not in the enabled list is left untouched.

## Project Matching

Use `match` when the same project has different local directory names across machines, or when you want to map a long local path-derived name to a shorter canonical project. It scans every enabled agent that keeps project memory.

Preview suggested mappings without changing anything:

```bash
agent-sync match --dry-run
```

Run interactively (prompts per project; previews automatically when not attached to a terminal):

```bash
agent-sync match
```

Apply all suggestions from the current machine:

```bash
agent-sync match --auto
```

Apply one explicit mapping:

```bash
agent-sync match -Users-alice-develop-myapp:-myapp
```

`match` copies local memory into the canonical project with no clobbering, writes the mapping file, and replaces the local memory directory with a symlink to the canonical memory directory — in every enabled agent's home.

## Agent Instructions

`agent-sync` can merge a managed block into the shared instructions file, reminding agents to use `agent-sync` for durable config and memory changes. Because that one file is linked into every enabled agent's home, the block reaches all of them at once.

```bash
agent-sync instructions install   # write or update the block
agent-sync instructions status    # check what is installed
agent-sync instructions remove    # remove only the managed block
```

The block lives in `$AGENT_SYNC_DIR/instructions.md`; the rest of that file is left alone.

## Shell Env Block

`agent-sync env install` writes the current sync configuration to a marked block in your default shell profile. `remove` deletes only that marked block.

```bash
agent-sync env install
agent-sync env status
agent-sync env remove
```

To inspect the block without editing files:

```bash
agent-sync env print
```

## Vendored Skills

A skill you copy from another team's repository or npm package should stay
updatable after you change it. `agent-sync skill` keeps that record:

```bash
agent-sync skill add ci-helper https://git.example.com/team/skills.git ci-helper   # <name> <source> [path] [ref]
agent-sync skill add helper npm:@team/helper-skill                   # npm: sources use `npm pack`
agent-sync skill list                  # every vendored skill, its source and pinned ref
agent-sync skill diff ci-helper        # what we changed, against the pinned upstream
agent-sync skill update ci-helper [ref] # move to a new upstream ref (default: latest)
```

`add` copies the skill directory (the one holding `SKILL.md`, minus any
subdirectory holding its own `SKILL.md`: that is another skill) and writes
`skills/<name>/.upstream` with its source, path, and the exact ref it resolved
to. Edit the vendored files in place, as you would your own skill. `update`
merges each file three ways (base = the pinned upstream, ours = your
directory, theirs = the new upstream): files you never touched take upstream's
version, your edits survive, and a clash is left with conflict markers and a
non-zero exit instead of being silently overwritten. Files upstream never had
(your notes, sidecars) are left alone. Review with `git diff`, then
`agent-sync sync`.

Prefer adapting a vendored skill from outside (environment variables, a
sidecar file of your own) over editing its files: every edit is a potential
conflict on the next update. Some skills keep a local token in their own
directory (often `config.json`); ignore that path in the data directory's
`.gitignore` so it never syncs.


| Env var | Meaning |
|---|---|
| `AGENT_SYNC_DIR` | Private local data directory. Required except for `init` and `help`. |
| `AGENT_SYNC_AGENTS` | Comma-separated agents to link: `claude`, `tclaude`, `codex`. Default: `claude`. |
| `AGENT_SYNC_BACKEND` | `git`, `s3`, `gcs`, or `local`. Default: `git`. |
| `AGENT_SYNC_STORAGE_URI` | `s3://bucket/prefix` or `gs://bucket/prefix` for object storage. |
| `AGENT_SYNC_STORAGE_OBJECT` | Archive object name. Default: `agent-sync-data.tar.gz`. |
| `AGENT_SYNC_WORKDIR` | Parent directory under `$HOME` to strip from canonical project names. |
| `AGENT_SYNC_REMOTE` | Git remote name. Default: `origin`. |

### Workdir Example

If all projects live under `~/develop/`, set:

```bash
export AGENT_SYNC_WORKDIR=develop
```

Then `~/develop/myapp` is stored as `projects/-myapp` instead of `projects/-develop-myapp`.

## Privacy And Security

Your data directory can contain secrets and sensitive work context:

- `settings.json` may include MCP server tokens or credentials.
- Project memory may include private repository, customer, or product details.
- Custom skills and global instructions may include internal workflows.

Use a private git repo or private object storage bucket. `agent-sync` does not encrypt or redact data.

Hardening built into the tool:

- Refuses to operate on data directories without the `.agent-sync-data` marker.
- Refuses to use a data directory inside any known agent home.
- Rejects unknown agent names before touching anything.
- For the git backend, refuses to run when the data directory is nested inside
  another git repository (so `git add -A` can never stage files of an
  unrelated enclosing repo) or sitting on a detached HEAD.
- Backs up existing local agent files before replacing them with symlinks.
- Removes symlinks only when their target is actually inside the configured data directory.
- Preserves MCP server `.venv/` directories as device-local state.
- Excludes conversation logs and `.venv/` directories from git/object-storage sync.
- Validates object-storage archives before extraction: absolute paths, `..`
  traversal, symlink members, and hardlink members are all rejected.

## Uninstall

```bash
agent-sync unlink               # remove managed symlinks for enabled agents
agent-sync unlink --env         # also remove managed shell env blocks
agent-sync instructions remove  # remove the managed instruction block
rm -rf ~/.local/share/agent-sync
rm ~/.local/bin/agent-sync
```

The data directory at `$AGENT_SYNC_DIR` is left intact.

## License

[MIT](LICENSE)
