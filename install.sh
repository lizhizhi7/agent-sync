#!/usr/bin/env bash
# agent-sync installer.
#
# Defaults:
#   tool repo : https://github.com/lizhizhi7/agent-sync.git
#   install   : ~/.local/share/agent-sync
#   bin       : ~/.local/bin/agent-sync
#
# Override any of these via env vars (AGENT_SYNC_REPO, AGENT_SYNC_INSTALL_DIR,
# AGENT_SYNC_BIN_DIR). Re-run to upgrade.

set -eu

REPO="${AGENT_SYNC_REPO:-https://github.com/lizhizhi7/agent-sync.git}"
INSTALL_DIR="${AGENT_SYNC_INSTALL_DIR:-$HOME/.local/share/agent-sync}"
BIN_DIR="${AGENT_SYNC_BIN_DIR:-$HOME/.local/bin}"

if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Updating $INSTALL_DIR ..."
    git -C "$INSTALL_DIR" pull --rebase --autostash
else
    echo "Cloning $REPO -> $INSTALL_DIR ..."
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone "$REPO" "$INSTALL_DIR"
fi

chmod +x "$INSTALL_DIR/bin/agent-sync"
mkdir -p "$BIN_DIR"
ln -sfn "$INSTALL_DIR/bin/agent-sync" "$BIN_DIR/agent-sync"

echo ""
echo "Installed:"
echo "  tool : $INSTALL_DIR"
echo "  bin  : $BIN_DIR/agent-sync"
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
echo "  agent-sync init ~/path/to/your/private/config"
echo "  export AGENT_SYNC_DIR=~/path/to/your/private/config"
echo "  agent-sync link"
