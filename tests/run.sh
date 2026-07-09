#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT/bin/claude-sync"

pass_count=0
tmp_dirs=()

claude_sync() {
    env \
        CLAUDE_SYNC_REMOTE= \
        CLAUDE_SYNC_STORAGE_OBJECT= \
        CLAUDE_SYNC_STORAGE_URI= \
        CLAUDE_SYNC_WORKDIR= \
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

    HOME="$home" CLAUDE_SYNC_BACKEND=local claude_sync init "$data" >/dev/null

    assert_file "$data/.claude-sync-data"
    assert_file "$data/.gitignore"
    [ ! -d "$data/.git" ] || { echo "local backend should not initialize git" >&2; exit 1; }
}

test_link_backs_up_existing_config() {
    local tmp home data backup_count
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    mkdir -p "$home/.claude" "$data"
    touch "$data/.claude-sync-data"
    printf '{"repo":true}\n' > "$data/settings.json"
    printf '{"local":true}\n' > "$home/.claude/settings.json"

    HOME="$home" CLAUDE_SYNC_DIR="$data" CLAUDE_SYNC_BACKEND=local claude_sync link >/dev/null

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
    touch "$data/.claude-sync-data"
    printf 'visible\n' > "$home/.claude/projects/$prefix$canonical/memory/file.md"
    printf 'hidden\n' > "$home/.claude/projects/$prefix$canonical/memory/.hidden"

    HOME="$home" CLAUDE_SYNC_DIR="$data" CLAUDE_SYNC_BACKEND=local claude_sync link >/dev/null

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
    touch "$data/.claude-sync-data"
    printf '{}\n' > "$data/settings.json"
    printf '{}\n' > "$sibling/settings.json"
    ln -s "$sibling/settings.json" "$home/.claude/settings.json"

    HOME="$home" CLAUDE_SYNC_DIR="$data" CLAUDE_SYNC_BACKEND=local claude_sync unlink >/dev/null

    assert_symlink_to "$home/.claude/settings.json" "$sibling/settings.json"
}

test_rejects_unmarked_data_dir() {
    local tmp home data
    tmp="$(new_tmp)"
    home="$tmp/home"
    data="$tmp/data"
    mkdir -p "$home" "$data"

    if HOME="$home" CLAUDE_SYNC_DIR="$data" CLAUDE_SYNC_BACKEND=local claude_sync status >/dev/null 2>&1; then
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
    touch "$data/.claude-sync-data"
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
        CLAUDE_SYNC_DIR="$data" \
        CLAUDE_SYNC_BACKEND=s3 \
        CLAUDE_SYNC_STORAGE_URI=s3://bucket/prefix \
        CLAUDE_SYNC_WORKDIR= \
        "$BIN" push >/dev/null

    listing="$(tar -tzf "$archive")"
    [[ "$listing" == *"./settings.json"* ]] || { echo "settings.json missing from archive" >&2; exit 1; }
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
    touch "$data/.claude-sync-data" "$remote_src/.claude-sync-data"
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
        CLAUDE_SYNC_DIR="$data" \
        CLAUDE_SYNC_BACKEND=s3 \
        CLAUDE_SYNC_STORAGE_URI=s3://bucket/prefix \
        CLAUDE_SYNC_WORKDIR= \
        "$BIN" pull >/dev/null 2>&1; then
        echo "expected symlink archive pull to fail" >&2
        exit 1
    fi
    [ ! -L "$data/settings.json" ] || { echo "local settings became a symlink" >&2; exit 1; }
    grep -qx 'keep' "$data/settings.json" || { echo "local settings was modified" >&2; exit 1; }
}

run_test "init supports local backend" test_init_local_backend
run_test "link backs up existing config before symlink" test_link_backs_up_existing_config
run_test "project memory migration includes dotfiles" test_migrates_dotfiles_in_project_memory
run_test "unlink does not remove similarly-prefixed foreign links" test_unlink_uses_path_boundary
run_test "unmarked data directories are rejected" test_rejects_unmarked_data_dir
run_test "s3 push archives only allowlisted data" test_s3_push_uses_allowlisted_archive
run_test "s3 pull rejects symlink archives" test_s3_pull_rejects_symlink_archive

printf 'ok: %s tests passed\n' "$pass_count"
