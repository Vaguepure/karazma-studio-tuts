# Karazma Studio v9.6

Windows operator app for Karazma long courses (SOP 2.5). This repository is the v9.6 operator code and docs.

## What it does

`v9_app/studio9.py` (`VERSION = '9.6'`) is the console the operator runs from `KarazmaStudio9\start.bat`:

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

Install and the day-to-day steps are in `docs/SOP_2.5_fa.md` and `docs/README_v9_drive.txt`. Patch notes are `docs/PATCH_v9.3.txt`, `docs/PATCH_v9.4.txt`, `docs/PATCH_v9.5.txt`, and `docs/PATCH_v9.6.txt`.

## App updates

App updates now come from this GitHub repo's `v9_app/` on `main`, via raw.githubusercontent.com. No GitHub login or token is needed on the Windows PC:

https://github.com/Vaguepure/karazma-studio-tuts/tree/main/v9_app

On startup, and from menu **[5] Check for update**, the app reads `v9_app/app_manifest.json` from that path and downloads changed files after a SHA256 check. A code update restarts the same program in the same console (same as 9.5). If GitHub is unreachable, the copy already on the PC is used.

v9.6 is the last dual publish to Google Drive and GitHub. Later app versions go to GitHub only. Drive is only for that last dual publish and for assets: `.assets`, ffmpeg zips, and the Whisper model fallback still use **karazma-studio-full-operator**. An assets-only update does not restart.

https://drive.google.com/drive/folders/1fvAAdI0kUTRnISd5WLC5h2MNnLvgB4Eb
