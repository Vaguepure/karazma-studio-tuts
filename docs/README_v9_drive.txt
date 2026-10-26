KARAZMA STUDIO v9 - everything v9 needs (separate from v8)
==========================================================
INSTALL (once):
 1. Download the 5 zips in this folder: karazma-studio-v9_1_main / _2_speech_engine / _3_speech_av / _4_ffmpeg / _5_ffprobe.zip
 2. Extract ALL FIVE into the same place (e.g. D:\, 50 GB free) -> one folder KarazmaStudio9
 3. Optional check: KarazmaStudio9\app\selftest.bat  -> "SELF-TEST PASSED"
 4. Double-click KarazmaStudio9\start.bat (Windows warning: More info -> Run anyway)

WHAT IS HERE
 karazma-studio-v9_*.zip  the program (python + speech engine + ffmpeg/ffprobe), each < 100 MB
 v9_app\                  code updates; the program checks app_manifest.json at every start
 .assets\                 fonts, bumper, logo motion, music, SFX, renderer (copy of v8's .assets); synced at every start
 models\                  faster-whisper medium/small in 95 MB parts + manifest (model download fallback)
 SOP 2.5 doc              how to work with v9

MODEL DOWNLOAD ORDER (automatic, SHA256 checked):
 Hugging Face -> hf-mirror.com -> ModelScope -> this Drive folder (models\) -> manual:
 copy the whole folder faster-whisper-medium into KarazmaStudio9\app\models\
 (optional: "model_mirror" in app\config.json = your own URL pattern with {m} and {f})
