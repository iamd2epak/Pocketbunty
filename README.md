# PocketBunty setup

Setup files for the PocketBunty Android app. Run this once in Termux:

```sh
bash <(curl -fsSL https://raw.githubusercontent.com/iamd2epak/pocketbunty/main/v2/setup.sh)
```

It installs everything in one go: Termux packages, Python, the bridge server (`v2/server.py`), git,
the GitHub CLI and the AI coder **OpenCode** (pinned 1.19.5, runs directly in Termux, no proot),
then starts the bridge and opens the app linked. Safe to run again: it repairs what's missing.
Everything lives in the hidden folder `~/.pocketbunty`. Later, to reconnect the app, just type
`pocketbunty` in Termux.

- `v2/setup.sh`: the one-command setup (bridge + `v2/env.sh`, then links the app).
- `v2/env.sh`: git, GitHub CLI, OpenCode and `agent.py` (the app also runs it in the background to repair).
- `v2/agent.py`: the app's link to OpenCode (one chat message = one private `opencode serve`, live events as JSON lines; providers, API keys, logins).
- `scripts/` and `server/`: the old jcode setup, kept only for app versions before 2.0. Remove later.

## Live app config (changes reach the app without an update)

- `config/recommended-models.json`: models shown first ("Recommended") in the model picker.
- `build/build-apk.sh`: the **Build APK** script (checks, push, GitHub Actions workflow for Android, Expo and games, link).
  When you change it, raise `version=` on its first line. Keep `protocol=1` unless the `@@` lines
  it prints change. The app ignores a copy that is cut off, has another protocol, or is older than
  the one bundled in the app.
