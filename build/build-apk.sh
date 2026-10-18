# pocketbunty-build-apk version=1 protocol=1
# shellcheck shell=bash
# Builds a test APK of a PocketBunty project on GitHub Actions. No AI involved.
# The app runs it in the project folder (Build APK screen) and reads the "@@" lines it prints:
#   @@STEP <check|save|start|build|link>   a step starts (the ones before it are done)
#   @@NOTE <text>                          detail for the current step
#   @@REPO <owner/name>  @@BRANCH <name>   @@RUN <id> <url>
#   @@ERRLOG_BEGIN … @@ERRLOG_END          the failed build's log (last lines)
#   @@APK <url> <bytes>                    done: the download link
#   @@FAIL <CODE> <message>                stopped (the app picks buttons by CODE)
# Input (env): PB_KIND android|androidjava|expo, PB_MODE build|resume, PB_REQUEST (unique id),
#              PB_RUN_ID + PB_REPO (resume: watch a run that's already going).
# The live copy is fetched from github.com/iamd2epak/pocketbunty (build/build-apk.sh): edit it
# there and raise "version" to change how every app builds. Keep "protocol=1" unless the @@ lines change.

set -u
export PATH="$HOME/.local/bin:$PATH" GH_PROMPT_DISABLED=1 GH_NO_UPDATE_NOTIFIER=1 GIT_TERMINAL_PROMPT=0 \
  NO_COLOR=1 CLICOLOR=0 GH_PAGER=cat PAGER=cat GIT_PAGER=cat LC_ALL=C.UTF-8

KIND="${PB_KIND:-android}"
MODE="${PB_MODE:-build}"
REQ="${PB_REQUEST:-$(date +%s)}"
RUN_ID="${PB_RUN_ID:-}"
REPO="${PB_REPO:-}"
WF=pocketbunty-apk.yml
TAG=pocketbunty-apk

step() { echo "@@STEP $1"; }
note() { echo "@@NOTE $*"; }
fail() { c="$1"; shift; echo "@@FAIL $c $*"; exit 1; }
last_line() { printf '%s\n' "$1" | sed 's/\r//g' | grep -v '^[[:space:]]*$' | tail -n 1 | cut -c1-240; }

# Cancel (Stop in the app): stop the run on GitHub too.
trap 'trap - INT TERM; [ -n "$RUN_ID" ] && [ -n "$REPO" ] && gh run cancel "$RUN_ID" --repo "$REPO" >/dev/null 2>&1; echo "@@FAIL CANCELLED Build cancelled."; exit 130' INT TERM

case "$KIND" in android|androidjava|expo) ;; *) fail KIND "This project type can't be built into an APK here." ;; esac

# ---------------------------------------------------------------- the GitHub workflow (fixed)

wf_head() {
cat <<'EOF'
# Made by the PocketBunty app (Build APK). It rewrites this file before each build:
# don't edit it, changes are lost. Builds a test (debug-signed) APK and puts it on the
# release "pocketbunty-apk".
name: PocketBunty APK
run-name: Build APK (${{ inputs.request }})

on:
  workflow_dispatch:
    inputs:
      request:
        description: Build id from the PocketBunty app
        required: false
        default: manual
      dir:
        description: App folder in the repo
        required: false
        default: .

permissions:
  contents: write

concurrency:
  group: pocketbunty-apk-${{ github.ref }}
  cancel-in-progress: true

jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    env:
      APP_DIR: ${{ inputs.dir }}
    steps:
      - name: Get the code
        uses: actions/checkout@v4
      - name: Set up Java 17
        uses: actions/setup-java@v4
        with:
          distribution: temurin
          java-version: '17'
EOF
}

wf_android() {
cat <<'EOF'
      - name: Set up Gradle
        uses: gradle/actions/setup-gradle@v4
        with:
          gradle-version: '8.14.3'
      - name: Build APK
        run: |
          cd "$APP_DIR"
          [ -n "$(ls settings.gradle* build.gradle* 2>/dev/null)" ] || { echo "::error::No Android project (settings.gradle) in '$APP_DIR'. Ask the AI to create the app first."; exit 1; }
          if [ -f gradlew ] && [ -f gradle/wrapper/gradle-wrapper.jar ]; then
            chmod +x gradlew
            ./gradlew --no-daemon --console=plain assembleDebug
          else
            echo "No Gradle wrapper in the project: using Gradle 8.14.3."
            gradle --no-daemon --console=plain assembleDebug
          fi
          apk="$(find . -path '*/build/outputs/apk/*' -name '*.apk' ! -name '*androidTest*' ! -name '*unsigned*' \
            | awk '{p=0; if ($0 ~ /universal/) p+=4; else if ($0 ~ /arm64/) p+=2; if ($0 ~ /\/debug\//) p+=2; if ($0 ~ /^\.\/app\//) p+=1; print p "\t" $0}' \
            | sort -rn | head -n 1 | cut -f2-)"
          [ -n "$apk" ] || { echo "::error::Gradle finished but made no APK. Is there an app module (com.android.application)?"; exit 1; }
          echo "APK_PATH=$PWD/${apk#./}" >> "$GITHUB_ENV"
EOF
}

wf_expo() {
cat <<'EOF'
      - name: Set up Node.js 22
        uses: actions/setup-node@v4
        with:
          node-version: '22'
      - name: Set up Gradle
        uses: gradle/actions/setup-gradle@v4
      - name: Install packages
        run: |
          cd "$APP_DIR"
          [ -f package.json ] || { echo "::error::No package.json in '$APP_DIR'. Ask the AI to create the app first."; exit 1; }
          if [ -f yarn.lock ]; then
            corepack enable && { yarn install --frozen-lockfile || yarn install; }
          elif [ -f pnpm-lock.yaml ]; then
            corepack enable && pnpm install
          elif [ -f package-lock.json ]; then
            npm ci --no-audit --no-fund || npm install --no-audit --no-fund || npm install --no-audit --no-fund --legacy-peer-deps
          else
            npm install --no-audit --no-fund || npm install --no-audit --no-fund --legacy-peer-deps
          fi
      - name: Make the Android project
        env:
          CI: '1'
          EXPO_NO_TELEMETRY: '1'
        run: |
          cd "$APP_DIR"
          if [ -f android/gradlew ]; then
            echo "Using the android/ folder from the repo."
          elif grep -q '"expo"' package.json; then
            if [ -f app.json ]; then
              node -e 'const fs=require("fs");const j=JSON.parse(fs.readFileSync("app.json","utf8"));const e=j.expo||j;e.android=e.android||{};if(!e.android.package){let id=String(e.slug||e.name||process.env.GITHUB_REPOSITORY.split("/")[1]).toLowerCase().replace(/[^a-z0-9]/g,"");if(!/^[a-z]/.test(id))id="app"+id;e.android.package="com.pocketbunty."+id;fs.writeFileSync("app.json",JSON.stringify(j,null,2));console.log("android.package = "+e.android.package)}'
            fi
            npx expo prebuild --platform android --clean --no-install
          else
            echo "::error::Not an Expo or React Native app: no expo package and no android/ folder."
            exit 1
          fi
      - name: Build APK
        run: |
          cd "$APP_DIR/android"
          chmod +x gradlew
          ./gradlew --no-daemon --console=plain assembleRelease -PreactNativeArchitectures=arm64-v8a,armeabi-v7a
          apk="$(find app/build/outputs/apk/release -name '*.apk' 2>/dev/null | head -n 1)"
          [ -n "$apk" ] || apk="$(find . -path '*/build/outputs/apk/*' -name '*.apk' ! -name '*androidTest*' | head -n 1)"
          [ -n "$apk" ] || { echo "::error::Gradle finished but made no APK."; exit 1; }
          echo "APK_PATH=$PWD/${apk#./}" >> "$GITHUB_ENV"
EOF
}

wf_publish() {
cat <<'EOF'
      - name: Put the APK on the release
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          name="${GITHUB_REPOSITORY#*/}-debug.apk"
          cp "$APK_PATH" "$RUNNER_TEMP/$name"
          gh release view pocketbunty-apk --repo "$GITHUB_REPOSITORY" >/dev/null 2>&1 ||
            gh release create pocketbunty-apk --repo "$GITHUB_REPOSITORY" --prerelease --target "$GITHUB_SHA" \
              --title "Test APK (PocketBunty)" --notes "Test APK built by PocketBunty."
          gh release upload pocketbunty-apk "$RUNNER_TEMP/$name" --repo "$GITHUB_REPOSITORY" --clobber
          gh release edit pocketbunty-apk --repo "$GITHUB_REPOSITORY" \
            --notes "Test (debug-signed) APK built by PocketBunty from $GITHUB_SHA on $GITHUB_REF_NAME. For testing only."
          url="https://github.com/$GITHUB_REPOSITORY/releases/download/pocketbunty-apk/$name"
          echo "APK: $url"
          echo "### [Download $name]($url) (test APK)" >> "$GITHUB_STEP_SUMMARY"
EOF
}

workflow() {
  wf_head
  if [ "$KIND" = expo ]; then wf_expo; else wf_android; fi
  wf_publish
}

# ---------------------------------------------------------------- helpers

# The app folder (relative to here) of this kind of project, or nothing.
is_android_dir() {
  [ -n "$(ls "$1"/settings.gradle* "$1"/build.gradle* 2>/dev/null)" ] &&
    grep -rqsE 'android[.-]application|androidApplication' --include='*.gradle' --include='*.gradle.kts' --include='*.toml' "$1"
}
is_expo_dir() {
  [ -f "$1/package.json" ] && { grep -qsE '"(expo|react-native)"' "$1/package.json" || [ -f "$1/android/gradlew" ]; }
}
find_app_dir() {
  test_fn=is_android_dir
  [ "$KIND" = expo ] && test_fn=is_expo_dir
  if $test_fn .; then echo .; return 0; fi
  for f in $(find . -maxdepth 3 \( -name node_modules -o -name .git -o -name build \) -prune -o \
      \( -name 'settings.gradle*' -o -name package.json \) -print 2>/dev/null | sort); do
    d="$(dirname "$f")"
    if $test_fn "$d"; then echo "${d#./}"; return 0; fi
  done
  return 1
}

# Lines like "node_modules/" are added to .gitignore when missing, so they never go to GitHub.
ignore() {
  for p in "$@"; do
    probe="$APP_PREFIX${p%/}"; case "$p" in */) probe="$probe/pb-probe" ;; esac
    git check-ignore -q --no-index "$probe" 2>/dev/null || { printf '%s\n' "$p" >> .gitignore; note "Added $p to .gitignore"; }
  done
}

# ---------------------------------------------------------------- 1. check

step check
echo "Build script v1 · $KIND"
command -v git >/dev/null 2>&1 || fail NOGIT "git isn't installed. Finish the setup on Home, then build again."
command -v gh >/dev/null 2>&1 || fail NOGH "GitHub isn't set up yet. Open GitHub on Home and sign in."
gh auth token -h github.com >/dev/null 2>&1 || fail LOGGEDOUT "You're not signed in to GitHub. Open GitHub on Home and sign in."
out="$(gh api user --jq .login 2>&1)" || {
  case "$out" in
    *401*|*"Bad credentials"*) fail LOGGEDOUT "Your GitHub login expired. Open GitHub on Home and sign in again." ;;
    *) fail NETWORK "Can't reach GitHub. Check the internet, then build again. ($(last_line "$out"))" ;;
  esac
}
LOGIN="$out"
note "Signed in as $LOGIN"

if [ "$MODE" != resume ]; then
  APP_DIR="$(find_app_dir)" || {
    if [ "$KIND" = expo ]; then
      fail NOAPP "No app here yet (no package.json with expo). Describe your app in Chat first, then build."
    else
      fail NOAPP "No Android app here yet (no settings.gradle with an app module). Describe your app in Chat first, then build."
    fi
  }
  [ "$APP_DIR" = . ] || note "App folder: $APP_DIR"
fi

# ---------------------------------------------------------------- 2. save to GitHub

if [ "$MODE" != resume ]; then
  step save
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git init -q -b main 2>/dev/null || git init -q || fail GIT "Couldn't start git in this folder."
  fi
  # Work from the top of the git repo (the project can be a folder inside it).
  APP_PREFIX="$(git rev-parse --show-prefix 2>/dev/null)"
  cd "$(git rev-parse --show-toplevel)" || fail GIT "Couldn't open the git repo."
  [ "$APP_DIR" = . ] && APP_DIR="${APP_PREFIX%/}" || APP_DIR="$APP_PREFIX$APP_DIR"
  [ -n "$APP_DIR" ] || APP_DIR=.
  [ "$APP_DIR" = . ] && APP_PREFIX= || APP_PREFIX="$APP_DIR/"

  GD="$(git rev-parse --git-dir)"
  [ -e "$GD/MERGE_HEAD" ] || [ -d "$GD/rebase-merge" ] || [ -d "$GD/rebase-apply" ] &&
    fail GITBUSY "git is in the middle of a merge or rebase here. Tap Fix with AI to finish it, then build again."
  [ -n "$(git ls-files -u | head -n 1)" ] && fail GITBUSY "This folder has unresolved merge conflicts. Tap Fix with AI to resolve them, then build again."
  BRANCH="$(git symbolic-ref --short -q HEAD)" || fail DETACHED "This folder isn't on a branch (detached HEAD). Tap Fix with AI to switch to one, then build again."
  echo "@@BRANCH $BRANCH"

  [ -n "$(git config user.name)" ] || git config user.name "$LOGIN"
  [ -n "$(git config user.email)" ] || git config user.email "$LOGIN@users.noreply.github.com"

  if [ "$KIND" = expo ]; then
    ignore node_modules/ .expo/
    nm="${APP_PREFIX}node_modules"
    [ -n "$(git ls-files "$nm" | head -n 1)" ] && git rm -r -q --cached "$nm" && note "Took node_modules out of git"
  else
    ignore .gradle/ build/ local.properties
  fi

  mkdir -p .github/workflows || fail GIT "Couldn't write .github/workflows."
  workflow > ".github/workflows/$WF.tmp" && mv -f ".github/workflows/$WF.tmp" ".github/workflows/$WF" ||
    fail GIT "Couldn't write the build workflow."

  git add -A || fail GIT "git couldn't add the files: $(last_line "$(git add -A 2>&1)")"
  if ! git diff --cached --quiet; then
    out="$(git commit -q --no-verify -m "Save work before building the APK" 2>&1)" || fail GIT "git couldn't save (commit): $(last_line "$out")"
    note "Saved your changes (commit)"
  fi

  # The GitHub repo: origin, else a new private one named after the folder.
  if ! git remote get-url origin >/dev/null 2>&1; then
    base="$(basename "$PWD" | tr -c 'A-Za-z0-9._-' '-' | sed 's/^-*//; s/-*$//')"
    [ -n "$base" ] || base=app
    made=
    for n in "$base" "$base-2" "$base-3" "$base-4" "$base-5"; do
      out="$(gh repo create "$n" --private --source=. --remote=origin 2>&1)" && { made="$n"; break; }
      case "$out" in *"already exists"*) continue ;; esac
      fail REPO "Couldn't make a GitHub repo: $(last_line "$out")"
    done
    [ -n "$made" ] || fail REPO "Couldn't make a GitHub repo: the names $base…$base-5 are taken."
    note "Made the private repo $LOGIN/$made"
  fi
  url="$(git config --get remote.origin.url)"
  case "$url" in *github.com[:/]*) ;; *) fail NOTGITHUB "This project's remote isn't on GitHub ($url). Only GitHub repos can be built." ;; esac
  REPO="$(printf '%s' "$url" | sed -E 's#^.*github\.com[:/]+##; s#/+$##; s#\.git$##')"
  info="$(gh api "repos/$REPO" --jq '[.full_name, (if .permissions.push then "WRITE" else "READ" end), (.archived|tostring)] | @tsv' 2>&1)" || {
    case "$info" in
      *"Not Found"*|*404*) fail NOREPO "The GitHub repo of this project ($REPO) wasn't found. Was it deleted or renamed?" ;;
      *) fail NETWORK "Can't reach GitHub. Check the internet, then build again. ($(last_line "$info"))" ;;
    esac
  }
  REPO="$(printf '%s' "$info" | cut -f1)"
  perm="$(printf '%s' "$info" | cut -f2)"
  archived="$(printf '%s' "$info" | cut -f3)"
  echo "@@REPO $REPO"
  [ "$archived" = true ] && fail ARCHIVED "$REPO is archived on GitHub (read-only). Unarchive it in its Settings, then build again."
  case "$perm" in WRITE) ;; *) fail NOPUSH "You can't push to $REPO (it isn't your repo). Fork it on GitHub and set it as origin, or make a new project." ;; esac

  push() { git push --no-verify -u origin "HEAD:refs/heads/$BRANCH" 2>&1; }
  out="$(push)" || {
    case "$out" in
      *"rejected"*|*"fetch first"*|*"non-fast-forward"*)
        note "GitHub has newer changes: merging them"
        m="$(git pull --no-rebase --no-edit --no-verify origin "$BRANCH" 2>&1)" || {
          git merge --abort >/dev/null 2>&1
          echo "$m"
          fail BEHIND "GitHub has newer changes on $BRANCH that clash with the ones here. Tap Fix with AI to merge them, then build again."
        }
        out="$(push)" ;;
      *) false ;;
    esac
  } || {
    echo "$out"
    case "$out" in
      *workflow*) fail NOWORKFLOW "Your GitHub login can't add build workflows (\"workflow\" permission). Open GitHub on Home and tap Allow, then build again." ;;
      *GH001*|*"exceeds GitHub's file size limit"*|*"Large files detected"*)
        fail BIGFILE "A file over 100 MB can't go to GitHub. Tap Fix with AI to take it out of git, then build again." ;;
      *403*|*"Permission"*|*"denied"*) fail NOPUSH "GitHub refused the push to $REPO: $(last_line "$out")" ;;
      *"Could not resolve host"*|*"unable to access"*) fail NETWORK "Can't reach GitHub. Check the internet, then build again." ;;
      *) fail PUSH "Couldn't push to GitHub: $(last_line "$out")" ;;
    esac
  }
  note "Pushed to $BRANCH on $REPO"

  # ------------------------------------------------------------ 3. start the build

  step start
  enabled="$(gh api "repos/$REPO/actions/permissions" --jq .enabled 2>/dev/null)"
  [ "$enabled" = false ] && fail ACTIONS_OFF "GitHub Actions is turned off for $REPO. Turn it on in the repo's Settings → Actions, then build again."
  started=
  for i in 1 2 3 4 5 6 7 8 9 10; do
    out="$(gh workflow run "$WF" --repo "$REPO" --ref "$BRANCH" -f "request=$REQ" -f "dir=$APP_DIR" 2>&1)" && { started=1; break; }
    case "$out" in
      *"Actions has been disabled"*|*"Actions is disabled"*|*"actions are disabled"*)
        fail ACTIONS_OFF "GitHub Actions is turned off for $REPO. Turn it on in the repo's Settings → Actions, then build again." ;;
      *"disabled"*)
        gh workflow enable "$WF" --repo "$REPO" >/dev/null 2>&1 || fail ACTIONS_OFF "The build workflow is turned off on GitHub and couldn't be turned on: $(last_line "$out")" ;;
    esac
    # Just pushed: GitHub needs a few seconds to see a new workflow file.
    sleep 5
  done
  [ -n "$started" ] || fail START "GitHub didn't start the build: $(last_line "$out")"
  for i in $(seq 1 40); do
    RUN_ID="$(gh run list --repo "$REPO" --workflow "$WF" --event workflow_dispatch -L 20 --json databaseId,displayTitle \
      --jq "[.[] | select(.displayTitle | contains(\"$REQ\"))][0].databaseId // empty" 2>/dev/null)"
    [ -n "$RUN_ID" ] && break
    sleep 3
  done
  [ -n "$RUN_ID" ] || fail START "The build started on GitHub but couldn't be found. See the repo's Actions tab."
  echo "@@RUN $RUN_ID https://github.com/$REPO/actions/runs/$RUN_ID"
fi

[ -n "$RUN_ID" ] && [ -n "$REPO" ] || fail START "Nothing to watch: build again."

# ---------------------------------------------------------------- 4. build (watch the run)

step build
began=$(date +%s); misses=0; shown=; status=; conclusion=
while :; do
  row="$(gh run view "$RUN_ID" --repo "$REPO" --json status,conclusion,jobs \
    --jq '[.status, (.conclusion // ""), ([.jobs[].steps[]? | select(.status == "in_progress") | .name] | first // "")] | @tsv' 2>&1)"
  if [ $? -ne 0 ]; then
    misses=$((misses + 1))
    case "$row" in *"not found"*|*404*) fail NORUN "The build on GitHub was deleted." ;; esac
    [ $misses -ge 30 ] && fail NETWORK "Lost contact with GitHub. The build may still be running: tap Check again."
    sleep 10; continue
  fi
  misses=0
  status="$(printf '%s' "$row" | cut -f1)"
  conclusion="$(printf '%s' "$row" | cut -f2)"
  now_step="$(printf '%s' "$row" | cut -f3)"
  [ "$status" = completed ] && break
  label="$now_step"; [ -n "$label" ] || label="Waiting for a GitHub machine ($status)"
  [ "$label" != "$shown" ] && { note "$label"; shown="$label"; }
  [ $(( $(date +%s) - began )) -gt 4500 ] && fail TIMEOUT "The build is taking over 75 minutes. Check it on GitHub, or tap Check again."
  sleep 10
done

case "$conclusion" in
  success) ;;
  cancelled) fail CANCELLED "The build was cancelled on GitHub." ;;
  *)
    steps="$(gh run view "$RUN_ID" --repo "$REPO" --json jobs --jq '[.jobs[].steps[]? | select(.conclusion == "failure") | .name] | join(", ")' 2>/dev/null)"
    log=
    for i in 1 2 3 4; do
      log="$(gh run view "$RUN_ID" --repo "$REPO" --log-failed 2>/dev/null)"
      [ -n "$log" ] && break
      sleep 5
    done
    echo "@@ERRLOG_BEGIN"
    # "job<TAB>step<TAB>2024-01-01T00:00:00.0000000Z text" → "text"
    printf '%s\n' "$log" | cut -f3- | sed -E 's/^[0-9]{4}-[0-9-]+T[0-9:.]+Z ?//; s/\x1b\[[0-9;]*m//g' | grep -vE '^##\[(group|endgroup)\]' | tail -n 250
    echo "@@ERRLOG_END"
    fail BUILD "The build failed${steps:+ at \"$steps\"} ($conclusion)."
    ;;
esac

# ---------------------------------------------------------------- 5. link

step link
created="$(gh run view "$RUN_ID" --repo "$REPO" --json createdAt --jq .createdAt 2>/dev/null)"
name="${REPO#*/}-debug.apk"
for i in 1 2 3 4 5 6; do
  row="$(gh api "repos/$REPO/releases/tags/$TAG" --jq ".assets[] | select(.name == \"$name\") | [.updated_at, (.size|tostring), .browser_download_url] | @tsv" 2>/dev/null)"
  at="$(printf '%s' "$row" | cut -f1)"
  if [ -n "$row" ] && { [ -z "$created" ] || [ ! "$at" \< "$created" ]; }; then
    echo "@@APK $(printf '%s' "$row" | cut -f3) $(printf '%s' "$row" | cut -f2)"
    exit 0
  fi
  sleep 5
done
fail NOAPK "The build passed but its APK isn't on the release \"$TAG\". Build again."
# end of pocketbunty-build-apk
