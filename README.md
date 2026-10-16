# PocketBunty setup

Setup files for the PocketBunty Android app. Run this once in Termux:

```sh
bash <(curl -fsSL https://raw.githubusercontent.com/iamd2epak/pocketbunty/main/scripts/setup.sh)
```

It installs Python, the bridge server (`server/server.py`), jcode, git and the GitHub CLI,
starts the bridge and opens the app linked. Safe to run again: it repairs what's missing.
Everything lives in the hidden folder `~/.pocketbunty` (an old `~/bridge` is moved there).
Later, to reconnect the app, just type `pocketbunty` in Termux.
