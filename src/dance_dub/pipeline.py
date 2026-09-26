"""Dance Dub pipeline: download → extract audio → transcribe → translate → burn subtitles.

Every stage is a file-to-file function: it reads the previous stage's output
from the per-video working folder and writes its own. Stages skip themselves
when their output already exists (unless force=True), so the pipeline can be
re-run to resume after a crash.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

LogFn = Callable[[str], None]

DEFAULT_STYLE = (
    "FontName=Arial,FontSize=24,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=1,Shadow=0,MarginV=30"
)
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}


def default_log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# Pure helpers (unit-tested offline)
# --------------------------------------------------------------------------

def slugify(text: str) -> str:
    """Turn a URL or title into a safe folder name."""
    text = text.strip().split("?")[0].split("#")[0]
    text = text.rstrip("/")
    base = text.rsplit("/", 1)[-1] if "/" in text else text
    if "." in base and not base.startswith("."):
        base = base.rsplit(".", 1)[0]
    base = re.sub(r"[^\w\s-]", "", base, flags=re.UNICODE)
    base = re.sub(r"[\s_-]+", "-", base).strip("-")
    return base[:60] or "video"


def find_video(workdir: Path) -> Optional[Path]:
    """Locate the downloaded video inside a workdir (video.* by yt-dlp outtmpl)."""
    for ext in sorted(VIDEO_EXTS, key=lambda e: 0 if e == ".mp4" else 1):
        candidate = workdir / f"video{ext}"
        if candidate.exists():
            return candidate
    return None


def srt_timestamp(seconds: float) -> str:
    """Format seconds as an SRT timestamp HH:MM:SS,mmm."""
    if seconds < 0:
        seconds = 0
    millis = int(round(seconds * 1000))
    h, rem = divmod(millis, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(segments: Sequence[Tuple[float, float, str]]) -> str:
    """Build an SRT document from (start, end, text) segment tuples."""
    blocks = []
    n = 0
    for start, end, text in segments:
        text = text.strip().replace("\n", " ")
        if not text:
            continue
        n += 1
        blocks.append(f"{n}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}\n")
    return "\n".join(blocks)


# --- SRT parsing / writing --------------------------------------------------

_SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def parse_srt(text: str) -> List[Tuple[str, str, str]]:
    """Parse SRT into a list of (index, 'start --> end', text) blocks."""
    blocks: List[Tuple[str, str, str]] = []
    chunks = re.split(r"\r?\n\r?\n+", text.strip())
    for chunk in chunks:
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        idx = 0
        if _SRT_TIME_RE.search(lines[0]):
            index = ""
        else:
            index = lines[0].strip()
            idx = 1
        if idx >= len(lines):
            continue
        m = _SRT_TIME_RE.search(lines[idx])
        if not m:
            continue
        timespan = lines[idx].strip()
        body = " ".join(ln.strip() for ln in lines[idx + 1 :])
        blocks.append((index, timespan, body))
    return blocks


def write_srt(blocks: Sequence[Tuple[str, str, str]]) -> str:
    out = []
    for i, (index, timespan, text) in enumerate(blocks, start=1):
        out.append(f"{index or i}\n{timespan}\n{text}\n")
    return "\n".join(out)


# --- ffmpeg command builders -------------------------------------------------

def ffmpeg_exe() -> str:
    """Return path to ffmpeg.

    The imageio-ffmpeg wheel binary is preferred: it is version-pinned and
    built with libass (needed by the subtitles filter), which system builds
    sometimes lack. Falls back to system PATH.
    """
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    from shutil import which

    exe = which("ffmpeg")
    if exe:
        return exe
    raise RuntimeError(
        "ffmpeg not found. Re-run install.bat (installs a bundled ffmpeg), "
        "or install ffmpeg and put it on PATH."
    )


def extract_audio_cmd(ffmpeg: str, video: Path, audio: Path) -> List[str]:
    return [
        ffmpeg,
        "-y",
        "-i",
        str(video),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(audio),
    ]


def _escape_filter_path(path: Path) -> str:
    """Escape a path for use inside an ffmpeg filtergraph on any OS."""
    p = str(path)
    p = p.replace("\\", "/")
    p = p.replace(":", "\\:")
    p = p.replace("'", "\\'")
    return p


def burn_cmd(
    ffmpeg: str, video: Path, srt: Path, out: Path, style: str = DEFAULT_STYLE
) -> List[str]:
    vf = f"subtitles='{_escape_filter_path(srt)}':force_style='{style}'"
    return [
        ffmpeg,
        "-y",
        "-i",
        str(video),
        "-vf",
        vf,
        "-c:a",
        "copy",
        str(out),
    ]


# --------------------------------------------------------------------------
# Stage 1: download
# --------------------------------------------------------------------------

_WIX_VIDEO_RE = re.compile(
    r"https?://(?:video|static)\.wixstatic\.com/(?:video|media)/[A-Za-z0-9_\-./%]+?\.mp4",
    re.IGNORECASE,
)


def find_wix_video_urls(page_url: str, html: str) -> List[str]:
    """Extract direct wixstatic MP4 URLs from page HTML (deduped, best first)."""
    seen: List[str] = []
    for m in _WIX_VIDEO_RE.finditer(html):
        url = m.group(0)
        if url not in seen:
            seen.append(url)

    def quality(u: str) -> int:
        q = re.search(r"/(\d{3,4})p/", u)
        return int(q.group(1)) if q else 0

    return sorted(seen, key=quality, reverse=True)


def _download_direct(url: str, dest: Path) -> None:
    """Stream a direct file URL to dest with urllib (no external deps)."""
    import urllib.request

    with urllib.request.urlopen(url, timeout=60) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def _wix_scrape_fallback(page_url: str, workdir: Path, log: LogFn) -> None:
    """Last resort: fetch the page HTML and pull direct wixstatic MP4 URLs."""
    import urllib.request

    if not page_url.lower().startswith("http"):
        return
    req = urllib.request.Request(
        page_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log(f"[download] page fetch failed: {exc}")
        return
    candidates = find_wix_video_urls(page_url, html)
    if not candidates:
        log("[download] no wixstatic video links found in page HTML")
        return
    for i, candidate in enumerate(candidates[:3]):
        dest = workdir / "video.mp4"
        try:
            log(f"[download] direct download: {candidate}")
            _download_direct(candidate, dest)
            if dest.stat().st_size > 10_000:  # plausible video, not an error page
                return
            dest.unlink(missing_ok=True)
        except Exception as exc:
            log(f"[download] candidate failed: {exc}")


def download_video(
    url: str,
    workdir: Path,
    log: LogFn = default_log,
    force: bool = False,
) -> Path:
    """Download `url` into workdir as video.<ext>. Returns the video path."""
    import yt_dlp

    workdir.mkdir(parents=True, exist_ok=True)
    existing = find_video(workdir)
    if existing and not force:
        log(f"[download] already exists, skipping: {existing.name}")
        return existing

    outtmpl = str(workdir / "video.%(ext)s")
    options = {
        "format": "bv*+ba/b",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "quiet": True,
        "no_warnings": True,
    }
    # Merging separate video+audio streams needs ffmpeg; point yt-dlp at the
    # bundled one so it works on machines without ffmpeg on PATH.
    try:
        options["ffmpeg_location"] = ffmpeg_exe()
    except RuntimeError:
        pass
    log(f"[download] fetching {url}")
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])
    except Exception as exc:
        log(f"[download] yt-dlp failed ({type(exc).__name__}); trying Wix page scrape…")
        _wix_scrape_fallback(url, workdir, log)

    video = find_video(workdir)
    if not video:
        raise RuntimeError(
            "Download finished but no video file was created. "
            "If this is a Wix page, try the direct video URL "
            "(see README 'Troubleshooting')."
        )
    log(f"[download] saved {video.name}")
    return video


# --------------------------------------------------------------------------
# Stage 2: extract audio
# --------------------------------------------------------------------------

def _run_ffmpeg(cmd: List[str], log: LogFn) -> None:
    """Run ffmpeg, surfacing its stderr on failure instead of a bare traceback."""
    # CREATE_NO_WINDOW: the packaged exe has no console, so without it every
    # ffmpeg call would flash a black console window on Windows.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.splitlines()[-12:])
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}):\n{tail}")


def extract_audio(
    workdir: Path,
    log: LogFn = default_log,
    force: bool = False,
) -> Path:
    video = find_video(workdir)
    if not video:
        raise FileNotFoundError("No video in folder — run the download step first.")
    audio = workdir / "audio.wav"
    if audio.exists() and not force:
        log("[extract] audio.wav exists, skipping")
        return audio
    cmd = extract_audio_cmd(ffmpeg_exe(), video, audio)
    log("[extract] extracting audio (16 kHz mono)")
    _run_ffmpeg(cmd, log)
    log("[extract] done")
    return audio


# --------------------------------------------------------------------------
# Stage 3: transcribe
# --------------------------------------------------------------------------

def _is_missing_cuda(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("cublas", "cudnn", "cuda"))


def transcribe_audio(
    workdir: Path,
    model_size: str = "small",
    log: LogFn = default_log,
    force: bool = False,
    model_factory=None,
) -> Path:
    """Transcribe audio.wav → transcript.srt + transcript.txt. Returns SRT path."""
    audio = workdir / "audio.wav"
    if not audio.exists():
        raise FileNotFoundError("No audio.wav in folder — run extract-audio first.")
    srt = workdir / "transcript.srt"
    txt = workdir / "transcript.txt"
    if srt.exists() and not force:
        log("[transcribe] transcript.srt exists, skipping")
        return srt

    cpu_fallback = None
    if model_factory is None:
        from faster_whisper import WhisperModel

        def model_factory(size: str):
            return WhisperModel(size, device="auto", compute_type="int8")

        def cpu_fallback(size: str):
            return WhisperModel(size, device="cpu", compute_type="int8")

    def run(model):
        segments_iter, info = model.transcribe(str(audio), vad_filter=True)
        return [(seg.start, seg.end, seg.text) for seg in segments_iter], info

    log(f"[transcribe] loading model '{model_size}' (first run downloads it)")
    model = model_factory(model_size)
    log("[transcribe] transcribing…")
    try:
        segments, info = run(model)
    except RuntimeError as exc:
        # device="auto" picks CUDA whenever an NVIDIA GPU is present, but the
        # CUDA libraries (cuBLAS/cuDNN) are often missing; that only surfaces
        # once transcription starts. Fall back to the CPU instead of failing.
        if cpu_fallback is None or not _is_missing_cuda(exc):
            raise
        log("[transcribe] GPU libraries not available, using the CPU instead")
        segments, info = run(cpu_fallback(model_size))
    srt.write_text(build_srt(segments), encoding="utf-8")
    txt.write_text(
        "\n".join(t for _, _, t in segments).strip(), encoding="utf-8"
    )
    (workdir / "detected_language.txt").write_text(
        getattr(info, "language", "") or "", encoding="utf-8"
    )
    log(f"[transcribe] detected language: {info.language} "
        f"(p={info.language_probability:.2f})")
    log(f"[transcribe] wrote {srt.name} and {txt.name}")
    return srt


# --------------------------------------------------------------------------
# Stage 4: translate
# --------------------------------------------------------------------------

_MAX_BATCH_CHARS = 4000
_BATCH_SEP = "\n@@@\n"

# Locale-style codes required by MyMemory (fallback translation provider).
_MM_MAP = {
    "en": "en-US", "es": "es-ES", "fr": "fr-FR", "de": "de-DE", "it": "it-IT",
    "pt": "pt-PT", "nl": "nl-NL", "pl": "pl-PL", "zh-CN": "zh-CN", "zh": "zh-CN",
    "ja": "ja-JP", "ko": "ko-KR", "ar": "ar-SA", "hi": "hi-IN", "ru": "ru-RU",
    "tr": "tr-TR", "vi": "vi-VN", "id": "id-ID", "th": "th-TH", "he": "he-IL",
    "el": "el-GR", "sv": "sv-SE", "da": "da-DK", "fi": "fi-FI", "no": "nb-NO",
    "cs": "cs-CZ", "hu": "hu-HU", "ro": "ro-RO", "uk": "uk-UA", "ms": "ms-MY",
}


class _ChainTranslator:
    """Tries each provider in order; first non-empty result wins."""

    def __init__(self, providers: List[Tuple[str, object]]):
        self.providers = providers
        self.errors: List[str] = []

    def translate(self, text: str) -> str:
        self.errors = []
        for name, tr in self.providers:
            try:
                result = tr.translate(text)
                if result:
                    return result
            except Exception as exc:
                self.errors.append(f"{name}: {exc}")
        raise RuntimeError(
            "all translation providers failed: " + "; ".join(self.errors[-2:])
        )


def _mymemory_provider(source: str, target: str):
    """Wrap MyMemory: maps codes, splits >450-char text cue-by-cue."""
    from deep_translator import MyMemoryTranslator

    src = _MM_MAP.get(source, "en-US" if source in ("auto", "") else source)
    tgt = _MM_MAP.get(target, target)

    class _MyMemory:
        def translate(self, text: str) -> str:
            translator = MyMemoryTranslator(source=src, target=tgt)
            if _BATCH_SEP not in text:
                return self._chunked(translator, text)
            cues = text.split(_BATCH_SEP)
            out = []
            for i, cue in enumerate(cues):
                if i:
                    time.sleep(0.15)  # stay under MyMemory's rate limit
                out.append(self._chunked(translator, cue))
            return _BATCH_SEP.join(out)

        @staticmethod
        def _chunked(translator, text: str) -> str:
            if len(text) <= 450:
                return translator.translate(text)
            # Oversized cue: translate in word chunks and join.
            words, buf, out = text.split(" "), "", []
            for w in words:
                if len(buf) + len(w) + 1 > 450:
                    out.append(translator.translate(buf.strip()))
                    buf = ""
                    time.sleep(0.15)
                buf += w + " "
            if buf.strip():
                out.append(translator.translate(buf.strip()))
            return " ".join(out)

    return _MyMemory()


def default_translator_factory(source: str, target: str):
    """Google (fast, plain codes, auto-detect) with MyMemory as fallback."""
    from deep_translator import GoogleTranslator

    google = GoogleTranslator(source=source or "auto", target=target)
    return _ChainTranslator([("google", google), ("mymemory", _mymemory_provider(source, target))])


def read_detected_language(workdir: Path) -> Optional[str]:
    f = workdir / "detected_language.txt"
    if f.exists():
        lang = f.read_text(encoding="utf-8").strip()
        if lang:
            return lang
    return None


def translate_srt_file(
    srt_path: Path,
    target_lang: str,
    source_lang: str = "auto",
    log: LogFn = default_log,
    force: bool = False,
    translator_factory=None,
) -> Path:
    """Translate an SRT file, preserving indices and timestamps. Returns new path."""
    out_path = srt_path.with_name(srt_path.stem + "_translated.srt")
    if out_path.exists() and not force:
        log(f"[translate] {out_path.name} exists, skipping")
        return out_path
    if not srt_path.exists():
        raise FileNotFoundError(f"{srt_path} not found — run transcribe first.")

    if translator_factory is None:
        translator_factory = default_translator_factory

    blocks = parse_srt(srt_path.read_text(encoding="utf-8"))
    if not blocks:
        log("[translate] nothing to translate (empty transcript)")
        out_path.write_text("", encoding="utf-8")
        return out_path

    translator = translator_factory(source_lang, target_lang)

    # Batch: join many subtitle lines with a separator so one request covers
    # many blocks; the separator keeps blocks apart after translation.
    batches: List[List[int]] = []
    texts: List[str] = [b[2] for b in blocks]
    current: List[int] = []
    current_len = 0
    for i, text in enumerate(texts):
        add_len = len(text) + len(_BATCH_SEP)
        if current and current_len + add_len > _MAX_BATCH_CHARS:
            batches.append(current)
            current, current_len = [], 0
        current.append(i)
        current_len += add_len
    if current:
        batches.append(current)

    translated = list(texts)
    log(f"[translate] {len(blocks)} cues in {len(batches)} request(s) → {target_lang}")
    for bi, batch in enumerate(batches, start=1):
        joined = _BATCH_SEP.join(texts[i] for i in batch)
        result = joined
        last_err: Optional[Exception] = None
        for attempt in range(4):
            exc: Optional[Exception] = None
            try:
                result = translator.translate(joined)
                if result:
                    break
            except Exception as e:  # network hiccups, rate limits
                exc = last_err = e
            wait = 5 * (attempt + 1)
            reason = exc or "empty result"
            log(f"[translate] request failed ({reason}); retry in {wait}s")
            time.sleep(wait)
        else:
            raise RuntimeError(
                "Translation kept failing (Google free endpoint rate limit?). "
                "Wait a minute and re-run the translate step — earlier steps are kept."
            ) from last_err
        if not isinstance(result, str) or _BATCH_SEP not in result:
            # Separator lost (translator mangled it): fall back per-cue.
            for i in batch:
                try:
                    translated[i] = translator.translate(texts[i]) or texts[i]
                except Exception:
                    translated[i] = texts[i]
        else:
            parts = [p.strip() for p in result.split(_BATCH_SEP)]
            if len(parts) != len(batch):
                for i in batch:
                    try:
                        translated[i] = translator.translate(texts[i]) or texts[i]
                    except Exception:
                        translated[i] = texts[i]
            else:
                for i, part in zip(batch, parts):
                    translated[i] = part
        log(f"[translate] batch {bi}/{len(batches)} done")

    out_blocks = [
        (blocks[i][0], blocks[i][1], translated[i]) for i in range(len(blocks))
    ]
    out_path.write_text(write_srt(out_blocks), encoding="utf-8")
    log(f"[translate] wrote {out_path.name}")
    return out_path


# --------------------------------------------------------------------------
# Stage 5: burn subtitles
# --------------------------------------------------------------------------

def burn_subtitles(
    workdir: Path,
    style: str = DEFAULT_STYLE,
    log: LogFn = default_log,
    force: bool = False,
) -> Path:
    video = find_video(workdir)
    if not video:
        raise FileNotFoundError("No video in folder — run the download step first.")
    srt = workdir / "transcript_translated.srt"
    if not srt.exists():
        srt = workdir / "transcript.srt"
    if not srt.exists():
        raise FileNotFoundError(
            "No transcript SRT in folder — run transcribe (and translate) first."
        )
    if not srt.read_text(encoding="utf-8").strip():
        raise RuntimeError(
            "No speech was detected in this video, so there are no subtitles "
            "to burn. (Music-only or silent videos can't be captioned.)"
        )
    out = workdir / f"final_{workdir.name}.mp4"
    if out.exists() and not force:
        log(f"[burn] {out.name} exists, skipping")
        return out
    cmd = burn_cmd(ffmpeg_exe(), video, srt, out, style)
    log(f"[burn] burning subtitles from {srt.name} (this re-encodes the video)…")
    _run_ffmpeg(cmd, log)
    log(f"[burn] wrote {out.name}")
    return out


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

def start_from_file(
    video_path: Path,
    out_root: Path,
    target_lang: str = "en",
    source_lang: str = "auto",
    model_size: str = "small",
    style: str = DEFAULT_STYLE,
    log: LogFn = default_log,
) -> Path:
    """Run stages 2-5 on a video already on disk. Returns the final video path."""
    video_path = Path(video_path).expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(f"File not found: {video_path}")
    if video_path.suffix.lower() not in VIDEO_EXTS:
        raise ValueError(
            f"Unsupported file type '{video_path.suffix}'. "
            f"Supported: {', '.join(sorted(VIDEO_EXTS))}"
        )
    workdir = out_root / slugify(video_path.stem or "local-video")
    workdir.mkdir(parents=True, exist_ok=True)
    dest = workdir / f"video{video_path.suffix.lower()}"
    if video_path != dest:
        if dest.exists():
            dest.unlink()
        import shutil

        shutil.copy2(video_path, dest)
        log(f"[download] copied {video_path.name} into {workdir.name}")
    return run_pipeline(workdir, target_lang=target_lang, source_lang=source_lang,
                        model_size=model_size, style=style, log=log)


def run_pipeline(
    workdir: Path,
    target_lang: str = "en",
    source_lang: str = "auto",
    model_size: str = "small",
    style: str = DEFAULT_STYLE,
    log: LogFn = default_log,
) -> Path:
    """Run stages 2-5 inside an existing workdir (assumes video present)."""
    extract_audio(workdir, log=log)
    transcribe_audio(workdir, model_size=model_size, log=log)
    translate_srt_file(
        workdir / "transcript.srt",
        target_lang=target_lang,
        source_lang=source_lang,
        log=log,
    )
    return burn_subtitles(workdir, style=style, log=log)


def run_all(
    url: str,
    out_root: Path,
    target_lang: str = "en",
    source_lang: str = "auto",
    model_size: str = "small",
    style: str = DEFAULT_STYLE,
    log: LogFn = default_log,
) -> Path:
    """Run the full pipeline for one URL. Returns the final video path."""
    workdir = out_root / slugify(url)
    workdir.mkdir(parents=True, exist_ok=True)
    download_video(url, workdir, log=log)
    return run_pipeline(workdir, target_lang=target_lang, source_lang=source_lang,
                        model_size=model_size, style=style, log=log)
