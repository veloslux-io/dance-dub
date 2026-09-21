# Dance Dub — download Wix videos, transcribe, translate, and add subtitles

Dance Dub is a free, local tool for Windows that takes videos from a Wix website and produces a copy with burned-in translated subtitles. Every step can run on its own or as one pipeline.

## What it does

1. **Download** a video from a Wix site page URL (or a direct video URL, or YouTube/Vimeo links)
2. **Extract audio** from the video
3. **Transcribe** the speech (Whisper, runs locally on your CPU — no accounts, no API keys)
4. **Translate** the transcript (Google Translate, keyless)
5. **Burn captions** into a copy of the video

## Install (one time)

1. Install Python from https://www.python.org/downloads/ — **check "Add python.exe to PATH"** on the first screen
2. Double-click `install.bat`
3. Wait for it to finish (it downloads ~500 MB of AI model on first run of transcribe, that's normal)

That's it. To use it, double-click **`run_gui.bat`**.

## GUI usage

1. Paste the video page URL (e.g. `https://your-site.wixsite.com/mysite/some-page`)
2. Pick an output folder
3. Choose "Translate to" language (default: English)
4. Click **Run all steps**
5. Find the result in `<output folder>/<video name>/final_<video name>.mp4`

Buttons for each single step are there too, if you only want part of the process.

## CLI usage

```
run_cli.bat all --url https://your-site.wixsite.com/mysite/some-page -o output --target-lang en
run_cli.bat download --url <url> -o output
run_cli.bat transcribe --input output/<video> --model small
run_cli.bat translate --input output/<video> --target-lang en
run_cli.bat burn --input output/<video>
```

Available subcommands: `download`, `extract-audio`, `transcribe`, `read-transcript`, `translate`, `burn`, `all`.

Run `run_cli.bat --help` for every option.

## Files produced per video

| File | What it is |
|---|---|
| `video.mp4` | downloaded original |
| `audio.wav` | extracted 16 kHz mono audio |
| `transcript.srt` / `transcript.txt` | original-language transcript |
| `transcript_translated.srt` | translated subtitles |
| `final_<video name>.mp4` | video with subtitles burned in |

## Troubleshooting

- **"ffmpeg not found"** — run `install.bat` again; it installs a bundled copy automatically.
- **Download fails on a Wix page** — try the direct video URL: open the page in Chrome → F12 → Network tab → play the video → find the `video.wixstatic.com/...file.mp4` request → copy its URL. Paste that as the URL.
- **First transcription is slow / downloads a model** — normal; it caches locally (`~/.cache`).
- **Translation fails repeatedly** — Google's free endpoint occasionally rate-limits; wait a minute and re-run `translate` (earlier steps are kept).
- **Transcription quality** — try `--model medium` (slower, better) or `--model tiny` (faster draft).

## Costs & privacy

Everything runs on your own machine. No accounts, no API keys, no uploads. Free forever.
