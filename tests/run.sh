#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT/bin/agent-sync"

# The developer's own AGENT_SYNC_* environment must not reach the tests: an
# exported AGENT_SYNC_AGENTS silently defeats every default-is-claude-only
# assertion. Tests that need a variable set it themselves, per invocation.
unset AGENT_SYNC_AGENTS AGENT_SYNC_DIR AGENT_SYNC_BACKEND AGENT_SYNC_REMOTE
unset AGENT_SYNC_STORAGE_OBJECT AGENT_SYNC_STORAGE_URI AGENT_SYNC_WORKDIR

pass_count=0
tmp_dirs=()

agent_sync() {
    env \
        -u AGENT_SYNC_REMOTE \
        -u AGENT_SYNC_STORAGE_OBJECT \
        -u AGENT_SYNC_STORAGE_URI \
        -u AGENT_SYNC_WORKDIR \
        "$BIN" "$@"
}

cleanup() {
    local dir
    for dir in "${tmp_dirs[@]:-}"; do
        rm -rf "$dir"
    done
}
trap cleanup EXIT

new_tmp() {
    local dir
    dir="$(mktemp -d)"
    dir="$(cd "$dir" && pwd -P)"
    tmp_dirs+=("$dir")
    printf '%s\n' "$dir"
}

run_test() {
    local name="$1"
    shift
    printf 'test: %s\n' "$name"
    "$@"
    pass_count=$((pass_count + 1))
}

assert_file() {
    [ -f "$1" ] || { echo "expected file: $1" >&2; exit 1; }
}

assert_dir() {
    [ -d "$1" ] || { echo "expected directory: $1" >&2; exit 1; }
}

assert_symlink_to() {
    local link="$1" target="$2" dest
    [ -L "$link" ] || { echo "expected symlink: $link" >&2; exit 1; }
    dest="$(readlink "$link")"
    [ "$dest" = "$target" ] || {
        echo "expected $link -> $target, got $dest" >&2
        exit 1
    }
}

test_init_local_backend() {
    local tmp home data
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    mkdir -p "$home"

    HOME="$home" AGENT_SYNC_BACKEND=local agent_sync init "$data" >/dev/null

    assert_file "$data/.agent-sync-data"
    assert_file "$data/.gitignore"
    grep -Fqx '!.agent-sync-projects' "$data/.gitignore" || { echo "mapping file is not allowlisted" >&2; exit 1; }
    [ ! -d "$data/.git" ] || { echo "local backend should not initialize git" >&2; exit 1; }
}

test_link_backs_up_existing_config() {
    local tmp home data backup_count
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    mkdir -p "$home/.claude" "$data"
    touch "$data/.agent-sync-data"
    printf '{"repo":true}\n' > "$data/settings.json"
    printf '{"local":true}\n' > "$home/.claude/settings.json"

    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local agent_sync link >/dev/null

    assert_symlink_to "$home/.claude/settings.json" "$data/settings.json"
    backup_count="$(find "$home/.claude/backups" -name settings.json -type f | wc -l | tr -d ' ')"
    [ "$backup_count" = "1" ] || { echo "expected one settings backup, got $backup_count" >&2; exit 1; }
}

test_migrates_dotfiles_in_project_memory() {
    local tmp home data prefix canonical
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    prefix="$(printf '%s' "$home" | tr '/' '-')"
    canonical="-project"
    mkdir -p "$home/.claude/projects/$prefix$canonical/memory" "$data"
    touch "$data/.agent-sync-data"
    printf 'visible\n' > "$home/.claude/projects/$prefix$canonical/memory/file.md"
    printf 'hidden\n' > "$home/.claude/projects/$prefix$canonical/memory/.hidden"

    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local agent_sync link >/dev/null

    assert_file "$data/projects/$canonical/memory/file.md"
    assert_file "$data/projects/$canonical/memory/.hidden"
    assert_symlink_to "$home/.claude/projects/$prefix$canonical/memory" "$data/projects/$canonical/memory"
}

test_unlink_uses_path_boundary() {
    local tmp home data sibling
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    sibling="$tmp/data-sibling"
    mkdir -p "$home/.claude" "$data" "$sibling"
    touch "$data/.agent-sync-data"
    printf '{}\n' > "$data/settings.json"
    printf '{}\n' > "$sibling/settings.json"
    ln -s "$sibling/settings.json" "$home/.claude/settings.json"

    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local agent_sync unlink >/dev/null

    assert_symlink_to "$home/.claude/settings.json" "$sibling/settings.json"
}

test_rejects_unmarked_data_dir() {
    local tmp home data
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    mkdir -p "$home" "$data"

    if HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local agent_sync status >/dev/null 2>&1; then
        echo "expected unmarked data dir to fail" >&2
        exit 1
    fi
}

test_s3_push_uses_allowlisted_archive() {
    local tmp home data fake_bin archive listing
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    fake_bin="$tmp/bin"
    archive="$tmp/archive.tar.gz"
    mkdir -p "$home" "$data/mcp-servers/server/.venv" "$data/projects/-app/memory" "$fake_bin"
    touch "$data/.agent-sync-data"
    printf -- '-local-app -app\n' > "$data/.agent-sync-projects"
    printf '{}\n' > "$data/settings.json"
    printf 'do not upload\n' > "$data/extra-secret.txt"
    printf 'venv secret\n' > "$data/mcp-servers/server/.venv/secret.txt"
    printf 'conversation\n' > "$data/projects/-app/session.jsonl"
    printf 'memory\n' > "$data/projects/-app/memory/note.md"
    cat > "$fake_bin/aws" <<'AWS'
#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "s3" ] && [ "$2" = "cp" ]; then
    cp "$3" "$AWS_FAKE_OBJECT"
    exit 0
fi
echo "unexpected aws call: $*" >&2
exit 1
AWS
    chmod +x "$fake_bin/aws"

    PATH="$fake_bin:$PATH" \
        AWS_FAKE_OBJECT="$archive" \
        HOME="$home" \
        AGENT_SYNC_DIR="$data" \
        AGENT_SYNC_BACKEND=s3 \
        AGENT_SYNC_STORAGE_URI=s3://bucket/prefix \
        AGENT_SYNC_WORKDIR='' \
        "$BIN" push >/dev/null

    listing="$(tar -tzf "$archive")"
    [[ "$listing" == *"./settings.json"* ]] || { echo "settings.json missing from archive" >&2; exit 1; }
    [[ "$listing" == *"./.agent-sync-projects"* ]] || { echo "mapping file missing from archive" >&2; exit 1; }
    [[ "$listing" == *"./projects/-app/memory/note.md"* ]] || { echo "memory note missing from archive" >&2; exit 1; }
    [[ "$listing" != *"extra-secret.txt"* ]] || { echo "extra file leaked into archive" >&2; exit 1; }
    [[ "$listing" != *".venv"* ]] || { echo ".venv leaked into archive" >&2; exit 1; }
    [[ "$listing" != *".jsonl"* ]] || { echo "jsonl leaked into archive" >&2; exit 1; }
}

test_s3_pull_rejects_symlink_archive() {
    local tmp home data fake_bin remote_src archive
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    fake_bin="$tmp/bin"
    remote_src="$tmp/remote-src"
    archive="$tmp/archive.tar.gz"
    mkdir -p "$home" "$data" "$fake_bin" "$remote_src"
    touch "$data/.agent-sync-data" "$remote_src/.agent-sync-data"
    printf 'keep\n' > "$data/settings.json"
    ln -s /etc/passwd "$remote_src/settings.json"
    tar -czf "$archive" -C "$remote_src" .
    cat > "$fake_bin/aws" <<'AWS'
#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "s3" ] && [ "$2" = "ls" ]; then
    exit 0
fi
if [ "$1" = "s3" ] && [ "$2" = "cp" ]; then
    cp "$AWS_FAKE_OBJECT" "$4"
    exit 0
fi
echo "unexpected aws call: $*" >&2
exit 1
AWS
    chmod +x "$fake_bin/aws"

    if PATH="$fake_bin:$PATH" \
        AWS_FAKE_OBJECT="$archive" \
        HOME="$home" \
        AGENT_SYNC_DIR="$data" \
        AGENT_SYNC_BACKEND=s3 \
        AGENT_SYNC_STORAGE_URI=s3://bucket/prefix \
        AGENT_SYNC_WORKDIR='' \
        "$BIN" pull >/dev/null 2>&1; then
        echo "expected symlink archive pull to fail" >&2
        exit 1
    fi
    [ ! -L "$data/settings.json" ] || { echo "local settings became a symlink" >&2; exit 1; }
    grep -qx 'keep' "$data/settings.json" || { echo "local settings was modified" >&2; exit 1; }
}

test_help_runs_without_any_env() {
    env \
        -u AGENT_SYNC_DIR \
        -u AGENT_SYNC_BACKEND \
        -u AGENT_SYNC_REMOTE \
        -u AGENT_SYNC_STORAGE_OBJECT \
        -u AGENT_SYNC_STORAGE_URI \
        -u AGENT_SYNC_WORKDIR \
        "$BIN" help >/dev/null
}

test_git_backend_rejects_nested_data_dir() {
    local tmp home outer data
    tmp="$(new_tmp)"
    home="$tmp/home"
    outer="$tmp/outer"
    data="$outer/data"
    mkdir -p "$home" "$data"
    git -C "$outer" init -q
    touch "$data/.agent-sync-data"
    printf 'leak\n' > "$outer/unrelated.txt"

    if HOME="$home" AGENT_SYNC_DIR="$data" agent_sync push >/dev/null 2>&1; then
        echo "expected push from nested data dir to fail" >&2
        exit 1
    fi
    git -C "$outer" diff --cached --quiet || { echo "outer repo was staged by agent-sync" >&2; exit 1; }
}

test_mcp_server_links_preserve_venv_and_prune_stale() {
    local tmp home data target
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    target="$home/.claude/mcp-servers/server"
    mkdir -p "$home" "$data/mcp-servers/server" "$target/.venv/bin"
    touch "$data/.agent-sync-data"
    printf 'code\n' > "$data/mcp-servers/server/server.py"
    printf 'SECRET=1\n' > "$data/mcp-servers/server/.env"
    printf 'venv\n' > "$target/.venv/bin/python"

    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local agent_sync link >/dev/null

    assert_symlink_to "$target/server.py" "$data/mcp-servers/server/server.py"
    assert_symlink_to "$target/.env" "$data/mcp-servers/server/.env"
    if [ ! -d "$target/.venv" ] || [ -L "$target/.venv" ]; then
        echo ".venv was not preserved as a real dir" >&2
        exit 1
    fi
    assert_file "$target/.venv/bin/python"

    rm "$data/mcp-servers/server/server.py"
    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local agent_sync link >/dev/null

    [ ! -L "$target/server.py" ] || { echo "stale mcp-server link was not pruned" >&2; exit 1; }
    assert_file "$target/.venv/bin/python"
}

test_s3_pull_rejects_hardlink_archive() {
    local tmp home data fake_bin remote_src archive
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    fake_bin="$tmp/bin"
    remote_src="$tmp/remote-src"
    archive="$tmp/archive.tar.gz"
    mkdir -p "$home" "$data" "$fake_bin" "$remote_src"
    touch "$data/.agent-sync-data" "$remote_src/.agent-sync-data"
    printf 'keep\n' > "$data/settings.json"
    printf 'a\n' > "$remote_src/settings.json"
    ln "$remote_src/settings.json" "$remote_src/instructions.md"
    tar -czf "$archive" -C "$remote_src" .
    cat > "$fake_bin/aws" <<'AWS'
#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "s3" ] && [ "$2" = "ls" ]; then
    exit 0
fi
if [ "$1" = "s3" ] && [ "$2" = "cp" ]; then
    cp "$AWS_FAKE_OBJECT" "$4"
    exit 0
fi
echo "unexpected aws call: $*" >&2
exit 1
AWS
    chmod +x "$fake_bin/aws"

    if PATH="$fake_bin:$PATH" \
        AWS_FAKE_OBJECT="$archive" \
        HOME="$home" \
        AGENT_SYNC_DIR="$data" \
        AGENT_SYNC_BACKEND=s3 \
        AGENT_SYNC_STORAGE_URI=s3://bucket/prefix \
        "$BIN" pull >/dev/null 2>&1; then
        echo "expected hardlink archive pull to fail" >&2
        exit 1
    fi
    grep -qx 'keep' "$data/settings.json" || { echo "local settings was modified" >&2; exit 1; }
}

test_match_preview_does_not_modify() {
    local tmp home data prefix local_name output
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    prefix="$(printf '%s' "$home" | tr '/' '-')"
    local_name="$prefix-app"
    mkdir -p "$home/.claude/projects/$local_name/memory" "$data"
    touch "$data/.agent-sync-data"
    printf 'memory\n' > "$home/.claude/projects/$local_name/memory/note.md"

    output="$(HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local agent_sync match)"

    [[ "$output" == *"local"*"$local_name"*"-> -app"* ]] || { echo "preview did not show suggested mapping" >&2; exit 1; }
    [ ! -e "$data/.agent-sync-projects" ] || { echo "preview wrote mapping file" >&2; exit 1; }
    [ ! -e "$data/projects/-app/memory/note.md" ] || { echo "preview copied project memory" >&2; exit 1; }
}

test_match_auto_applies_suggested_mapping() {
    local tmp home data prefix local_name
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    prefix="$(printf '%s' "$home" | tr '/' '-')"
    local_name="$prefix-app"
    mkdir -p "$home/.claude/projects/$local_name/memory" "$data"
    touch "$data/.agent-sync-data"
    printf 'memory\n' > "$home/.claude/projects/$local_name/memory/note.md"

    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local agent_sync match --auto >/dev/null

    grep -Fqx -- "$local_name -app" "$data/.agent-sync-projects" || { echo "auto mapping was not recorded" >&2; exit 1; }
    assert_file "$data/projects/-app/memory/note.md"
    assert_symlink_to "$home/.claude/projects/$local_name/memory" "$data/projects/-app/memory"
}

test_match_explicit_mapping_overrides_suggestion() {
    local tmp home data prefix local_name
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    prefix="$(printf '%s' "$home" | tr '/' '-')"
    local_name="$prefix-long-path-app"
    mkdir -p "$home/.claude/projects/$local_name/memory" "$data"
    touch "$data/.agent-sync-data"
    printf 'memory\n' > "$home/.claude/projects/$local_name/memory/note.md"

    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local agent_sync match "$local_name:-app" >/dev/null

    grep -Fqx -- "$local_name -app" "$data/.agent-sync-projects" || { echo "explicit mapping was not recorded" >&2; exit 1; }
    assert_file "$data/projects/-app/memory/note.md"
    assert_symlink_to "$home/.claude/projects/$local_name/memory" "$data/projects/-app/memory"
}

test_env_install_and_remove_managed_block() {
    local tmp home data
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    mkdir -p "$home" "$data"
    touch "$data/.agent-sync-data"
    printf 'export KEEP_ME=1\n' > "$home/.zshrc"

    HOME="$home" SHELL=/bin/zsh AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local agent_sync env install >/dev/null

    grep -Fqx '# >>> agent-sync >>>' "$home/.zshrc" || { echo "env block was not installed" >&2; exit 1; }
    grep -Fqx "export AGENT_SYNC_DIR=$data" "$home/.zshrc" || { echo "AGENT_SYNC_DIR missing from env block" >&2; exit 1; }

    HOME="$home" SHELL=/bin/zsh AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local agent_sync env remove >/dev/null

    ! grep -Fqx '# >>> agent-sync >>>' "$home/.zshrc" || { echo "env block was not removed" >&2; exit 1; }
    grep -Fqx 'export KEEP_ME=1' "$home/.zshrc" || { echo "unrelated shell config was removed" >&2; exit 1; }
}

test_instructions_install_and_remove_managed_block() {
    local tmp home data
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    mkdir -p "$home/.claude" "$home/.codex" "$data"
    touch "$data/.agent-sync-data"
    printf 'existing note\n' > "$data/instructions.md"

    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local agent_sync instructions install >/dev/null

    grep -Fqx '<!-- >>> agent-sync:auto-sync >>> -->' "$data/instructions.md" || { echo "instruction block missing" >&2; exit 1; }

    # One shared file reaches every agent through its own linked name.
    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local AGENT_SYNC_AGENTS=claude,codex agent_sync link >/dev/null
    grep -Fqx '<!-- >>> agent-sync:auto-sync >>> -->' "$home/.claude/CLAUDE.md" || { echo "block not visible to claude" >&2; exit 1; }
    grep -Fqx '<!-- >>> agent-sync:auto-sync >>> -->' "$home/.codex/AGENTS.md" || { echo "block not visible to codex" >&2; exit 1; }

    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local agent_sync instructions remove >/dev/null

    ! grep -Fqx '<!-- >>> agent-sync:auto-sync >>> -->' "$data/instructions.md" || { echo "instruction block was not removed" >&2; exit 1; }
    grep -Fqx 'existing note' "$data/instructions.md" || { echo "existing content was not preserved" >&2; exit 1; }
}

test_skills_link_per_entry_and_keep_vendor_skills() {
    local tmp home data
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    mkdir -p "$home/.codex/skills/.system" "$home/.codex/skills/vendor" "$data/skills/mine" "$data/skills/stale"
    touch "$data/.agent-sync-data"
    printf 'mine\n' > "$data/skills/mine/SKILL.md"
    printf 'stale\n' > "$data/skills/stale/SKILL.md"
    printf 'vendor\n' > "$home/.codex/skills/vendor/SKILL.md"
    printf 'system\n' > "$home/.codex/skills/.system/marker"

    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local AGENT_SYNC_AGENTS=codex agent_sync link >/dev/null

    assert_symlink_to "$home/.codex/skills/mine" "$data/skills/mine"
    [ ! -L "$home/.codex/skills/vendor" ] || { echo "vendor skill was replaced by a link" >&2; exit 1; }
    assert_file "$home/.codex/skills/vendor/SKILL.md"
    assert_file "$home/.codex/skills/.system/marker"

    # A skill removed from the data directory loses its link, vendor skills stay.
    rm -rf "$data/skills/stale"
    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local AGENT_SYNC_AGENTS=codex agent_sync link >/dev/null
    [ ! -L "$home/.codex/skills/stale" ] || { echo "stale skill link was not pruned" >&2; exit 1; }
    assert_file "$home/.codex/skills/vendor/SKILL.md"

    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local AGENT_SYNC_AGENTS=codex agent_sync unlink >/dev/null
    [ ! -e "$home/.codex/skills/mine" ] || { echo "managed skill link was not removed" >&2; exit 1; }
    assert_file "$home/.codex/skills/vendor/SKILL.md"
    assert_file "$home/.codex/skills/.system/marker"
}

test_links_multiple_agents_from_one_data_dir() {
    local tmp home data prefix
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    prefix="$(printf '%s' "$home" | tr '/' '-')"
    mkdir -p "$home" "$data/skills/demo" "$data/projects/-app/memory"
    touch "$data/.agent-sync-data"
    printf '{"shared":true}\n' > "$data/settings.json"
    printf 'shared instructions\n' > "$data/instructions.md"
    printf 'demo\n' > "$data/skills/demo/SKILL.md"
    printf 'memory\n' > "$data/projects/-app/memory/note.md"

    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local \
        AGENT_SYNC_AGENTS=claude,tclaude,codex agent_sync link >/dev/null

    # Claude Code and tclaude take the full asset set, sharing one settings.json.
    assert_symlink_to "$home/.claude/settings.json" "$data/settings.json"
    assert_symlink_to "$home/.tclaude/settings.json" "$data/settings.json"
    assert_symlink_to "$home/.claude/CLAUDE.md" "$data/instructions.md"
    assert_symlink_to "$home/.tclaude/CLAUDE.md" "$data/instructions.md"
    assert_symlink_to "$home/.tclaude/projects/$prefix-app/memory" "$data/projects/-app/memory"

    # Codex takes only what genuinely maps: instructions under its own name, plus skills.
    assert_symlink_to "$home/.codex/AGENTS.md" "$data/instructions.md"
    assert_symlink_to "$home/.codex/skills/demo" "$data/skills/demo"
    [ ! -e "$home/.codex/settings.json" ] || { echo "codex must not receive settings.json" >&2; exit 1; }
    [ ! -e "$home/.codex/CLAUDE.md" ] || { echo "codex must not receive CLAUDE.md" >&2; exit 1; }
    [ ! -e "$home/.codex/projects" ] || { echo "codex must not receive project memory" >&2; exit 1; }
}

test_link_leaves_disabled_agents_untouched() {
    local tmp home data
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    mkdir -p "$home/.tclaude" "$data"
    touch "$data/.agent-sync-data"
    printf '{"shared":true}\n' > "$data/settings.json"
    printf '{"device":true}\n' > "$home/.tclaude/settings.json"

    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local agent_sync link >/dev/null

    assert_symlink_to "$home/.claude/settings.json" "$data/settings.json"
    [ ! -L "$home/.tclaude/settings.json" ] || { echo "disabled agent was linked" >&2; exit 1; }
    grep -Fqx '{"device":true}' "$home/.tclaude/settings.json" || { echo "disabled agent config was modified" >&2; exit 1; }
}

test_unlink_only_touches_enabled_agents() {
    local tmp home data
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    mkdir -p "$home/.claude" "$home/.tclaude" "$data"
    touch "$data/.agent-sync-data"
    printf '{}\n' > "$data/settings.json"
    ln -s "$data/settings.json" "$home/.claude/settings.json"
    ln -s "$data/settings.json" "$home/.tclaude/settings.json"

    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local agent_sync unlink >/dev/null

    [ ! -e "$home/.claude/settings.json" ] || { echo "enabled agent link was not removed" >&2; exit 1; }
    assert_symlink_to "$home/.tclaude/settings.json" "$data/settings.json"

    HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local AGENT_SYNC_AGENTS=tclaude agent_sync unlink >/dev/null
    [ ! -e "$home/.tclaude/settings.json" ] || { echo "explicit agent link was not removed" >&2; exit 1; }
}

test_rejects_unknown_agent() {
    local tmp home data
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    mkdir -p "$home" "$data"
    touch "$data/.agent-sync-data"

    if HOME="$home" AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local \
        AGENT_SYNC_AGENTS=claude,nope agent_sync link >/dev/null 2>&1; then
        echo "expected unknown agent to fail" >&2
        exit 1
    fi
}

test_unlink_env_removes_managed_env_block() {
    local tmp home data
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    mkdir -p "$home/.claude" "$data"
    touch "$data/.agent-sync-data"
    printf '{}\n' > "$data/settings.json"
    ln -s "$data/settings.json" "$home/.claude/settings.json"
    cat > "$home/.zshrc" <<'EOF'
export KEEP_ME=1
# >>> agent-sync >>>
export AGENT_SYNC_DIR=/tmp/old
# <<< agent-sync <<<
EOF

    HOME="$home" SHELL=/bin/zsh AGENT_SYNC_DIR="$data" AGENT_SYNC_BACKEND=local agent_sync unlink --env >/dev/null

    [ ! -e "$home/.claude/settings.json" ] || { echo "managed symlink was not removed" >&2; exit 1; }
    ! grep -Fqx '# >>> agent-sync >>>' "$home/.zshrc" || { echo "env block was not removed by unlink --env" >&2; exit 1; }
    grep -Fqx 'export KEEP_ME=1' "$home/.zshrc" || { echo "unrelated shell config was removed by unlink --env" >&2; exit 1; }
}

run_test "init supports local backend" test_init_local_backend
run_test "link backs up existing config before symlink" test_link_backs_up_existing_config
run_test "project memory migration includes dotfiles" test_migrates_dotfiles_in_project_memory
run_test "unlink does not remove similarly-prefixed foreign links" test_unlink_uses_path_boundary
run_test "unmarked data directories are rejected" test_rejects_unmarked_data_dir
run_test "s3 push archives only allowlisted data" test_s3_push_uses_allowlisted_archive
run_test "s3 pull rejects symlink archives" test_s3_pull_rejects_symlink_archive
run_test "help runs without any agent-sync env" test_help_runs_without_any_env
run_test "git backend rejects nested data dir" test_git_backend_rejects_nested_data_dir
run_test "mcp-server links preserve .venv and prune stale" test_mcp_server_links_preserve_venv_and_prune_stale
run_test "s3 pull rejects hardlink archives" test_s3_pull_rejects_hardlink_archive
run_test "match preview does not modify data" test_match_preview_does_not_modify
run_test "match auto applies suggested mapping" test_match_auto_applies_suggested_mapping
run_test "match explicit mapping overrides suggestion" test_match_explicit_mapping_overrides_suggestion
run_test "env install/remove manages only marked block" test_env_install_and_remove_managed_block
run_test "instructions install/remove manages marked block" test_instructions_install_and_remove_managed_block
run_test "skills link per entry and keep vendor skills" test_skills_link_per_entry_and_keep_vendor_skills
run_test "links several agents from one data dir" test_links_multiple_agents_from_one_data_dir
run_test "link leaves disabled agents untouched" test_link_leaves_disabled_agents_untouched
run_test "unlink only touches enabled agents" test_unlink_only_touches_enabled_agents
run_test "unknown agent names are rejected" test_rejects_unknown_agent
run_test "unlink --env removes managed env block" test_unlink_env_removes_managed_env_block

printf 'ok: %s tests passed\n' "$pass_count"
