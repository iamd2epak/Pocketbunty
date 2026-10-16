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
# Android has no /tmp; keep temp files next to ours (same storage, survives a missing TMPDIR).
export TMPDIR="$DIR/tmp"
APT_OPTS=(-y --no-install-recommends -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -o Acquire::Retries=3)

UPDATE_JCODE=false
for arg in "$@"; do
  case "$arg" in
    --update-jcode) UPDATE_JCODE=true ;;
  esac
done

mkdir -p "$DIR" "$DIR/tmp" || { echo "can't create $DIR"; exit 1; }

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
install_missing git:git gh:gh procps:pgrep tar:tar coreutils:sha256sum gzip:gzip
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
  # Libraries jcode links to (libssl/libcrypto, libgcc_s) and TLS certificates, glibc builds.
  glibc_libs=()
  [ -e "$PREFIX/glibc/lib/libssl.so.3" ] || glibc_libs+=(openssl-glibc)
  [ -e "$PREFIX/glibc/lib/libgcc_s.so.1" ] || glibc_libs+=(gcc-libs-glibc)
  dpkg -s ca-certificates-glibc >/dev/null 2>&1 || glibc_libs+=(ca-certificates-glibc)
  if [ ${#glibc_libs[@]} -gt 0 ]; then
    run_retry 2 apt_get install "${glibc_libs[@]}" || { run_retry 2 apt_update; run_retry 2 apt_get install "${glibc_libs[@]}"; } \
      || problem "glibc libraries couldn't be installed (${glibc_libs[*]})"
  fi
  install_missing patchelf:patchelf
  have patchelf || problem "patchelf couldn't be installed"
  ok "helpers installed"
fi

step "Installing the AI coder (jcode)"
# Own installer (same layout as jcode.sh/install), so each failure says what went wrong and a
# half-done download is resumed. Falls back to the official installer if this one fails.
JCODE_REPO="1jehuang/jcode"
JCODE_META="https://jcode.sh/releases"
JERR=""
jfail() { JERR="$*"; printf '  ! %s\n' "$*"; echo "jcode: $*" >>"$LOG"; return 1; }
jcurl() { curl -fsSL --connect-timeout 15 --retry 3 --retry-delay 3 "$@"; }

jcode_own_install() {
  local arch artifact version bases base sums want got tmp vdir linker free rc
  case "$(uname -m)" in aarch64|arm64) arch=aarch64 ;; x86_64) arch=x86_64 ;; *) jfail "unsupported CPU"; return 1 ;; esac
  artifact="jcode-linux-$arch"
  linker="$PREFIX/glibc/lib/ld-linux-aarch64.so.1"
  [ "$arch" = x86_64 ] && linker="$PREFIX/glibc/lib/ld-linux-x86-64.so.2"
  [ -x "$linker" ] || { jfail "glibc is missing (Termux package glibc)"; return 1; }
  have patchelf || { jfail "patchelf is missing"; return 1; }
  have sha256sum || { jfail "sha256sum is missing"; return 1; }

  # Needs ~400 MB: 50 MB download + 150 MB program (+ the old one while updating).
  free="$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')"
  if [ -n "$free" ] && [ "$free" -lt 409600 ] 2>/dev/null; then
    jfail "Not enough free storage (need 400 MB, have $((free / 1024)) MB)"; return 1
  fi

  # GitHub first (jcode.sh's copy can lag behind), jcode.sh if GitHub is unreachable.
  version="$(curl -fsSIL --connect-timeout 15 --max-time 30 -o /dev/null -w '%{url_effective}' "https://github.com/$JCODE_REPO/releases/latest" 2>>"$LOG")"
  version="${version##*/}"
  if ! printf '%s' "$version" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+'; then
    version="$(jcurl --max-time 30 "$JCODE_META/latest/version" 2>>"$LOG" | tr -d '\r\n')"
  fi
  printf '%s' "$version" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+' || { jfail "Couldn't reach jcode.sh (check internet)"; return 1; }

  sums="$(jcurl --max-time 30 "$JCODE_META/$version/SHA256SUMS" 2>>"$LOG" || jcurl --max-time 30 "https://github.com/$JCODE_REPO/releases/download/$version/SHA256SUMS" 2>>"$LOG")"
  want="$(printf '%s\n' "$sums" | awk -v a="$artifact.tar.gz" '$2 == a || $2 == "*" a { print tolower($1); exit }')"
  [ ${#want} -eq 64 ] || { jfail "Couldn't get the jcode checksum (check internet)"; return 1; }

  tmp="$DIR/tmp"; mkdir -p "$tmp"
  local dl="$tmp/$artifact-$version.tar.gz"
  bases="$(jcurl --max-time 30 "$JCODE_META/$version/download-bases" 2>>"$LOG" | grep -E '^https://[^[:space:]]+$')"
  bases="$(printf '%s\nhttps://github.com/%s/releases/download/%s\n' "$bases" "$JCODE_REPO" "$version" | awk 'NF && !seen[$0]++')"
  got=""
  # A finished file from an earlier try is reused; a broken one is downloaded again.
  [ -f "$dl" ] && [ "$(sha256sum "$dl" | awk '{print $1}')" = "$want" ] && got="$want"
  if [ -z "$got" ]; then
    for base in $bases; do
      for _ in 1 2 3; do
        # -C - resumes a half-done download (slow or dropped networks).
        curl -fsSL --connect-timeout 15 --speed-limit 1024 --speed-time 60 -C - -o "$dl" "${base%/}/$artifact.tar.gz" >>"$LOG" 2>&1
        rc=$?
        got="$(sha256sum "$dl" 2>/dev/null | awk '{print $1}')"
        [ "$got" = "$want" ] && break 2
        # Finished but wrong, or the server can't resume: start over.
        case $rc in 0|22|33|36) rm -f "$dl" ;; esac
        sleep 3
      done
    done
  fi
  [ "$got" = "$want" ] || { rm -f "$dl"; jfail "Download failed (check internet and try again)"; return 1; }

  vdir="$HOME/.jcode/builds/versions/${version#v}"
  rm -rf "$tmp/x"; mkdir -p "$tmp/x" "$vdir" "$HOME/.jcode/builds/stable" "$HOME/.local/bin"
  tar xzf "$dl" -C "$tmp/x" >>"$LOG" 2>&1 || { rm -rf "$tmp/x"; jfail "Couldn't unpack jcode (storage full?)"; return 1; }
  [ -f "$tmp/x/$artifact" ] || { rm -rf "$tmp/x"; jfail "The jcode download looks wrong"; return 1; }
  find "$tmp/x" -maxdepth 1 -type f \( -name "$artifact.bin" -o -name 'libssl.so*' -o -name 'libcrypto.so*' \) -exec cp -f {} "$vdir/" \;
  mv -f "$tmp/x/$artifact" "$vdir/jcode" || { rm -rf "$tmp/x"; jfail "Couldn't save jcode (storage full?)"; return 1; }
  rm -rf "$tmp/x"
  chmod +x "$vdir/jcode"
  # Some builds are a small start script plus the program in <name>.bin: patch the real program.
  local elf="$vdir/jcode"
  [ "$(head -c 4 "$elf" | od -An -c | tr -d ' ')" = '177ELF' ] || elf="$vdir/$artifact.bin"
  patchelf --set-interpreter "$linker" "$elf" >>"$LOG" 2>&1 || { jfail "Couldn't prepare jcode for Termux (patchelf)"; return 1; }
  ln -sfn "$vdir/jcode" "$HOME/.jcode/builds/stable/jcode"
  printf '%s\n' "${version#v}" >"$HOME/.jcode/builds/stable-version"
  rm -f "$JCODE_BIN"
  # Same launcher the official installer writes on Termux (absolute bash: no /usr/bin/env here).
  printf '#!%s/bin/bash\nunset LD_PRELOAD\nexec "%s" "$@"\n' "$PREFIX" "$HOME/.jcode/builds/stable/jcode" >"$JCODE_BIN"
  chmod +x "$JCODE_BIN"
  grep -qF '.local/bin' "$HOME/.bashrc" 2>/dev/null || printf '\n# Added by jcode installer\nexport PATH="%s:$PATH"\n' "$HOME/.local/bin" >>"$HOME/.bashrc"
  if ! jcode_works; then
    local why lib
    why="$("$JCODE_BIN" --version 2>&1 | head -n3)"
    echo "$why" >>"$LOG"
    lib="$(printf '%s' "$why" | sed -n 's/.*loading shared libraries: \([^:]*\):.*/\1/p' | head -n1)"
    [ -n "$lib" ] && { jfail "jcode needs $lib, which isn't installed"; return 1; }
    jfail "jcode was installed but doesn't start on this phone"; return 1
  fi
  rm -f "$dl"
  # Keep only the newest 2 versions (each is ~150 MB).
  ls -1t "$HOME/.jcode/builds/versions" 2>/dev/null | tail -n +3 | while read -r old; do
    rm -rf "$HOME/.jcode/builds/versions/$old"
  done
  return 0
}

jcode_official_install() {
  local f="$DIR/tmp/jcode-install.sh"
  mkdir -p "$DIR/tmp"
  jcurl --max-time 60 "$JCODE_INSTALL_URL" -o "$f" >>"$LOG" 2>&1 || return 1
  JCODE_SKIP_SERVER_RELOAD=1 TMPDIR="$DIR/tmp" bash "$f" </dev/null >>"$LOG" 2>&1
  local rc=$?; rm -f "$f"; return $rc
}

if [ "$ARCH_OK" = true ] && { ! jcode_works || [ "$UPDATE_JCODE" = true ]; }; then
  if jcode_own_install; then
    ok "$("$JCODE_BIN" --version 2>/dev/null | head -n1) installed"
  else
    own_err="$JERR"
    echo "trying the official jcode installer" >>"$LOG"
    if jcode_official_install && jcode_works; then
      ok "$("$JCODE_BIN" --version 2>/dev/null | head -n1) installed"
    else
      problem "jcode: ${own_err:-install failed} (log: ~/.pocketbunty/env.log)"
    fi
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
