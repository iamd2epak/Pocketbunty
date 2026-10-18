# PocketBunty setup

Setup files for the PocketBunty Android app. Run this once in Termux:

```sh
bash <(curl -fsSL https://raw.githubusercontent.com/iamd2epak/pocketbunty/main/scripts/setup.sh)
```

It installs Python, the bridge server (`server/server.py`), jcode, git and the GitHub CLI,
starts the bridge and opens the app linked. Safe to run again: it repairs what's missing.
Everything lives in the hidden folder `~/.pocketbunty` (an old `~/bridge` is moved there).
Later, to reconnect the app, just type `pocketbunty` in Termux.

## Live app config (changes reach the app without an update)

- `config/recommended-models.json`: models shown first ("Recommended") in the model picker.
- `build/build-apk.sh`: the **Build APK** script (checks, push, GitHub Actions workflow for Android, Expo and games, link).
  When you change it, raise `version=` on its first line. Keep `protocol=1` unless the `@@` lines
  it prints change. The app ignores a copy that is cut off, has another protocol, or is older than
  the one bundled in the app.
