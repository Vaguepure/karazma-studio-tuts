# Karazma Studio v9.5

Windows operator app for Karazma long courses (SOP 2.5). This repository is the v9.5 operator code and docs. The installed app does not update from git.

## What it does

`v9_app/studio9.py` (`VERSION = '9.5'`) is the console the operator runs from `KarazmaStudio9\start.bat`:

1. Download a course (host links or a Google Drive folder).
2. Transcribe and find claps, then pack one zip for Mosi.
3. Apply Mosi's `edit.json` logs and render into `Final`.
4. Upload the Final videos to the host.
5. Check for update.

The UI is English only.

Option 3 reads `<EP>.edit.json` (or a `<Course>_logs.zip`). An episode marked with a skip stub is not cut or rendered:

```json
{"episode": "C5030", "skip": true, "reason": "..."}
```

`mosi_tools/v9_log.py` builds those logs on Mosi's side, including `--skip`.

Cut rules (v9.4, in both `studio9.py` and `v9_log.py`): silence longer than 1 second between words is cut; a kept piece shorter than 1 second is dropped. Cuts stay on word boundaries.

## How Arsha runs it

Arsha uses the installed `KarazmaStudio9` folder on Windows, not a checkout of this repo. One course per run; episodes run one after another. After option 2, the `<Course>_for_Mosi.zip` goes to Mosi. When the logs come back, they go in the course folder and option 3 applies them. Skip stubs are reported as skipped and left alone. Option 4 uploads `Final`.

Install and the day-to-day steps are in `docs/SOP_2.5_fa.md` and `docs/README_v9_drive.txt`. Patch notes are `docs/PATCH_v9.3.txt`, `docs/PATCH_v9.4.txt`, and `docs/PATCH_v9.5.txt`.

## Drive updates vs this repo

The running app syncs from the Google Drive folder **karazma-studio-full-operator** (`v9_app` and `.assets`), not from GitHub:

https://drive.google.com/drive/folders/1fvAAdI0kUTRnISd5WLC5h2MNnLvgB4Eb

On startup, and from menu **[5] Check for update**, it reads `v9_app/app_manifest.json` on that Drive folder and downloads changed files after a SHA256 check. From 9.5, a `v9_app` code update restarts the same program in the same console. An `.assets`-only update does not restart. If Drive is unreachable, the copy already on the PC is used.

This repo is the reviewed snapshot of that operator code (manifest version 9.5) plus the SOP and patch notes. Shipping a new build to operators still means updating the Drive `v9_app` folder the app syncs from.
