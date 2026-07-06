#!/usr/bin/env bash
#
# Per-user, mount-independent setup for the GSC MCP server.
#
# The server source lives on a shared network volume that macOS mounts under a
# different name for each user (e.g. /Volumes/Whitehat vs /Volumes/Whitehat-1),
# so nothing user-specific may reference an absolute /Volumes path. This script
# installs everything the running user needs into their own $HOME:
#
#   ~/.venvs/gsc-mcp/                     the Python venv (interpreter + package)
#   ~/.config/gsc-mcp/client_secrets.json the OAuth client secrets
#   ~/.config/gsc-mcp/token.json          migrated OAuth token (if one existed)
#   ~/.config/gsc-mcp/accounts/           migrated multi-account state (if any)
#   <repo>/.mcp.json                      a portable config using ${HOME}
#
# The package is installed NON-editable (copied into the venv), so once set up
# the server no longer reads code from the share and is immune to mount renames.
# Because the installed code lives in site-packages, the server cannot migrate
# on-share credentials itself — this script does that copy up front instead.
#
# Re-run this script after pulling code changes to refresh the installed copy.
# Re-runs never clobber existing credentials in ~/.config/gsc-mcp.
#
# Usage:  bash scripts/setup_user_env.sh
set -euo pipefail

# Locate the repo root from this script, whatever the mount is called.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$HOME/.venvs/gsc-mcp"
CONFIG_DIR="$HOME/.config/gsc-mcp"

echo "Repo:   $REPO"
echo "Venv:   $VENV"
echo "Config: $CONFIG_DIR"

# 1. Pick a Python interpreter (3.11+). Prefer the stable framework build.
PYBIN="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13"
[ -x "$PYBIN" ] || PYBIN="$(command -v python3)"
echo "Python: $PYBIN ($("$PYBIN" --version 2>&1))"

# 2. Create the venv and install the package NON-editable (decoupled from the share).
"$PYBIN" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet "$REPO"

# 3. Populate ~/.config/gsc-mcp with credentials + state.
#    Never clobber existing files (safe to re-run).
mkdir -p "$CONFIG_DIR"

# 3a. Client secrets (required for OAuth). Prefer an explicit path in the env.
SECRETS_SRC="${GSC_OAUTH_CLIENT_SECRETS_FILE:-$REPO/client_secrets.json}"
if [ -f "$SECRETS_SRC" ] && [ ! -f "$CONFIG_DIR/client_secrets.json" ]; then
  cp "$SECRETS_SRC" "$CONFIG_DIR/client_secrets.json"
  chmod 600 "$CONFIG_DIR/client_secrets.json"
  echo "Client secrets -> $CONFIG_DIR/client_secrets.json"
elif [ -f "$CONFIG_DIR/client_secrets.json" ]; then
  echo "Client secrets already present in $CONFIG_DIR (left as-is)."
else
  echo "WARNING: no client_secrets.json found at $SECRETS_SRC."
  echo "         Copy one to $CONFIG_DIR/client_secrets.json before first use."
fi

# 3b. Migrate OAuth state so the user keeps their logins (no re-auth).
#     accounts/ (multi-account) takes precedence over the bare token.json.
if [ -f "$REPO/accounts/accounts.json" ] && [ ! -f "$CONFIG_DIR/accounts/accounts.json" ]; then
  cp -R "$REPO/accounts" "$CONFIG_DIR/accounts"
  rm -f "$CONFIG_DIR/accounts/.DS_Store"
  echo "Migrated accounts/ -> $CONFIG_DIR/accounts (original preserved)."
elif [ -f "$REPO/token.json" ] && [ ! -f "$CONFIG_DIR/token.json" ]; then
  cp "$REPO/token.json" "$CONFIG_DIR/token.json"
  echo "Migrated token.json -> $CONFIG_DIR/token.json (original preserved)."
else
  echo "OAuth state already present or nothing to migrate."
fi

# 4. Write a portable .mcp.json (gitignored). ${HOME} is expanded per-user by Claude Code.
cat > "$REPO/.mcp.json" <<'JSON'
{
  "mcpServers": {
    "gsc": {
      "type": "stdio",
      "command": "${HOME}/.venvs/gsc-mcp/bin/gsc-mcp-server",
      "args": [],
      "env": {
        "GSC_STATE_DIR": "${HOME}/.config/gsc-mcp",
        "GSC_OAUTH_CLIENT_SECRETS_FILE": "${HOME}/.config/gsc-mcp/client_secrets.json"
      }
    }
  }
}
JSON
echo "Wrote $REPO/.mcp.json"

# 5. Smoke test: the module must import from the venv, not the share.
#    Run from $HOME so the repo dir isn't on sys.path (cwd would otherwise
#    shadow the installed copy) — this proves the venv works even if the
#    share is gone.
resolved="$(cd "$HOME" && "$VENV/bin/python" -c 'import gsc_server; print(gsc_server.__file__)')"
case "$resolved" in
  "$VENV"/*) echo "OK: gsc_server imports from the venv ($resolved)";;
  *) echo "WARNING: gsc_server imported from $resolved (expected under $VENV)"; exit 1;;
esac

echo
echo "Done. Next steps:"
echo "  - Restart Claude Code, then verify:  claude mcp list | grep gsc"
echo "  - Claude Desktop does NOT expand \${HOME}: edit the 'gsc' entry in"
echo "    ~/Library/Application Support/Claude/claude_desktop_config.json to use"
echo "    absolute home paths, e.g. $VENV/bin/gsc-mcp-server, then restart the app."
