#!/data/data/com.termux/files/usr/bin/bash
# PocketBunty setup v2 for Termux (AI coder: OpenCode).
#
# Kept short on purpose: it sets up only the bridge (Termux packages, Python + server.py), starts
# it and opens the app linked. The app then installs git, the GitHub CLI and OpenCode by itself
# (env.sh, in the background). Safe to run again: finished steps are skipped, and it repairs what
# is missing or broken. Everything lives in one hidden folder, ~/.pocketbunty. It also adds the
# `pocketbunty` command (reconnects the app).
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/iamd2epak/pocketbunty/main/v2/setup.sh)
#
# Options: --full (also install git, GitHub CLI and OpenCode here), --reinstall-opencode, --no-start

set -u
set -o pipefail

BASE_URL="${POCKETBUNTY_BASE_URL:-https://raw.githubusercontent.com/iamd2epak/pocketbunty/main/v2}"
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
    --bridge-only|--no-jcode) WANT_ENV=false ;;
    --reinstall-opencode|--update-jcode) WANT_ENV=true; ENV_ARGS+=(--reinstall-opencode) ;;
    --no-start) WANT_START=false ;;
    --full) WANT_ENV=true ;;
  esac
done

# ---------- output helpers ----------
if [ -t 1 ]; then
  C_OK=$'\033[1;32m' C_WARN=$'\033[1;33m' C_ERR=$'\033[1;31m' C_STEP=$'\033[1;36m' C_DIM=$'\033[2m' C_BOLD=$'\033[1m' C_OFF=$'\033[0m'
  TTY=true
else
  C_OK="" C_WARN="" C_ERR="" C_STEP="" C_DIM="" C_BOLD="" C_OFF=""
  TTY=false
fi
STEP_NO=0
STEPS=6
PROBLEMS=()
banner() {
  printf '\n%s┌──────────────────────────────────────┐%s\n' "$C_STEP" "$C_OFF"
  printf '%s│%s  %s%-36s%s%s│%s\n' "$C_STEP" "$C_OFF" "$C_BOLD" "$1" "$C_OFF" "$C_STEP" "$C_OFF"
  [ -n "${2:-}" ] && printf '%s│%s  %-36s%s│%s\n' "$C_STEP" "$C_OFF" "$2" "$C_STEP" "$C_OFF"
  printf '%s└──────────────────────────────────────┘%s\n' "$C_STEP" "$C_OFF"
}
step() { STEP_NO=$((STEP_NO + 1)); printf '\n%s[%d/%d] %s%s\n' "$C_STEP" "$STEP_NO" "$STEPS" "$*" "$C_OFF"; }
ok()   { printf '  %s✓%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
note() { printf '  %s%s%s\n' "$C_DIM" "$*" "$C_OFF"; }
problem() { PROBLEMS+=("$*"); printf '  %s✗ %s%s\n' "$C_ERR" "$*" "$C_OFF"; }
die() {
  printf '\n%s✗ Setup stopped: %s%s\n' "$C_ERR" "$*" "$C_OFF"
  printf '  Full log: %s\n  Fix the problem above and paste the same command again.\n\n' "$LOG"
  exit 1
}
have() { command -v "$1" >/dev/null 2>&1; }

# Runs a command quietly (output to the log); a spinner with the time shows it's still working.
quiet() {
  if [ "$TTY" != true ]; then "$@" >>"$LOG" 2>&1; return $?; fi
  "$@" >>"$LOG" 2>&1 &
  local pid=$! i=0 start=$SECONDS chars='|/-\'
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r  %s%s working… %ds%s ' "$C_DIM" "${chars:i++%4:1}" $((SECONDS - start)) "$C_OFF"
    sleep 0.3
  done
  printf '\r\033[K'
  wait "$pid"
}

# Quiet, retried a few times for flaky networks.
run_retry() {
  local tries=$1; shift
  local n=1
  while true; do
    if quiet "$@"; then return 0; fi
    [ "$n" -ge "$tries" ] && return 1
    note "retrying ($n/$((tries - 1)))…"
    sleep $((n * 3))
    n=$((n + 1))
  done
}

# ---------- 0. checks ----------
mkdir -p "$BRIDGE_DIR" 2>/dev/null
: >"$LOG" 2>/dev/null || LOG=/dev/null
[ "$WANT_ENV" = true ] || STEPS=5
banner "🐰  PocketBunty setup" "About 1-3 minutes. Keep Termux open."

if [ -z "${PREFIX:-}" ] || [ ! -d "${PREFIX:-/nonexistent}" ] || ! have apt-get; then
  die "this script must run inside the Termux app."
fi

# The Google Play build of Termux can't install glibc, so the AI coder can't run there.
if [ "${TERMUX_APK_RELEASE:-}" = "GOOGLE_PLAY_STORE" ]; then
  printf '\n%s✗ This Termux is from Google Play and cannot run the AI coder.%s\n' "$C_ERR" "$C_OFF"
  printf '  1. Uninstall this Termux.\n  2. Install Termux from GitHub: https://github.com/termux/termux-app/releases/latest\n'
  printf '     (the file that ends in universal.apk), or from F-Droid.\n  3. Open the new Termux and paste the same command again.\n\n'
  exit 1
fi

case "$(uname -m)" in
  aarch64|arm64|x86_64) ;;
  *) warn "This phone's CPU ($(uname -m)) is 32-bit: the AI coder needs a 64-bit phone. Only the terminal will work." ;;
esac

free_kb="$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')"
if [ -n "$free_kb" ] && [ "$free_kb" -lt 102400 ] 2>/dev/null; then
  die "not enough free storage: need at least 100 MB, this phone has $((free_kb / 1024)) MB free. Free some space first."
elif [ -n "$free_kb" ] && [ "$free_kb" -lt 716800 ] 2>/dev/null; then
  warn "only $((free_kb / 1024)) MB free: the AI coder needs about 700 MB later. Free some space."
fi

# One setup at a time.
LOCK="${TMPDIR:-$PREFIX/tmp}/pocketbunty-setup.lock"
mkdir -p "$(dirname "$LOCK")" 2>/dev/null
if ! mkdir "$LOCK" 2>/dev/null; then
  # Stale lock from a killed run (older than 30 min)?
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
    rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || die "can't create $LOCK"
  else
    die "setup is already running in another Termux session. Wait for it, or close that session."
  fi
fi
trap 'rm -rf "$LOCK"' EXIT

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

step "Updating Termux (first time can take a few minutes)"
quiet dpkg --configure -a
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
# Old libraries + new packages = "CANNOT LINK EXECUTABLE" errors, so upgrade everything first.
if run_retry 2 apt-get "${APT_OPTS[@]}" full-upgrade; then
  ok "installed packages are up to date"
else
  quiet dpkg --configure -a
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

step "Installing basic tools"
pkg_need python python || die "couldn't install Python. See $LOG"
pkg_need python-pip pip || problem "pip couldn't be installed (needed for the bridge)"
pkg_need curl curl || die "couldn't install curl. See $LOG"
curl --version >/dev/null 2>&1 || die "curl is broken (old libraries). Run: apt update && apt full-upgrade, then this command again."
pkg_need procps pgrep || warn "procps not installed (bridge auto-start check is weaker)"
pkg_need termux-am am || warn "termux-am not installed (the app may need the token pasted by hand)"
pkg_need coreutils timeout || true

# ---------- 2. bridge ----------
step "Setting up the bridge (links the app to Termux)"
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
# Downloads one file of ours, checks it, then replaces the old one (keeps the old one if offline).
fetch() {
  local name=$1 dest=$2 marker=$3 check=$4
  if run_retry 3 curl -fsSL --connect-timeout 15 -o "$dest.download" "$BASE_URL/$name" \
     && grep -q "$marker" "$dest.download" && $check "$dest.download" 2>>"$LOG"; then
    mv -f "$dest.download" "$dest" && ok "$name downloaded"
    return 0
  fi
  rm -f "$dest.download"
  return 1
}
py_check() { python -m py_compile "$1"; }
sh_check() { bash -n "$1"; }
fetch server.py "$SERVER" "bridge server" py_check \
  || { [ -f "$SERVER" ] && warn "couldn't download the latest server.py, keeping the one you have" || die "couldn't download server.py. Check your internet connection."; }
ENV_SH="$BRIDGE_DIR/env.sh"
fetch env.sh "$ENV_SH" "pocketbunty-env" sh_check || warn "couldn't download env.sh now (the app downloads it later)"
fetch agent.py "$BRIDGE_DIR/agent.py" "pocketbunty-agent" py_check || warn "couldn't download agent.py now (retried in the next step)"
rm -rf "$BRIDGE_DIR/__pycache__" 2>/dev/null

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
URL="https://raw.githubusercontent.com/iamd2epak/pocketbunty/main/v2/setup.sh"
if [ "${1:-}" != setup ] && [ -f "$DIR/server.py" ] && [ -s "$DIR/token" ] && [ -x "$DIR/start.sh" ] && [ -f "$DIR/agent.py" ]; then
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

# Every Termux login shell: start the bridge if needed, and find opencode in ~/.local/bin.
PROFILE_D="$PREFIX/etc/profile.d"
mkdir -p "$PROFILE_D"
rm -f "$PROFILE_D/codenest.sh" 2>/dev/null
cat >"$PROFILE_D/pocketbunty.sh" <<'EOF2'
# Added by PocketBunty setup.
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
[ -x "$HOME/.pocketbunty/start.sh" ] && "$HOME/.pocketbunty/start.sh" >/dev/null 2>&1
EOF2
ok "bridge starts by itself when you open Termux"

# ---------- 3+4. git, GitHub CLI, OpenCode ----------
if [ "$WANT_ENV" = true ]; then
  step "Installing the AI coder, Git and GitHub (biggest step)"
  if [ -f "$ENV_SH" ]; then
    # env.sh prints its own [n/4] parts (with a download bar); its problems show in the summary below.
    bash "$ENV_SH" "${ENV_ARGS[@]}"
    if [ "$(sed -n 's/^state=//p' "$BRIDGE_DIR/env.state" 2>/dev/null)" = done ]; then
      ok "AI coder ready"
    else
      problems="$(sed -n 's/^problems=//p' "$BRIDGE_DIR/env.state" 2>/dev/null)"
      IFS='|' read -r -a env_problems <<<"$problems"
      for p in "${env_problems[@]}"; do [ -n "$p" ] && PROBLEMS+=("$p"); done
      [ ${#env_problems[@]} -eq 0 ] && PROBLEMS+=("the AI coder setup didn't finish (see ~/.pocketbunty/env.log)")
    fi
  else
    problem "env.sh is missing. Paste the same command again."
  fi
fi

# ---------- clean up the old layout ----------
if ! inside_bridge && [ -d "$OLD_DIR" ]; then
  rm -rf "$OLD_DIR/__pycache__" 2>/dev/null
  rm -f "$OLD_DIR/server.py" "$OLD_DIR/start.sh" "$OLD_DIR/token" "$OLD_DIR/server.log" \
    "$OLD_DIR/server.pid" "$OLD_DIR/.server.py.download" 2>/dev/null
  rmdir "$OLD_DIR" 2>/dev/null && ok "old ~/bridge folder removed (now in ~/.pocketbunty)"
fi
rm -f "$HOME/pocketbunty-setup.sh" "$HOME/.pocketbunty-setup.log" 2>/dev/null

# ---------- 5. start and link ----------
health() { curl -fsS --max-time 3 -H "X-Token: $TOKEN" "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; }
started=false
if [ "$WANT_START" = true ]; then
  step "Starting the bridge"
  if inside_bridge; then
    warn "running inside the app's terminal: restart the bridge later from Termux (type: pocketbunty)"
  else
    # Restart so the new server.py runs (also stops an old bridge from ~/bridge).
    if have pkill; then pkill -f "bridge/server\.py|\.pocketbunty/server\.py" >/dev/null 2>&1; sleep 1; fi
    rm -f "$BRIDGE_DIR/server.pid"
    "$START"
  fi
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
fi

# ---------- link and summary ----------
linked=false
if [ "$WANT_START" = true ]; then
  step "Opening the app"
  if [ "$started" = true ] && have am && am start -a android.intent.action.VIEW -d "$APP_LINK?token=$TOKEN" >>"$LOG" 2>&1; then
    linked=true
    ok "PocketBunty opened"
  fi
fi

if [ ${#PROBLEMS[@]} -gt 0 ]; then
  printf '\n%s┌─ Setup finished with %d problem(s) ─┐%s\n' "$C_WARN" "${#PROBLEMS[@]}" "$C_OFF"
  for p in "${PROBLEMS[@]}"; do printf '  %s•%s %s\n' "$C_WARN" "$C_OFF" "$p"; done
  printf '  Paste the same command again to retry. Log: %s\n' "$LOG"
fi
if [ "$started" = true ]; then
  banner "✓  Bridge ready!" "The app installs the AI coder next."
  # Android may not let Termux open the app (e.g. the screen was off): the code always works.
  printf '  If PocketBunty didn'"'"'t open, open it and enter this code:\n\n'
  printf '      %s%s%s\n\n' "$C_BOLD$C_WARN" "$TOKEN" "$C_OFF"
  [ "$linked" = true ] || note "Tap the code to copy it (long-press → Copy)."
  printf '  Next time just open Termux, or type: %spocketbunty%s\n\n' "$C_BOLD" "$C_OFF"
fi
exit 0
