#!/bin/bash
set -euo pipefail

REPO_URL="https://github.com/felipe-promineo/github-pr-overview.git"
DEFAULT_DIR="$HOME/Repositories/github-pr-overview"
LABEL="github-pr-overview"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SERVICE="gui/$(id -u)/$LABEL"
URL="http://127.0.0.1:8765"
LOG="/tmp/github-pr-overview.log"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
fail() { printf '\n\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: install.sh [--dir PATH] [--no-browser] [--uninstall]

Installs GitHub PR overview and starts it at login. Run it again to update.

  --dir PATH     where to clone the app (default: $DEFAULT_DIR)
  --no-browser   don't open the board when done
  --uninstall    stop the app and remove the login item and gh aliases
                 (keeps the clone and ~/.config/github-pr-overview)
EOF
}

stop_service() {
  if launchctl print "$SERVICE" >/dev/null 2>&1; then
    launchctl bootout "$SERVICE" 2>/dev/null || true
    for _ in {1..20}; do
      launchctl print "$SERVICE" >/dev/null 2>&1 || return 0
      sleep 0.25
    done
    fail "Couldn't stop the running $LABEL login item."
  fi
}

uninstall() {
  step "Removing the login item"
  stop_service
  rm -f "$PLIST"
  if command -v gh >/dev/null 2>&1; then
    gh alias delete pr-overview >/dev/null 2>&1 || true
    gh alias delete pr-overview-web >/dev/null 2>&1 || true
  fi
  note "Done. The clone and your settings in ~/.config/github-pr-overview are still there; delete them to remove everything."
}

app_dir=""
open_browser=true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) [[ $# -ge 2 ]] || fail "--dir needs a path"; app_dir="$2"; shift 2 ;;
    --no-browser) open_browser=false; shift ;;
    --uninstall) uninstall; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "Unknown option: $1" ;;
  esac
done

[[ "$(uname -s)" == "Darwin" ]] || fail "GitHub PR overview only runs on macOS."

step "Checking Homebrew"
if ! command -v brew >/dev/null 2>&1; then
  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [[ -x "$candidate" ]]; then
      eval "$("$candidate" shellenv)"
      break
    fi
  done
fi
command -v brew >/dev/null 2>&1 || fail "Homebrew is needed. Install it from https://brew.sh, then run this installer again."
note "$(command -v brew)"

step "Installing tools"
python_works() { command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; }
missing=()
command -v gh >/dev/null 2>&1 || missing+=(gh)
command -v terminal-notifier >/dev/null 2>&1 || missing+=(terminal-notifier)
command -v git >/dev/null 2>&1 || missing+=(git)
python_works || missing+=(python)
if [[ ${#missing[@]} -gt 0 ]]; then
  note "brew install ${missing[*]}"
  brew install "${missing[@]}"
  hash -r
fi
python_works || fail "Python 3.8 or newer is needed, and python3 still doesn't run it."
python="$(command -v python3)"
note "gh: $(command -v gh)"
note "terminal-notifier: $(command -v terminal-notifier)"
note "python3: $python ($("$python" --version 2>&1))"

step "Checking the GitHub login"
if ! gh auth status >/dev/null 2>&1; then
  note "Not logged in to GitHub. Starting gh auth login."
  gh auth login </dev/tty
fi
note "Logged in as $(gh api user --jq .login)"

step "Getting the app"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || true)"
if [[ -z "$app_dir" && -f "$script_dir/github-pr-overview.py" ]]; then
  app_dir="$script_dir"
fi
app_dir="${app_dir:-$DEFAULT_DIR}"
if [[ -d "$app_dir/.git" ]]; then
  note "Updating $app_dir"
  git -C "$app_dir" pull --ff-only --quiet || note "Couldn't update it (local changes or another branch); installing it as it is."
elif [[ -e "$app_dir" ]]; then
  fail "$app_dir exists but isn't a clone of the app. Pass --dir with another path."
else
  note "Cloning into $app_dir"
  mkdir -p "$(dirname "$app_dir")"
  git clone --quiet "$REPO_URL" "$app_dir"
fi
app_dir="$(cd "$app_dir" && pwd)"
[[ -f "$app_dir/github-pr-overview.py" && -f "$app_dir/github-pr-overview.plist" ]] || fail "$app_dir doesn't contain the app."

step "Setting up the login item"
stop_service
if curl -fsS -o /dev/null --max-time 2 "$URL" 2>/dev/null; then
  fail "Something else already serves $URL (maybe python3 github-pr-overview.py in a terminal). Stop it and run this installer again."
fi
brew_bin="$(brew --prefix)/bin"
agent_path="$brew_bin"
for directory in /usr/local/bin /usr/bin /bin; do
  [[ ":$agent_path:" == *":$directory:"* ]] || agent_path="$agent_path:$directory"
done
mkdir -p "$(dirname "$PLIST")"
sed \
  -e "s|<string>/opt/homebrew/bin/python3</string>|<string>$python</string>|" \
  -e "s|/path/to/github-pr-overview|$app_dir|" \
  -e "s|<string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>|<string>$agent_path</string>|" \
  "$app_dir/github-pr-overview.plist" >"$PLIST"
plutil -lint "$PLIST" >/dev/null || fail "The generated $PLIST isn't valid."
launchctl bootstrap "gui/$(id -u)" "$PLIST"
note "$PLIST"

step "Starting the board"
for _ in {1..40}; do
  if curl -fsS -o /dev/null --max-time 2 "$URL" 2>/dev/null; then
    note "Running on $URL, and starts at login"
    break
  fi
  sleep 0.25
done
curl -fsS -o /dev/null --max-time 2 "$URL" 2>/dev/null || { tail -20 "$LOG" >&2 || true; fail "The board didn't start. The log above is from $LOG."; }

step "Adding gh aliases"
gh alias set --clobber pr-overview "!$app_dir/github-pr-overview.sh" >/dev/null
gh alias set --clobber pr-overview-web "!open $URL" >/dev/null
note "gh pr-overview prints your PRs in the terminal; gh pr-overview-web opens the board"

step "Done"
organizations="$(gh api user/orgs --jq '[.[].login] | join(", ")' 2>/dev/null || true)"
note "Orgs the gh token can see: ${organizations:-none}"
note "If an org you belong to is missing, or its repos don't show up in Settings > Tracked repositories,"
note "it probably uses SSO: run gh auth refresh, then authorize the token for it at https://github.com/settings/tokens."
note "Run this installer again at any time to update the app."
if $open_browser; then
  open "$URL"
fi
