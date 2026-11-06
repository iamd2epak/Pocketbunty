# PocketBunty setup

Setup files for the PocketBunty Android app. Run this once in Termux:

```sh
bash <(curl -fsSL https://raw.githubusercontent.com/iamd2epak/pocketbunty/main/v2/setup.sh)
```

It sets up only the bridge (Termux packages, Python, `v2/server.py`) so the Termux part is short;
then the app installs git, the GitHub CLI and the AI coder **OpenCode** (pinned 1.19.5, runs directly in Termux, no proot)
in the background with `v2/env.sh`. The code to link the app is always shown at the end. Safe to run again.
Everything lives in the hidden folder `~/.pocketbunty`. Later, to reconnect the app, just type
`pocketbunty` in Termux.

- `v2/setup.sh`: the Termux setup (bridge only; `--full` also runs env.sh), then links the app.
- `v2/env.sh`: git, GitHub CLI, OpenCode and `agent.py` (the app runs it in the background after linking, and again to repair).
- `v2/agent.py`: the app's link to OpenCode (one chat message = one private `opencode serve`, live events as JSON lines; providers, API keys, logins).
- `scripts/` and `server/`: the old jcode setup, kept only for app versions before 2.0. Remove later.

## Live app config (changes reach the app without an update)

- `config/recommended-models.json`: models shown first ("Recommended") in the model picker.
- `build/build-apk.sh`: the **Build APK** script (checks, push, GitHub Actions workflow for Android, Expo and games, link).
  When you change it, raise `version=` on its first line. Keep `protocol=1` unless the `@@` lines
  it prints change. The app ignores a copy that is cut off, has another protocol, or is older than
  the one bundled in the app.
