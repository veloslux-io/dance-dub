# Dance Dub — download Wix videos, transcribe, translate, add subtitles

Dance Dub takes a video from a Wix site (or any video file on your computer), transcribes the speech, translates it, and produces a copy with burned-in subtitles. Everything runs locally. No accounts or uploads are needed; a Wix API key is optional and only used to list your own site's videos.

## Windows app (recommended)

1. Download `DanceDub-windows.zip` from the repo's [Releases](../../releases) page
2. Unzip it anywhere (keep all files together)
3. Double-click **`DanceDub.exe`** — your browser opens the app
   - First launch: Windows SmartScreen may warn ("Windows protected your PC"). Click **More info → Run anyway** — the app is unsigned but only talks to `127.0.0.1` on your own machine
4. Paste a Wix video page URL, **or** use "From my Wix site" to pick videos from your site (see below), **or** switch to "From a file on this PC" and drop a video in
5. Pick the language, accuracy and video quality, press **Start**
   - **Video quality → Smaller file** (default) re-encodes at a standard setting, which usually gives a smaller file than the download.
   - **Same as original** matches the source video's bitrate, so the subtitled copy keeps the original's picture quality and is about the same size.

First transcription downloads the AI model once (~460 MB), then it's cached in your user folder. Output lands in `<user folder>\DanceDub\Videos\<video name>\`.

## From my Wix site (API key)

Lists every video on your own Wix site so you can tick the ones you want and process them in one go, keeping the site's structure as folders.

1. In Wix, open [API Keys](https://manage.wix.com/account/api-keys) (site owner only) and create a key with the **Media Manager** and **Online Programs** permissions.
2. In Dance Dub choose **From my Wix site**, paste the key, and enter the site ID (the long code after `/dashboard/` in your site's dashboard address; pasting the whole address works).
3. Pick a source and press **Load videos**:
   - **Media library**: every video in the Media Manager, grouped by its Media Manager folders.
   - **Online Programs**: every video step, grouped as program, then numbered sections and steps in the program's order.
4. Each video has two boxes and a badge:
   - **⬇ Download** fetches just the video file. It shows ticked and greyed when the video is already in the **Save to** folder.
   - **✨ Process** downloads the video if needed, then runs audio, transcription, translation and subtitles.
   - The badge (**Not downloaded**, **✓ Downloaded**, **✓ Subtitled**) is read from the Save to folder, so deleting a video file there turns it back to Not downloaded. The list re-checks when you return to the window, change the folder, finish a job, or press **↻ Check folder**.

   A folder's boxes tick everything inside it. Press **Start**; one failure doesn't stop the rest.

Output mirrors the listing, e.g. `Videos\Bachata 101\01 Week 1\01 Welcome\final_01 Welcome.mp4`. Program steps that use a video from the separate Wix Video app get no file link from Wix's API. Dance Dub finds those videos in the Media Manager instead, where Wix tags each Wix Video upload with its video ID, and the list shows which file it used. If a step still has no file, press **Paste link** next to it and paste the video's address: open the step on your live site, press F12, go to the Network tab, play the video, filter for `wixstatic` or `m3u8`, and copy that request's URL. When steps stay unresolved, what Wix returned for them is saved to `DanceDub\wix-video-steps.json` (it contains no key). "Remember on this computer" keeps the key in this browser's storage on your PC.

## Edit subtitles and remake the video

Choose **✏️ Edit subtitles** to fix what Whisper heard or how it was translated.

1. Pick a video from the list (every video in the **Save to** folder that has subtitles).
2. Choose **Original language** or **Translated**. The video plays above the lines and shows your edits as you type. Click a line to jump to it; ▶ plays from it, ＋ adds a line after it, ✕ removes it. Times are `hours:minutes:seconds,milliseconds`.
3. **Save** writes the `.srt` and `.vtt`. The first save keeps the untouched file as `<name>.srt.bak`.
4. Choose which subtitles go in the new video and the **Video quality**, then press **Save & remake video**. It replaces `final_<video name>.mp4`.

After fixing the original-language lines, **Translate again** re-translates them into the **Translate to** language. This replaces the translated subtitles, including edits made to them; a backup is kept the first time.

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
run_cli.bat burn --input output/<video> --quality original --subs original --force
```

`--quality original` (on `burn` and `all`) keeps the source's bitrate. `--subs original|translated` picks the subtitles to burn, and `--force` makes the video again.

## Files produced per video

| File | What it is |
|---|---|
| `video.mp4` | the original video |
| `audio.wav` | extracted audio |
| `transcript.srt` / `transcript.vtt` / `transcript.txt` | original-language transcript (`.vtt` is the WebVTT copy web players take as captions) |
| `transcript_translated.srt` / `transcript_translated.vtt` | translated subtitles |
| `final_<video name>.mp4` | video with subtitles burned in |
| `final_settings.json` | the quality and subtitles the final video was made with; changing either remakes it on the next run |
| `*.srt.bak` | the subtitles as they were before your first edit |

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
