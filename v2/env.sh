#!/data/data/com.termux/files/usr/bin/bash
# pocketbunty-env v2: installs the coding environment (git, GitHub CLI, OpenCode) for PocketBunty.
#
# setup.sh runs it in the Termux window during the first setup. The app also starts it in the
# background (detached) to repair a broken setup, and shows progress from ~/.pocketbunty/env.state.
#
#   bash ~/.pocketbunty/env.sh            (or: bash <(curl -fsSL …/v2/env.sh))
#
# Safe to run again: finished parts are skipped, broken ones repaired.
# Options: --reinstall-opencode (download OpenCode again)

set -u
set -o pipefail
trap '' HUP

BASE_URL="${POCKETBUNTY_BASE_URL:-https://raw.githubusercontent.com/iamd2epak/pocketbunty/main/v2}"
DIR="$HOME/.pocketbunty"
STATE="$DIR/env.state"
LOG="$DIR/env.log"
LOCK="$DIR/env.lock"
OC_DIR="$DIR/opencode"
OC_BIN="$OC_DIR/opencode"
OC_VERSION="1.19.5"   # pinned: tested on phones. Change only after testing a new one.
AGENT="$DIR/agent.py"
export PATH="$HOME/.local/bin:$PATH"
export DEBIAN_FRONTEND=noninteractive
# Android has no /tmp; keep temp files next to ours.
export TMPDIR="$DIR/tmp"
APT_OPTS=(-y --no-install-recommends -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -o Acquire::Retries=3)

REINSTALL=false
for arg in "$@"; do
  case "$arg" in
    --reinstall-opencode) REINSTALL=true ;;
  esac
done

mkdir -p "$DIR" "$DIR/tmp" || { echo "can't create $DIR"; exit 1; }

# ---------- output (colors and a spinner in a terminal, plain lines in the background) ----------
if [ -t 1 ]; then
  C_OK=$'\033[1;32m' C_WARN=$'\033[1;33m' C_ERR=$'\033[1;31m' C_STEP=$'\033[1;36m' C_DIM=$'\033[2m' C_OFF=$'\033[0m'
  TTY=true
else
  C_OK="" C_WARN="" C_ERR="" C_STEP="" C_DIM="" C_OFF=""
  TTY=false
fi

TOTAL=4
STEP=0
LABEL="Starting"
PROBLEMS=()
write_state() {
  local problems=""
  [ ${#PROBLEMS[@]} -gt 0 ] && problems="$(IFS='|'; echo "${PROBLEMS[*]}")"
  {
    echo "state=$1"
    echo "step=$STEP"
    echo "total=$TOTAL"
    echo "label=$LABEL"
    echo "pid=$$"
    echo "problems=$problems"
  } >"$STATE.tmp" 2>/dev/null && mv -f "$STATE.tmp" "$STATE"
}
step() { STEP=$((STEP + 1)); LABEL="$*"; printf '\n%s[%d/%d] %s%s\n' "$C_STEP" "$STEP" "$TOTAL" "$*" "$C_OFF"; write_state running; }
ok()   { printf '  %s✓%s %s\n' "$C_OK" "$C_OFF" "$*"; }
note() { printf '  %s%s%s\n' "$C_DIM" "$*" "$C_OFF"; }
problem() { PROBLEMS+=("$*"); printf '  %s✗ %s%s\n' "$C_ERR" "$*" "$C_OFF"; write_state running; }
have() { command -v "$1" >/dev/null 2>&1; }

# Runs a command quietly (output to the log). In a terminal a spinner shows it's still working.
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

# Quiet, retried for flaky networks.
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

# ---------- one run at a time ----------
if [ -s "$LOCK" ]; then
  other="$(cat "$LOCK" 2>/dev/null)"
  if [ -n "$other" ] && [ "$other" != "$$" ] && tr '\0' ' ' </proc/"$other"/cmdline 2>/dev/null | grep -q "env.sh"; then
    echo "Already running (pid $other). Wait for it to finish."
    exit 0
  fi
fi
echo $$ >"$LOCK"
WAKE=false
finish() {
  rm -f "$LOCK"
  [ "$WAKE" = true ] && timeout 10 termux-wake-unlock >/dev/null 2>&1
}
trap finish EXIT
# Something killed us mid-way: tell the app instead of looking stuck.
trap 'PROBLEMS+=("Setup was stopped"); write_state failed; exit 1' INT TERM

: >"$LOG" 2>/dev/null || LOG=/dev/null
write_state running

if [ -z "${PREFIX:-}" ] || ! have apt-get; then
  PROBLEMS+=("This must run inside Termux")
  write_state failed
  echo "This must run inside the Termux app."
  exit 1
fi
if [ "${TERMUX_APK_RELEASE:-}" = "GOOGLE_PLAY_STORE" ]; then
  PROBLEMS+=("This Termux is from Google Play and can't run the AI coder. Install Termux from GitHub or F-Droid")
  write_state failed
  printf '%s✗ This Termux is from Google Play and cannot run the AI coder.%s\n' "$C_ERR" "$C_OFF"
  exit 1
fi

# Keep the phone from sleeping Termux while installing (released at the end).
if have termux-wake-lock && timeout 10 termux-wake-lock >/dev/null 2>&1; then WAKE=true; fi

# apt can't run twice at once: wait for another install (e.g. in Termux) to finish.
apt_wait() {
  local i=0
  while [ $i -lt 120 ] && { pgrep -x apt-get >/dev/null 2>&1 || pgrep -x apt >/dev/null 2>&1 || pgrep -x dpkg >/dev/null 2>&1; }; do
    sleep 3; i=$((i + 1))
  done
}
apt_get() { apt_wait; apt-get "${APT_OPTS[@]}" "$@"; }
apt_update() { apt_wait; apt-get update -o Acquire::Retries=3; }

# Installs the packages whose command is missing, all at once (fast), then one by one if that fails.
install_missing() {
  local want=() pair pkg cmd
  for pair in "$@"; do
    pkg="${pair%%:*}"; cmd="${pair#*:}"
    have "$cmd" || want+=("$pkg")
  done
  [ ${#want[@]} -eq 0 ] && return 0
  run_retry 2 apt_get install "${want[@]}" && return 0
  for pkg in "${want[@]}"; do run_retry 2 apt_get install "$pkg"; done
  return 0
}

# ---------- 1. Termux ----------
step "Preparing Termux"
quiet dpkg --configure -a
# Skip the package list download if the setup just did it (last hour).
lists="$PREFIX/var/lib/apt/lists"
if [ -z "$(find "$lists" -maxdepth 1 -name '*Packages' -mmin -60 2>/dev/null | head -n1)" ]; then
  run_retry 3 apt_update || problem "Couldn't update the package list (check internet)"
fi
# Old libraries + new packages = "CANNOT LINK EXECUTABLE": upgrade what's installed first.
if ! curl --version >/dev/null 2>&1 || ! python -c 'import ssl' >/dev/null 2>&1; then
  run_retry 2 apt_get full-upgrade || problem "Termux packages couldn't be upgraded (run: apt update && apt full-upgrade)"
fi
ok "Termux ready"

# ---------- 2. git + GitHub ----------
step "Installing Git and GitHub"
install_missing git:git gh:gh procps:pgrep tar:tar coreutils:timeout gzip:gzip ripgrep:rg
have git && ok "git" || problem "git couldn't be installed"
have gh && ok "GitHub CLI" || problem "GitHub CLI (gh) couldn't be installed"
have rg && ok "ripgrep (fast search for the AI)" || note "ripgrep not installed (the AI searches a bit slower)"
if have git; then
  git config --global init.defaultBranch >/dev/null 2>&1 || git config --global init.defaultBranch main
  git config --global --get safe.directory >/dev/null 2>&1 || git config --global --add safe.directory '*'
fi

# ---------- 3. helpers for OpenCode ----------
oc_works() { [ -x "$OC_BIN" ] && [ "$(env -u LD_PRELOAD "$OC_BIN" --version 2>/dev/null | tr -d '\r' | tail -n1)" = "$OC_VERSION" ]; }
case "$(uname -m)" in
  aarch64|arm64) OC_PKG=opencode-linux-arm64; LINKER="$PREFIX/glibc/lib/ld-linux-aarch64.so.1" ;;
  x86_64) OC_PKG=opencode-linux-x64; LINKER="$PREFIX/glibc/lib/ld-linux-x86-64.so.2" ;;
  *) OC_PKG="" ;;
esac

step "Getting the AI coder ready"
if [ -z "$OC_PKG" ]; then
  problem "The AI coder needs a 64-bit phone (this one is $(uname -m))"
elif oc_works && [ "$REINSTALL" != true ]; then
  ok "already installed"
else
  # OpenCode is a glibc program: Termux needs glibc + patchelf to run it.
  if [ ! -x "$LINKER" ]; then
    if ! dpkg -s glibc-repo >/dev/null 2>&1; then
      run_retry 2 apt_get install glibc-repo && run_retry 2 apt_update || problem "glibc-repo couldn't be installed"
    fi
    run_retry 2 apt_get install glibc || problem "glibc couldn't be installed"
  fi
  install_missing patchelf:patchelf
  [ -x "$LINKER" ] && have patchelf && ok "helpers installed" || problem "glibc or patchelf is missing"
fi

# ---------- 4. OpenCode + PocketBunty's link to it ----------
step "Installing the AI coder (OpenCode $OC_VERSION)"
OERR=""
ofail() { OERR="$*"; echo "opencode: $*" >>"$LOG"; return 1; }

oc_install() {
  local free tgz want got url tmp="$DIR/tmp"
  [ -x "$LINKER" ] || { ofail "glibc is missing (Termux package glibc)"; return 1; }
  have patchelf || { ofail "patchelf is missing"; return 1; }
  # ~80 MB download + 210 MB program.
  free="$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')"
  if [ -n "$free" ] && [ "$free" -lt 409600 ] 2>/dev/null; then
    ofail "Not enough free storage (need 400 MB, have $((free / 1024)) MB). Free some space and try again"; return 1
  fi
  tgz="$tmp/$OC_PKG-$OC_VERSION.tgz"
  # The checksum npm publishes (sha512, base64).
  want="$(curl -fsSL --connect-timeout 15 --max-time 60 "https://registry.npmjs.org/$OC_PKG/$OC_VERSION" 2>>"$LOG" \
    | python -c 'import json,sys; print(json.load(sys.stdin)["dist"]["integrity"])' 2>>"$LOG")"
  case "$want" in sha512-*) ;; *) ofail "Couldn't reach npmjs.org (check internet)"; return 1 ;; esac
  sum_ok() {
    [ -f "$tgz" ] && [ "$(python -c 'import base64,hashlib,sys
h = hashlib.sha512()
with open(sys.argv[1], "rb") as f:
    for b in iter(lambda: f.read(1 << 20), b""):
        h.update(b)
print("sha512-" + base64.b64encode(h.digest()).decode())' "$tgz" 2>/dev/null)" = "$want" ]
  }
  if ! sum_ok; then
    for url in "https://registry.npmjs.org/$OC_PKG/-/$OC_PKG-$OC_VERSION.tgz" \
               "https://registry.npmmirror.com/$OC_PKG/-/$OC_PKG-$OC_VERSION.tgz"; do
      for _ in 1 2 3; do
        if [ "$TTY" = true ]; then
          note "downloading (~80 MB)…"
          curl -fL --connect-timeout 15 --speed-limit 1024 --speed-time 60 -C - --progress-bar -o "$tgz" "$url" 2>&1 >>"$LOG" | cat
        else
          curl -fsSL --connect-timeout 15 --speed-limit 1024 --speed-time 60 -C - -o "$tgz" "$url" >>"$LOG" 2>&1
        fi
        sum_ok && break 2
        rm -f "$tgz"   # wrong or broken: start over
        sleep 3
      done
    done
  fi
  sum_ok || { rm -f "$tgz"; ofail "Download failed (check internet and try again)"; return 1; }
  rm -rf "$tmp/oc" && mkdir -p "$tmp/oc" "$OC_DIR"
  quiet tar xzf "$tgz" -C "$tmp/oc" package/bin/opencode || { rm -rf "$tmp/oc"; ofail "Couldn't unpack OpenCode (storage full?)"; return 1; }
  mv -f "$tmp/oc/package/bin/opencode" "$OC_BIN.new" || { rm -rf "$tmp/oc"; ofail "Couldn't save OpenCode (storage full?)"; return 1; }
  rm -rf "$tmp/oc"
  chmod +x "$OC_BIN.new"
  patchelf --set-interpreter "$LINKER" "$OC_BIN.new" >>"$LOG" 2>&1 || { rm -f "$OC_BIN.new"; ofail "Couldn't prepare OpenCode for Termux (patchelf)"; return 1; }
  mv -f "$OC_BIN.new" "$OC_BIN"
  if ! oc_works; then
    local why
    why="$(env -u LD_PRELOAD "$OC_BIN" --version 2>&1 | head -n3)"
    echo "$why" >>"$LOG"
    case "$why" in
      *"loading shared libraries"*) ofail "OpenCode needs a library that isn't installed: $(printf '%s' "$why" | sed -n 's/.*libraries: \([^:]*\):.*/\1/p')" ;;
      *) ofail "OpenCode was installed but doesn't start on this phone ($(printf '%s' "$why" | head -n1 | cut -c1-120))" ;;
    esac
    return 1
  fi
  rm -f "$tgz"
  return 0
}

if [ -n "$OC_PKG" ] && { ! oc_works || [ "$REINSTALL" = true ]; }; then
  if oc_install; then ok "OpenCode $OC_VERSION installed"; else problem "OpenCode: $OERR (log: ~/.pocketbunty/env.log)"; fi
elif oc_works; then
  ok "OpenCode $OC_VERSION"
fi

# The app talks to OpenCode through agent.py.
if run_retry 3 curl -fsSL --connect-timeout 15 -o "$AGENT.download" "$BASE_URL/agent.py" \
   && grep -q "pocketbunty-agent" "$AGENT.download" && python -m py_compile "$AGENT.download" 2>>"$LOG"; then
  mv -f "$AGENT.download" "$AGENT"
else
  rm -f "$AGENT.download"
fi
rm -rf "$DIR/__pycache__" 2>/dev/null
[ -f "$AGENT" ] && ok "PocketBunty link to OpenCode" || problem "Couldn't download agent.py (check internet)"

if oc_works; then
  printf '%s\n' "$OC_VERSION" >"$OC_DIR/version"
  # `opencode` in the Terminal too (same program, without Termux's LD_PRELOAD).
  mkdir -p "$HOME/.local/bin"
  printf '#!%s/bin/bash\nunset LD_PRELOAD\nexport OPENCODE_DISABLE_AUTOUPDATE=1\nexec "%s" "$@"\n' "$PREFIX" "$OC_BIN" >"$HOME/.local/bin/opencode"
  chmod +x "$HOME/.local/bin/opencode"
  # First start downloads OpenCode's list of AI providers and models (needed for the free models).
  quiet timeout 180 env -u LD_PRELOAD OPENCODE_DISABLE_AUTOUPDATE=1 "$OC_BIN" models opencode \
    && ok "free AI models ready" || note "couldn't load the model list now (it loads on the first chat)"
fi

# The old AI coder (jcode) isn't used any more: free its ~300 MB.
if [ -d "$HOME/.jcode/builds" ] || [ -e "$HOME/.local/bin/jcode" ]; then
  rm -rf "$HOME/.jcode/builds" "$HOME/.local/bin/jcode" 2>/dev/null && ok "removed the old AI coder (jcode)"
fi

# ---------- done ----------
LABEL="Done"
if oc_works && [ -f "$AGENT" ] && have git; then
  write_state done
  printf '\n%s✓ The AI coder is ready.%s\n' "$C_OK" "$C_OFF"
else
  write_state failed
  printf '\n%sSetup finished with problems:%s\n' "$C_WARN" "$C_OFF"
  for p in "${PROBLEMS[@]}"; do printf '  - %s\n' "$p"; done
  printf 'Run it again to retry. Log: %s\n' "$LOG"
fi
exit 0
