#!/data/data/com.termux/files/usr/bin/bash
# PocketBunty one-time setup for Termux.
#
# Sets up only the bridge (Termux packages, Python + bridge server), starts it and links the app.
# The app then installs the rest (git, GitHub CLI, jcode) in the background with scripts/env.sh. Safe to run again: finished steps are skipped,
# and it repairs what is missing or broken. Everything lives in one hidden folder, ~/.pocketbunty
# (bridge, token, start script, logs), so it isn't deleted by mistake. It also adds the
# `pocketbunty` command: reconnects the app (or repairs the setup if files are missing).
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/iamd2epak/pocketbunty/main/scripts/setup.sh)
#
# Options: --full (also install git, gh and jcode here), --update-jcode (same, reinstalling jcode),
#          --no-start (don't start the bridge)

set -u
set -o pipefail

BASE_URL="${POCKETBUNTY_BASE_URL:-https://raw.githubusercontent.com/iamd2epak/pocketbunty/main}"
BRIDGE_DIR="$HOME/.pocketbunty"
OLD_DIR="$HOME/bridge"  # before v7 the bridge lived here (visible): moved into BRIDGE_DIR
SERVER="$BRIDGE_DIR/server.py"
START="$BRIDGE_DIR/start.sh"
LOG="$BRIDGE_DIR/setup.log"
PORT=8765
APP_LINK="termichat://link"

ENV_ARGS=()
WANT_ENV=false
WANT_START=true
for arg in "$@"; do
  case "$arg" in
    --full) WANT_ENV=true ;;
    --update-jcode) WANT_ENV=true; ENV_ARGS+=(--update-jcode) ;;
    # Older apps ran "--no-start" in their terminal to install jcode: keep doing that.
    --no-start) WANT_START=false; WANT_ENV=true ;;
    --no-jcode) WANT_ENV=false ;;
  esac
done

# ---------- output helpers ----------
if [ -t 1 ]; then
  C_OK=$'\033[1;32m' C_WARN=$'\033[1;33m' C_ERR=$'\033[1;31m' C_STEP=$'\033[1;36m' C_DIM=$'\033[2m' C_OFF=$'\033[0m'
else
  C_OK="" C_WARN="" C_ERR="" C_STEP="" C_DIM="" C_OFF=""
fi
STEP_NO=0
PROBLEMS=()
step() { STEP_NO=$((STEP_NO + 1)); printf '\n%s[%d] %s%s\n' "$C_STEP" "$STEP_NO" "$*" "$C_OFF"; }
ok()   { printf '  %s✓%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
note() { printf '  %s%s%s\n' "$C_DIM" "$*" "$C_OFF"; }
problem() { PROBLEMS+=("$*"); printf '  %s✗%s %s\n' "$C_ERR" "$C_OFF" "$*"; }
die() {
  printf '\n%s✗ Setup stopped: %s%s\n' "$C_ERR" "$*" "$C_OFF"
  printf '  Full log: %s\n  Fix the problem above and run the same command again.\n' "$LOG"
  exit 1
}
have() { command -v "$1" >/dev/null 2>&1; }

# Runs a command quietly (output goes to the log), retrying a few times for flaky networks.
run_retry() {
  local tries=$1; shift
  local n=1
  while true; do
    if "$@" >>"$LOG" 2>&1; then return 0; fi
    [ "$n" -ge "$tries" ] && return 1
    note "retrying ($n/$((tries - 1)))…"
    sleep $((n * 3))
    n=$((n + 1))
  done
}

# ---------- 0. sanity checks ----------
mkdir -p "$BRIDGE_DIR" 2>/dev/null
: >"$LOG" 2>/dev/null || LOG=/dev/null
printf '%sPocketBunty setup%s  (log: %s)\n' "$C_OK" "$C_OFF" "$LOG"

if [ -z "${PREFIX:-}" ] || [ ! -d "${PREFIX:-/nonexistent}" ] || ! have apt-get; then
  die "this script must run inside the Termux app."
fi

# One setup at a time.
LOCK="${TMPDIR:-$PREFIX/tmp}/pocketbunty-setup.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  # Stale lock from a killed run (older than 30 min)?
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
    rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || die "can't create $LOCK"
  else
    die "setup is already running in another Termux session. Wait for it, or close that session."
  fi
fi
trap 'rm -rf "$LOCK"' EXIT

if [ "${TERMUX_APK_RELEASE:-}" = "GOOGLE_PLAY_STORE" ]; then
  warn "Termux from Google Play has limits. If something fails, install Termux from F-Droid or GitHub."
fi

# Is this shell running under the bridge (the app's terminal)? Then don't restart the bridge.
inside_bridge() {
  local pid=$$ i=0 cmd
  while [ "$pid" -gt 1 ] && [ $i -lt 20 ]; do
    cmd="$(tr '\0' ' ' </proc/$pid/cmdline 2>/dev/null || true)"
    case "$cmd" in *bridge/server.py*|*.pocketbunty/server.py*) return 0 ;; esac
    pid="$(awk '{print $4}' /proc/$pid/stat 2>/dev/null || echo 1)"
    i=$((i + 1))
  done
  return 1
}

# ---------- 1. Termux packages ----------
export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -o Acquire::Retries=3)

step "Updating Termux packages (first time can take a few minutes)"
apt_update() { apt-get update -o Acquire::Retries=3; }
if ! run_retry 2 apt_update; then
  warn "package mirror not responding, switching to the main Termux mirror"
  SOURCES="$PREFIX/etc/apt/sources.list"
  [ -f "$SOURCES" ] && cp -f "$SOURCES" "$SOURCES.pocketbunty-backup" 2>/dev/null
  echo "deb https://packages-cf.termux.dev/apt/termux-main stable main" >"$SOURCES"
  rm -f "$PREFIX/etc/termux/chosen_mirrors" 2>/dev/null
  run_retry 3 apt_update || die "can't download the package list. Check your internet connection (or run: termux-change-repo)."
fi
ok "package list updated"
# Old libraries + new packages = "CANNOT LINK EXECUTABLE" errors, so upgrade first.
if run_retry 2 apt-get "${APT_OPTS[@]}" full-upgrade; then
  ok "installed packages are up to date"
else
  run_retry 1 dpkg --configure -a
  run_retry 2 apt-get "${APT_OPTS[@]}" -f install
  run_retry 2 apt-get "${APT_OPTS[@]}" full-upgrade && ok "installed packages are up to date" \
    || warn "some packages couldn't be upgraded (continuing)"
fi

# Installs one package unless its command already works. Returns 1 on failure.
pkg_need() {
  local pkg=$1 cmd=${2:-$1}
  if [ "$cmd" != "-" ] && have "$cmd"; then ok "$pkg"; return 0; fi
  if run_retry 2 apt-get "${APT_OPTS[@]}" install "$pkg"; then ok "$pkg installed"; return 0; fi
  return 1
}

step "Installing tools"
pkg_need python python || die "couldn't install Python. See $LOG"
pkg_need python-pip pip || problem "pip couldn't be installed (needed for the bridge)"
pkg_need curl curl || die "couldn't install curl. See $LOG"
pkg_need procps pgrep || warn "procps not installed (bridge auto-start check is weaker)"
pkg_need termux-am am || warn "termux-am not installed (the app may need the token pasted by hand)"

# ---------- 2. Python packages for the bridge ----------
step "Setting up the bridge server (Python)"
py_has_aiohttp() { python -c 'import aiohttp' >/dev/null 2>&1; }
pip_install() {
  # Pure-Python builds: no compiler needed on the phone.
  AIOHTTP_NO_EXTENSIONS=1 MULTIDICT_NO_EXTENSIONS=1 YARL_NO_EXTENSIONS=1 \
    FROZENLIST_NO_EXTENSIONS=1 PROPCACHE_NO_EXTENSIONS=1 \
    python -m pip install --disable-pip-version-check --no-input "$@" aiohttp
}
if py_has_aiohttp; then
  ok "aiohttp"
else
  if run_retry 2 pip_install || run_retry 1 pip_install --break-system-packages; then
    :
  else
    note "trying again with a compiler (slower)…"
    pkg_need clang clang >/dev/null
    run_retry 2 python -m pip install --disable-pip-version-check --no-input aiohttp || true
  fi
  py_has_aiohttp && ok "aiohttp installed" || die "couldn't install aiohttp for Python. See $LOG"
fi

mkdir -p "$BRIDGE_DIR" || die "can't create $BRIDGE_DIR"
TMP_SERVER="$BRIDGE_DIR/.server.py.download"
if run_retry 3 curl -fsSL --connect-timeout 15 -o "$TMP_SERVER" "$BASE_URL/server/server.py" \
   && grep -q "bridge server" "$TMP_SERVER" && python -m py_compile "$TMP_SERVER" 2>>"$LOG"; then
  mv -f "$TMP_SERVER" "$SERVER" && ok "server.py downloaded"
else
  rm -f "$TMP_SERVER"
  if [ -f "$SERVER" ]; then warn "couldn't download the latest server.py, keeping the one you have"
  else die "couldn't download server.py. Check your internet connection."; fi
fi
rm -rf "$BRIDGE_DIR/__pycache__" 2>/dev/null

# env.sh: the app runs it next (in the background) for git, GitHub CLI and jcode.
ENV_SH="$BRIDGE_DIR/env.sh"
if run_retry 3 curl -fsSL --connect-timeout 15 -o "$ENV_SH.download" "$BASE_URL/scripts/env.sh" \
   && grep -q "pocketbunty-env" "$ENV_SH.download" && bash -n "$ENV_SH.download" 2>>"$LOG"; then
  mv -f "$ENV_SH.download" "$ENV_SH" && ok "env.sh downloaded"
else
  rm -f "$ENV_SH.download"
  warn "couldn't download env.sh now (the app downloads it later)"
fi

# Token: same file server.py reads, created here so the app can be linked right away.
TOKEN_FILE="$BRIDGE_DIR/token"
# Moving from the old ~/bridge: keep its token so the app stays linked.
if [ ! -s "$TOKEN_FILE" ] && [ -s "$OLD_DIR/token" ]; then
  cp -f "$OLD_DIR/token" "$TOKEN_FILE" && ok "token moved from ~/bridge"
fi
if [ ! -s "$TOKEN_FILE" ]; then
  (umask 077; python -c 'import secrets; print(secrets.token_urlsafe(24))' >"$TOKEN_FILE") || die "can't create the token"
fi
chmod 600 "$TOKEN_FILE" 2>/dev/null
TOKEN="$(head -n1 "$TOKEN_FILE" | tr -d '[:space:]')"
[ -n "$TOKEN" ] || die "token file is empty: $TOKEN_FILE"

# start.sh: starts the bridge in the background unless it already runs.
cat >"$START" <<'EOF2'
#!/data/data/com.termux/files/usr/bin/bash
# Starts the PocketBunty bridge in the background (does nothing if it already runs).
DIR="$HOME/.pocketbunty"
PID_FILE="$DIR/server.pid"
if [ -s "$PID_FILE" ]; then
  pid="$(cat "$PID_FILE" 2>/dev/null)"
  case "$(tr '\0' ' ' </proc/"$pid"/cmdline 2>/dev/null)" in *.pocketbunty/server.py*) exit 0 ;; esac
fi
command -v pgrep >/dev/null 2>&1 && pgrep -f "\.pocketbunty/server\.py" >/dev/null 2>&1 && exit 0
[ -f "$DIR/server.py" ] || exit 1
cd "$DIR" || exit 1
nohup python "$DIR/server.py" >"$DIR/server.log" 2>&1 </dev/null &
echo $! >"$PID_FILE"
EOF2
chmod +x "$START" && ok "start script: ~/.pocketbunty/start.sh"

# `pocketbunty` command: one short word to reconnect the app later.
CMD="$PREFIX/bin/pocketbunty"
cat >"$CMD" <<'EOF2'
#!/data/data/com.termux/files/usr/bin/bash
# PocketBunty: starts the bridge and goes back to the app. "pocketbunty setup" repairs everything.
DIR="$HOME/.pocketbunty"
URL="https://raw.githubusercontent.com/iamd2epak/pocketbunty/main/scripts/setup.sh"
if [ "${1:-}" != setup ] && [ -f "$DIR/server.py" ] && [ -s "$DIR/token" ] && [ -x "$DIR/start.sh" ]; then
  "$DIR/start.sh"; sleep 1
  echo "PocketBunty: connected, going back to the app…"
  am start -a android.intent.action.VIEW -d "termichat://link?token=$(head -n1 "$DIR/token" | tr -d '[:space:]')" >/dev/null 2>&1
else
  [ "${1:-}" = setup ] && shift
  echo "PocketBunty: setting up…"
  mkdir -p "$DIR" && curl -fsSL "$URL" -o "$DIR/setup.sh" && exec bash "$DIR/setup.sh" "$@"
fi
EOF2
chmod +x "$CMD" && ok "command added: pocketbunty (reconnects the app)"

# Every Termux login shell: start the bridge if needed, and find jcode in ~/.local/bin.
PROFILE_D="$PREFIX/etc/profile.d"
mkdir -p "$PROFILE_D"
rm -f "$PROFILE_D/codenest.sh" 2>/dev/null
cat >"$PROFILE_D/pocketbunty.sh" <<'EOF2'
# Added by PocketBunty setup.
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
[ -x "$HOME/.pocketbunty/start.sh" ] && "$HOME/.pocketbunty/start.sh" >/dev/null 2>&1
EOF2
ok "bridge starts by itself when you open Termux"

# ---------- 5. start and link ----------
health() { curl -fsS --max-time 3 -H "X-Token: $TOKEN" "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; }

if [ "$WANT_START" = true ]; then
  step "Starting the bridge"
  if inside_bridge; then
    warn "running inside the app's terminal: restart the bridge later from Termux (type: pocketbunty)"
  else
    # Restart so the new server.py runs.
    # Also stops an old bridge still running from ~/bridge.
    if have pkill; then pkill -f "bridge/server\.py|\.pocketbunty/server\.py" >/dev/null 2>&1; sleep 1; fi
    rm -f "$BRIDGE_DIR/server.pid"
    "$START"
  fi
  started=false
  for _ in $(seq 1 20); do
    if health; then started=true; break; fi
    sleep 1
  done
  if [ "$started" = true ]; then
    ok "bridge is running"
  else
    problem "bridge didn't start. Last lines of ~/.pocketbunty/server.log:"
    tail -n 8 "$BRIDGE_DIR/server.log" 2>/dev/null | sed 's/^/      /'
  fi

  step "Linking the app"
  if [ "$started" = true ] && have am && am start -a android.intent.action.VIEW -d "$APP_LINK?token=$TOKEN" >>"$LOG" 2>&1; then
    ok "app opened and linked"
  else
    warn "open the app and paste this token:"
    printf '\n      %s%s%s\n' "$C_WARN" "$TOKEN" "$C_OFF"
  fi
fi

# ---------- git, GitHub CLI, jcode (only with --full / from a terminal) ----------
if [ "$WANT_ENV" = true ]; then
  step "Installing git, GitHub CLI and jcode"
  if [ -f "$ENV_SH" ]; then
    bash "$ENV_SH" "${ENV_ARGS[@]}" || problem "environment setup had problems (see $BRIDGE_DIR/env.log)"
  else
    problem "env.sh is missing. Run the same command again."
  fi
fi

# ---------- clean up the old layout ----------
# The old visible files (~/bridge, ~/pocketbunty-setup.sh) are not used any more.
if ! inside_bridge && [ -d "$OLD_DIR" ]; then
  rm -rf "$OLD_DIR/__pycache__" 2>/dev/null
  rm -f "$OLD_DIR/server.py" "$OLD_DIR/start.sh" "$OLD_DIR/token" "$OLD_DIR/server.log" \
    "$OLD_DIR/server.pid" "$OLD_DIR/.server.py.download" 2>/dev/null
  rmdir "$OLD_DIR" 2>/dev/null && ok "old ~/bridge folder removed (now in ~/.pocketbunty)"
fi
rm -f "$HOME/pocketbunty-setup.sh" "$HOME/.pocketbunty-setup.log" 2>/dev/null

# ---------- summary ----------
printf '\n'
if [ ${#PROBLEMS[@]} -eq 0 ]; then
  printf '%s✓ Bridge ready!%s The app finishes the setup by itself. Next time just open Termux (or type: pocketbunty).\n' "$C_OK" "$C_OFF"
else
  printf '%sSetup finished with %d problem(s):%s\n' "$C_WARN" "${#PROBLEMS[@]}" "$C_OFF"
  for p in "${PROBLEMS[@]}"; do printf '  - %s\n' "$p"; done
  printf 'Run the same command again to retry. Log: %s\n' "$LOG"
fi
exit 0
