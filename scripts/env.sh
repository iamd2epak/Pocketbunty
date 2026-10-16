#!/data/data/com.termux/files/usr/bin/bash
# pocketbunty-env: installs the coding environment (git, GitHub CLI, jcode) after the bridge is set up.
#
# The app starts it in the background through the bridge (detached, so it keeps going if the app
# is closed) and shows progress from ~/.pocketbunty/env.state. It also works in a terminal:
#
#   bash ~/.pocketbunty/env.sh            (or: bash <(curl -fsSL …/scripts/env.sh))
#
# Safe to run again: finished parts are skipped, broken ones repaired.
# Options: --update-jcode (reinstall jcode at its latest version)

set -u
set -o pipefail
trap '' HUP

DIR="$HOME/.pocketbunty"
STATE="$DIR/env.state"
LOG="$DIR/env.log"
LOCK="$DIR/env.lock"
JCODE_INSTALL_URL="https://jcode.sh/install"
JCODE_BIN="$HOME/.local/bin/jcode"
export PATH="$HOME/.local/bin:$PATH"
export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y --no-install-recommends -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -o Acquire::Retries=3)

UPDATE_JCODE=false
for arg in "$@"; do
  case "$arg" in
    --update-jcode) UPDATE_JCODE=true ;;
  esac
done

mkdir -p "$DIR" || { echo "can't create $DIR"; exit 1; }

# ---------- state for the app ----------
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
step() { STEP=$((STEP + 1)); LABEL="$*"; printf '\n[%d/%d] %s\n' "$STEP" "$TOTAL" "$*"; write_state running; }
ok()   { printf '  ✓ %s\n' "$*"; }
problem() { PROBLEMS+=("$*"); printf '  ✗ %s\n' "$*"; write_state running; }
have() { command -v "$1" >/dev/null 2>&1; }

# Quiet (output to the log), retried for flaky networks.
run_retry() {
  local tries=$1; shift
  local n=1
  while true; do
    if "$@" >>"$LOG" 2>&1; then return 0; fi
    [ "$n" -ge "$tries" ] && return 1
    sleep $((n * 3))
    n=$((n + 1))
  done
}

# ---------- one run at a time ----------
if [ -s "$LOCK" ]; then
  other="$(cat "$LOCK" 2>/dev/null)"
  if [ -n "$other" ] && [ "$other" != "$$" ] && tr '\0' ' ' </proc/"$other"/cmdline 2>/dev/null | grep -q "env.sh"; then
    echo "Already running (pid $other)."
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
echo "PocketBunty environment setup (log: $LOG)"

if [ -z "${PREFIX:-}" ] || ! have apt-get; then
  PROBLEMS+=("This must run inside Termux")
  write_state failed
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
# Args: pkg:cmd pairs.
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
run_retry 1 dpkg --configure -a
# Skip the package list download if Termux setup just did it (last hour).
lists="$PREFIX/var/lib/apt/lists"
if [ -z "$(find "$lists" -maxdepth 1 -name '*Packages' -mmin -60 2>/dev/null | head -n1)" ]; then
  run_retry 3 apt_update || problem "Couldn't update the package list (check internet)"
fi
ok "Termux ready"

# ---------- 2. git + GitHub ----------
step "Installing Git and GitHub"
install_missing git:git gh:gh procps:pgrep tar:tar
have git && ok "git" || problem "git couldn't be installed"
have gh && ok "GitHub CLI" || problem "GitHub CLI (gh) couldn't be installed"
if have git; then
  git config --global init.defaultBranch >/dev/null 2>&1 || git config --global init.defaultBranch main
  git config --global --get safe.directory >/dev/null 2>&1 || git config --global --add safe.directory '*'
fi

# ---------- 3. jcode ----------
jcode_works() { [ -x "$JCODE_BIN" ] && "$JCODE_BIN" --version >/dev/null 2>&1; }
case "$(uname -m)" in aarch64|arm64|x86_64) ARCH_OK=true ;; *) ARCH_OK=false ;; esac

step "Getting the AI coder ready"
if [ "$ARCH_OK" != true ]; then
  problem "The AI coder doesn't support this phone's CPU ($(uname -m))"
elif jcode_works && [ "$UPDATE_JCODE" != true ]; then
  ok "already installed"
else
  # jcode is a glibc program: Termux needs glibc + patchelf to run it.
  if [ ! -d "$PREFIX/glibc/lib" ]; then
    if ! dpkg -s glibc-repo >/dev/null 2>&1; then
      run_retry 2 apt_get install glibc-repo && run_retry 2 apt_update || problem "glibc-repo couldn't be installed"
    fi
    run_retry 2 apt_get install glibc || problem "glibc couldn't be installed"
  fi
  install_missing patchelf:patchelf
  have patchelf || problem "patchelf couldn't be installed"
  ok "helpers installed"
fi

step "Installing the AI coder (jcode)"
if [ "$ARCH_OK" = true ] && { ! jcode_works || [ "$UPDATE_JCODE" = true ]; }; then
  jcode_install() { curl -fsSL --connect-timeout 15 --retry 2 "$JCODE_INSTALL_URL" | bash; }
  if run_retry 3 jcode_install && jcode_works; then
    ok "$("$JCODE_BIN" --version 2>/dev/null | head -n1) installed"
  else
    problem "jcode didn't install (check internet and try again)"
  fi
elif jcode_works; then
  ok "$("$JCODE_BIN" --version 2>/dev/null | head -n1)"
fi

# ---------- done ----------
LABEL="Done"
if jcode_works && have git; then
  write_state done
  printf '\n✓ All set! The AI coder is ready.\n'
else
  write_state failed
  printf '\nSetup finished with problems:\n'
  for p in "${PROBLEMS[@]}"; do printf '  - %s\n' "$p"; done
  printf 'Run it again to retry. Log: %s\n' "$LOG"
fi
exit 0
