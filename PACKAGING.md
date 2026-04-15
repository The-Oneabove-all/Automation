# Packaging & Testing — Auto Send (macOS)

This document explains how to test the app locally and package it for macOS using **PyInstaller** or **py2app**. ⚠️ Read the macOS Notes below about Messages automation permissions.

## Quick tests (no real SMS)
- GUI: open the app
  ```bash
  python3 Automation/auto_send.py
  ```
- CLI dry-run (won't send):
  ```bash
  python3 Automation/auto_send.py --to "+15551234567" --message "Test" --dry-run
  ```
- CLI actual send (will try macOS Messages or Twilio):
  ```bash
  python3 Automation/auto_send.py --to "+15551234567" --message "Hello from desktop app"
  ```

## Build with PyInstaller (recommended)
1. Create a virtualenv (recommended):
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install pyinstaller twilio
   ```
2. Build (windowed, no console):
   ```bash
   pyinstaller --name "AutoSend" --onefile --windowed Automation/auto_send.py
   ```
3. Result:
   - A single-file executable will be in `dist/AutoSend` (macOS executable). You can distribute that executable or wrap it in an installer.

Notes:
- If you prefer a `.app` bundle you may need to use a spec file or `py2app` (see below) for a native .app bundle.
- Test the built executable on a machine with the same OS version due to macOS code-signing and entitlement differences.

## Creating a DMG installer (drag-to-install)
I provide a helper script `create_dmg.sh` which generates a drag-to-install DMG containing `AutoSend.app` and an `Applications` link. Usage:

- Simple (uses default layout, no background):

```bash
./create_dmg.sh dist/AutoSend.app dist/AutoSend.dmg
```

- With a custom background image (PNG):

```bash
./create_dmg.sh dist/AutoSend.app dist/AutoSend.dmg assets/dmg_bg.png
```

What the script does:
- Copies `AutoSend.app` into a temp staging folder, adds a symlink to `/Applications`.
- Optionally places your background image into `.background/background.png` inside the DMG.
- Uses `hdiutil` and an AppleScript tweak so the Finder window shows the app and Applications link positioned for drag-and-drop installation.

Notes & caveats:
- This DMG is not code-signed or notarized. For distribution outside your machine, sign + notarize before distributing (see packaging notes earlier).
- If you want a custom background, prefer a PNG sized around 600×400 (or larger) to look crisp; the script will use the image directly.

## Build with py2app (native .app)
1. Install:
   ```bash
   pip install py2app
   ```
2. Create `setup.py` in project root with a minimal config (see py2app docs) and run:
   ```bash
   python3 setup.py py2app
   ```
3. `dist/` will contain the `.app` bundle.

## macOS Notes & Permissions ⚠️
- If using the **Messages** AppleScript path, macOS will ask the user to grant the Terminal (or the packaged app) permission to control the Messages app (System Settings → Privacy & Security → Automation).
- For distribution, you may need to **codesign** and notarize the app to avoid warnings on other machines.

## Twilio fallback
- Optionally set env vars:
  - `TWILIO_ACCOUNT_SID`
  - `TWILIO_AUTH_TOKEN`
  - `TWILIO_FROM`
- Install the Twilio library: `pip install twilio`

## Files included
- `build_mac_app.sh` — helper script to create a virtualenv and build with PyInstaller.
- `requirements.txt` — optional deps (e.g., `twilio`).
- `tests/test_dryrun.sh` — test the CLI with `--dry-run`.

---
💡 Tip: Use the `--dry-run` flag to test CLI sends safely before enabling real sends.
