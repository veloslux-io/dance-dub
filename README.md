# Dance Dub — download Wix videos, transcribe, translate, add subtitles

Dance Dub takes a video from a Wix site (or any video file on your computer), transcribes the speech, translates it, and produces a copy with burned-in subtitles. Everything runs locally — no accounts, no API keys, no uploads.

## Windows app (recommended)

1. Download `DanceDub-windows.zip` from the repo's [Releases](../../releases) page
2. Unzip it anywhere (keep all files together)
3. Double-click **`DanceDub.exe`** — your browser opens the app
   - First launch: Windows SmartScreen may warn ("Windows protected your PC"). Click **More info → Run anyway** — the app is unsigned but only talks to `127.0.0.1` on your own machine
4. Paste a Wix video page URL **or** switch to "From a file on this PC" and drop a video in
5. Pick the language and accuracy, press **Start**

First transcription downloads the AI model once (~460 MB), then it's cached in your user folder. Output lands in `<user folder>\DanceDub\Videos\<video name>\`.

## macOS / Linux (from source)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=src .venv/bin/python -m dance_dub        # app (opens browser)
PYTHONPATH=src .venv/bin/python -m dance_dub.cli --help   # CLI
```

## What it does

1. **Download** — Wix pages, direct `wixstatic.com` URLs, YouTube/Vimeo embeds
2. **Extract audio** — 16 kHz mono WAV
3. **Transcribe** — Whisper (faster-whisper, CPU) with language auto-detect
4. **Translate** — Google (keyless) with MyMemory fallback
5. **Burn** — styled subtitles rendered into a copy of the video

Each stage is also a CLI subcommand; finished stages are skipped on re-run, so a crashed job resumes where it left off.

## CLI

```
run_cli.bat all --url https://your-site.wixsite.com/mysite/page -o output --target-lang en
run_cli.bat download|extract-audio|transcribe|translate|burn --input output/<video>
```

## Files produced per video

| File | What it is |
|---|---|
| `video.mp4` | the original video |
| `audio.wav` | extracted audio |
| `transcript.srt` / `transcript.txt` | original-language transcript |
| `transcript_translated.srt` | translated subtitles |
| `final_<video name>.mp4` | video with subtitles burned in |

## Troubleshooting

- **SmartScreen warning** — More info → Run anyway (one time). Signing certs cost money; this is a personal tool.
- **Download fails on a Wix page** — try the direct video URL: open the page in Chrome → F12 → Network tab → play the video → find the `video.wixstatic.com/...file.mp4` request → copy that URL.
- **First transcription is slow** — normal; it downloads the model once. `tiny`/`base` models are faster drafts.
- **Music-only video** — there's no speech to caption; the app will tell you.
- **Translation fails repeatedly** — the free Google endpoint rate-limits sometimes; wait a minute and press Start again (finished stages are kept).

## Building the Windows app

Tag a release (`v*`) or run the workflow manually — `.github/workflows/build-windows.yml` builds with PyInstaller on a Windows runner, smoke-tests that the exe actually serves the UI, and attaches `DanceDub-windows.zip` to the release.

## Costs & privacy

Free. Everything runs on your own machine; nothing leaves it except the video download and translation lookups.
