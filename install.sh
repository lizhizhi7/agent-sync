#!/usr/bin/env bash
# claude-sync installer.
#
# Defaults:
#   tool repo : https://github.com/lizhizhi7/claude-sync.git
#   install   : ~/.local/share/claude-sync
#   bin       : ~/.local/bin/claude-sync
#
# Override any of these via env vars (CLAUDE_SYNC_REPO, CLAUDE_SYNC_INSTALL_DIR,
# CLAUDE_SYNC_BIN_DIR). Re-run to upgrade.

set -eu

REPO="${CLAUDE_SYNC_REPO:-https://github.com/lizhizhi7/claude-sync.git}"
INSTALL_DIR="${CLAUDE_SYNC_INSTALL_DIR:-$HOME/.local/share/claude-sync}"
BIN_DIR="${CLAUDE_SYNC_BIN_DIR:-$HOME/.local/bin}"

if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Updating $INSTALL_DIR ..."
    git -C "$INSTALL_DIR" pull --rebase --autostash
else
    echo "Cloning $REPO -> $INSTALL_DIR ..."
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone "$REPO" "$INSTALL_DIR"
fi

chmod +x "$INSTALL_DIR/bin/claude-sync"
mkdir -p "$BIN_DIR"
ln -sfn "$INSTALL_DIR/bin/claude-sync" "$BIN_DIR/claude-sync"

echo ""
echo "Installed:"
echo "  tool : $INSTALL_DIR"
echo "  bin  : $BIN_DIR/claude-sync"
echo ""

case ":$PATH:" in
    *":$BIN_DIR:"*)
        ;;
    *)
        echo "Add to your shell profile (.zshrc / .bashrc):"
        echo "  export PATH=\"$BIN_DIR:\$PATH\""
        echo ""
        ;;
esac

echo "Next:"
echo "  claude-sync init ~/path/to/your/private/config"
echo "  export CLAUDE_SYNC_DIR=~/path/to/your/private/config"
echo "  claude-sync link"
