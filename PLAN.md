# Dance Dub — Wix Video → Subtitled Video Pipeline Implementation Plan

> **For Hermes:** Implement this plan task-by-task; commit after each task.

**Goal:** A free, Windows-friendly CLI + one-click GUI that downloads videos from a Wix site, transcribes (Whisper), translates, and burns-in subtitles — each step runnable individually or as one pipeline.

**Architecture:** A thin Python CLI orchestrates five independent file-to-file stages (download → extract audio → transcribe → translate → burn-in), passing files through a per-video working folder. Every stage reads the previous stage's output file if present, so steps can be run individually and a crashed run resumes by re-running `all`. A tiny Tkinter GUI (stdlib, zero extra deps) wraps the same functions for non-technical users. GPU is optional (faster-whisper `device="auto"`); everything runs CPU-only with int8 quantization.

**Tech Stack:** Python 3.9+, yt-dlp, ffmpeg (via imageio-ffmpeg wheel), faster-whisper (CTranslate2), deep-translator (Google web endpoint, keyless), Tkinter (stdlib).

---

## Why these choices (vs. alternatives)

| Decision | Why | Alternative rejected |
|---|---|---|
| yt-dlp generic extractor for Wix | Wix media lives on `video.wixstatic.com` / `static.wixstatic.com` as plain MP4s; yt-dlp's generic extractor grabs it; it also handles YouTube/Vimeo embeds on Wix pages | Custom scraping of Wix's JS player — brittle, Wix changes markup often |
| faster-whisper, `small` model, int8, CPU | 4x faster than openai-whisper, no PyTorch install, runs comfortably on any Windows laptop; `small` ≈ 461 MB download, auto-cached | openai-whisper (needs PyTorch, much slower on CPU), cloud STT APIs (cost, keys) |
| deep-translator (GoogleTranslator) | Keyless, free, handles long text by chunking, translates SRT while preserving timestamps | Argos/NLLB local (heavier setup), DeepL API (key + quota) |
| ffmpeg burn-in (`subtitles=` filter with `force_style`) | One command, no audio re-encode, precise styling/positioning | `.srt` as soft subtitle track (invisible in some players; brief asks for burned-in copy) |
| Tkinter GUI on top of pipeline funcs | Stdlib = zero extra install; user just double-clicks `run_gui.bat` | Gradio/Streamlit (extra deps, browser tab, slower to build) |
| imageio-ffmpeg wheel | Pips a static ffmpeg binary — no manual ffmpeg PATH setup | Winget/manual FFmpeg installs (extra steps for a non-technical user) |

**Translation default:** auto-detect source language → English. `--source-lang`/`--target-lang` flags accept ISO codes (e.g. `es`, `fr`). Whisper auto-detects the spoken language and the tool surfaces it.

## Repo layout

```
dance-dub/
├── PLAN.md                  # this plan
├── README.md                # install + usage (friend-friendly)
├── requirements.txt         # yt-dlp, faster-whisper, deep-translator, imageio-ffmpeg
├── .gitignore               # venv, output/, __pycache__, *.mp4
├── src/dance_dub/
│   ├── __init__.py
│   ├── cli.py               # argparse entry: subcommands per stage + `all`
│   ├── pipeline.py          # stage functions + orchestrator (all logic lives here)
│   └── gui.py               # Tkinter wrapper, calls pipeline functions
├── install.bat              # one-time setup (creates venv, installs reqs)
├── run_gui.bat              # double-click launcher (venv bootstrap + gui)
├── run_cli.bat              # CLI launcher with venv bootstrap
└── tests/test_pipeline.py   # offline unit tests (SRT formatting, translation chunking, burn-in cmd build)
```

## Stage specs

Working folder per video: `output/<video-name>/` holds `video.mp4`, `audio.wav`, `transcript.srt` (original language), `transcript.txt` (plain text), `transcript_translated.srt` (target lang), `final_<video-name>.mp4` (subtitled copy).

| # | Stage | Tool | Input → Output | Notes |
|---|---|---|---|---|
| 1 | download | yt-dlp | URL → `video.mp4` | `-f bv*+ba/b`; retries; handles wixstatic direct links & YouTube/Vimeo embeds; `--no-playlist` |
| 2 | extract-audio | ffmpeg | `video.mp4` → `audio.wav` (16 kHz mono PCM) | Exactly what Whisper wants |
| 3 | transcribe | faster-whisper `small` int8 | `audio.wav` → `transcript.srt` + `transcript.txt` | `vad_filter=True`, segment-level timestamps, language auto-detected + surfaced |
| 4 | translate | deep-translator GoogleTranslator | `transcript.srt` → `transcript_translated.srt` | Translate text lines only (never timestamps), batch per request, preserve numbering |
| 5 | burn | ffmpeg `subtitles=` filter | `video.mp4` + translated SRT → `final_*.mp4` | `force_style`: Arial 24, white, outline, bottom-center; configurable |

`dance-dub all` runs 1→5 skipping any stage whose output already exists (natural resume). Each stage is also a standalone subcommand.

## Task list

### Task 1: Repo scaffold
All files above with stubs; `.gitignore`; initial commit.

### Task 2: Stage 1 — download (`download_video`)
Wrap `yt_dlp.YoutubeDL`: `format: bv*+ba/b`, `outtmpl: <dir>/video.%(ext)s`, `noplaylist`, `retries: 5`. Return final path. Test with mocked yt-dlp. Commit `feat: download stage`.

### Task 3: Stage 2 — audio extraction (`extract_audio`)
ffmpeg `-vn -acodec pcm_s16le -ar 16000 -ac 1` via imageio-ffmpeg binary. Test: mocked subprocess, assert args. Commit `feat: audio extraction`.

### Task 4: Stage 3 — transcribe (`transcribe_audio`)
faster-whisper `Whisper(model_size, device="auto", compute_type="int8")`, `vad_filter=True`. Format SRT timestamps `HH:MM:SS,mmm`; write plain `.txt`; surface detected language. `--model tiny|base|small|medium` (default small). Test: fake segments → SRT formatting (mocked model). Commit `feat: transcribe stage`.

### Task 5: Stage 4 — translate (`translate_srt`)
Parse SRT blocks; translate text only; batch ≤ ~4500 chars/request; retry ×3. Test: mock translator, assert structure/timestamps preserved. Commit `feat: translate stage`.

### Task 6: Stage 5 — burn-in (`burn_subtitles`)
`-vf subtitles=...:force_style=...` with Windows path escaping (`C\:`). List-form args. Test: command construction. Commit `feat: burn-in stage`.

### Task 7: CLI wiring
Subcommands `download | extract-audio | transcribe | translate | burn | all`; shared `--model --target-lang --source-lang -o`. Friendly missing-file errors. Commit `feat: CLI`.

### Task 8: Tkinter GUI
URL entry, folder picker, language dropdown, model dropdown, Run-all + per-stage buttons, threaded progress log. Commit `feat: tkinter GUI`.

### Task 9: Windows `.bat` launchers
`install.bat`, `run_gui.bat`, `run_cli.bat` with venv auto-bootstrap. Commit `feat: windows launchers`.

### Task 10: README
Install (double-click install.bat), GUI + CLI usage, troubleshooting. Commit `docs: readme`.

### Task 11: Real smoke test
Synthetic video (ffmpeg testsrc + tone) → `all` end-to-end locally; verify `final_*.mp4` exists with correct structure. Then verify on a real Wix URL.

### Task 12 (conditional): Wix fallback scraper
Only if yt-dlp generic fails on real Wix pages: regex-scrape page HTML for `video.wixstatic.com` links, download directly.

## Risks & mitigations

- **Wix pages hide video URLs in JS** — yt-dlp generic usually finds them; Task 12 fallback; worst case README shows DevTools → Network method for the direct `file.mp4` URL.
- **CPU transcription speed** — `small` int8 ≈ real-time-ish on modern CPUs; `tiny`/`base` for drafts.
- **Google endpoint rate limits** — batching + retries.
- **ffmpeg not on PATH** — imageio-ffmpeg wheel ships a static binary; auto-located.
- **Long videos** — staging means a crash never loses earlier work; `all` resumes.
- **Tkinter threading** — worker thread + queue; UI never blocks.

## Verification (all must pass before "done")

1. `pytest tests/ -v` → all pass (offline, mocked externals).
2. Synthetic end-to-end run produces `final_*.mp4`.
3. Real Wix URL: stages 1–2 verified on the friend's site.
4. On Windows: `install.bat` → `run_gui.bat` works with no manual PATH edits.
