"""Dance Dub pipeline: download → extract audio → transcribe → translate → burn subtitles.

Every stage is a file-to-file function: it reads the previous stage's output
from the per-video working folder and writes its own. Stages skip themselves
when their output already exists (unless force=True), so the pipeline can be
re-run to resume after a crash.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

LogFn = Callable[[str], None]

DEFAULT_STYLE = (
    "FontName=Arial,FontSize=24,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=1,Shadow=0,MarginV=30"
)
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}

# Video quality for the subtitled copy. "standard" is ffmpeg's default x264
# setting (CRF 23), which usually comes out smaller than the source;
# "original" aims for the source's own bitrate so the copy looks the same.
QUALITIES = ("standard", "original")

# The two subtitle tracks a work folder can hold, by the name the app uses.
SUB_TRACKS = {"original": "transcript.srt", "translated": "transcript_translated.srt"}


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


def srt_to_vtt(srt_text: str) -> str:
    """Convert SRT to WebVTT (what web players such as Wix's take as captions)."""
    out = ["WEBVTT", ""]
    for _, timespan, text in parse_srt(srt_text):
        t = _SRT_TIME_RE.search(timespan)
        if not t or not text.strip():
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = t.groups()
        out.append(f"{int(h1):02d}:{m1}:{s1}.{ms1.ljust(3, '0')} --> "
                   f"{int(h2):02d}:{m2}:{s2}.{ms2.ljust(3, '0')}")
        # "-->" may not appear in cue text, and & and < start markup.
        out.append(text.strip().replace("&", "&amp;").replace("<", "&lt;")
                   .replace("-->", "--&gt;"))
        out.append("")
    return "\n".join(out)


def write_vtt(srt_path: Path) -> Path:
    """Write <name>.vtt next to an SRT file, refreshing it if the SRT is newer."""
    vtt = srt_path.with_suffix(".vtt")
    if not vtt.exists() or vtt.stat().st_mtime < srt_path.stat().st_mtime:
        vtt.write_text(srt_to_vtt(srt_path.read_text(encoding="utf-8")),
                       encoding="utf-8")
    return vtt


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


_VIDEO_SIZE_RE = re.compile(r"Video: .*?, (\d{2,5})x(\d{2,5})")
_ROTATION_RE = re.compile(r"(?:rotation of|rotate\s*:)\s*(-?\d+(?:\.\d+)?)")


def probe_video_size(ffmpeg: str, video: Path) -> Optional[Tuple[int, int]]:
    """(width, height) as the video is displayed (phone rotation applied)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(video)],
        capture_output=True, text=True, errors="replace", creationflags=flags,
    )
    m = _VIDEO_SIZE_RE.search(proc.stderr)
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    rot = _ROTATION_RE.search(proc.stderr)
    if rot and round(abs(float(rot.group(1)))) % 180 == 90:
        w, h = h, w
    return w, h


_DURATION_KBPS_RE = re.compile(r"Duration: .*?bitrate: (\d+) kb/s")
_STREAM_RE = re.compile(r"Stream #.*?: (Video|Audio): (.*)")
_KBPS_RE = re.compile(r"(\d+) kb/s")


def video_kbps_from_probe(stderr: str) -> Optional[int]:
    """The video stream's bitrate in kb/s from `ffmpeg -i` output.

    MP4s list it on the stream line. MKV/WebM often don't, so fall back to
    the container's total bitrate minus the audio streams'.
    """
    total, audio, video = None, 0, None
    for line in stderr.splitlines():
        m = _DURATION_KBPS_RE.search(line)
        if m:
            total = int(m.group(1))
            continue
        m = _STREAM_RE.search(line)
        if not m:
            continue
        k = _KBPS_RE.search(m.group(2))
        if m.group(1) == "Video":
            if video is None and k:
                video = int(k.group(1))
        elif k:
            audio += int(k.group(1))
    if video:
        return video
    if total and total > audio:
        return total - audio
    return None


def probe_video_kbps(ffmpeg: str, video: Path) -> Optional[int]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(video)],
        capture_output=True, text=True, errors="replace", creationflags=flags,
    )
    return video_kbps_from_probe(proc.stderr)


def video_encode_args(quality: str, source_kbps: Optional[int]) -> List[str]:
    """ffmpeg video-encoder flags for a quality setting (see QUALITIES)."""
    if quality != "original":
        return []
    if source_kbps:
        return ["-c:v", "libx264", "-preset", "medium", "-b:v", f"{source_kbps}k",
                "-maxrate", f"{source_kbps * 3 // 2}k", "-bufsize", f"{source_kbps * 2}k"]
    # Bitrate unknown: a CRF this low is visually the same as the source.
    return ["-c:v", "libx264", "-preset", "slow", "-crf", "17"]


def _ass_time(h: str, m: str, s: str, ms: str) -> str:
    cs = int(ms.ljust(3, "0")) // 10
    return f"{int(h)}:{m}:{s}.{cs:02d}"


def srt_to_ass(srt_text: str, width: int, height: int) -> str:
    """Convert SRT to ASS laid out in the video's own pixels.

    Burning plain SRT makes libass size text against a 384x288 canvas scaled
    to the video's height, so on a tall phone video the captions came out
    huge. Here the canvas is the video itself, and the font is a fixed share
    of its shorter side, so captions look the same on portrait and landscape.
    """
    short = min(width, height)
    size = max(12, round(short * 0.055))
    outline = max(1, round(size * 0.08))
    margin_v = round(height * 0.06)
    margin_lr = round(width * 0.06)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,Arial,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},0,2,{margin_lr},{margin_lr},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for _, timespan, text in parse_srt(srt_text):
        t = _SRT_TIME_RE.search(timespan)
        if not t or not text.strip():
            continue
        start, end = _ass_time(*t.groups()[:4]), _ass_time(*t.groups()[4:])
        # Braces start ASS override tags; keep them as literal text.
        body = text.strip().replace("{", "(").replace("}", ")").replace("\n", "\\N")
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{body}")
    return "\n".join(lines) + "\n"


def burn_cmd(
    ffmpeg: str, video: Path, srt: Path, out: Path, style: Optional[str] = DEFAULT_STYLE,
    video_args: Sequence[str] = (),
) -> List[str]:
    vf = f"subtitles='{_escape_filter_path(srt)}'"
    if style:
        vf += f":force_style='{style}'"
    return [
        ffmpeg,
        "-y",
        "-i",
        str(video),
        "-vf",
        vf,
        *video_args,
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


def download_file_url(
    url: str,
    workdir: Path,
    log: LogFn = default_log,
    force: bool = False,
) -> Path:
    """Download a known direct video link (e.g. from the Wix API) as video.mp4."""
    workdir.mkdir(parents=True, exist_ok=True)
    existing = find_video(workdir)
    if existing and not force:
        log(f"[download] already exists, skipping: {existing.name}")
        return existing
    if not url:
        raise RuntimeError("Wix gave no download link for this video.")
    dest = workdir / "video.mp4"
    part = workdir / "video.mp4.part"
    log("[download] fetching video from Wix")
    _download_direct(url, part)
    if part.stat().st_size <= 10_000:  # an error page, not a video
        part.unlink(missing_ok=True)
        raise RuntimeError("Wix returned an empty or invalid video file.")
    part.replace(dest)
    log(f"[download] saved {dest.name}")
    return dest


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
        write_vtt(srt)
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
    vtt = write_vtt(srt)
    log(f"[transcribe] wrote {srt.name}, {vtt.name} and {txt.name}")
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
        write_vtt(out_path)
        return out_path
    if not srt_path.exists():
        raise FileNotFoundError(f"{srt_path} not found — run transcribe first.")

    if translator_factory is None:
        translator_factory = default_translator_factory

    blocks = parse_srt(srt_path.read_text(encoding="utf-8"))
    if not blocks:
        log("[translate] nothing to translate (empty transcript)")
        out_path.write_text("", encoding="utf-8")
        write_vtt(out_path)
        return out_path

    # Use the language Whisper detected rather than "auto": the fallback
    # provider (MyMemory) can't auto-detect and would treat the text as
    # English, handing French back untranslated when the target is English.
    if source_lang in ("auto", "", None):
        source_lang = read_detected_language(srt_path.parent) or "auto"
    if source_lang.split("-")[0].lower() == target_lang.split("-")[0].lower():
        log(f"[translate] already in {target_lang}, keeping the original text")
        out_path.write_text(write_srt(blocks), encoding="utf-8")
        write_vtt(out_path)
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
    log(f"[translate] {len(blocks)} cues in {len(batches)} request(s), "
        f"{source_lang} → {target_lang}")
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
    vtt = write_vtt(out_path)
    log(f"[translate] wrote {out_path.name} and {vtt.name}")
    return out_path


# --------------------------------------------------------------------------
# Stage 5: burn subtitles
# --------------------------------------------------------------------------

def _burn_source(workdir: Path, subs: str) -> Path:
    """The SRT to burn: "original", "translated", or "auto" (translated if any)."""
    if subs in SUB_TRACKS:
        srt = workdir / SUB_TRACKS[subs]
        if not srt.exists():
            raise FileNotFoundError(
                f"No {subs} subtitles ({srt.name}) in folder — run "
                + ("translate" if subs == "translated" else "transcribe") + " first."
            )
        return srt
    srt = workdir / "transcript_translated.srt"
    if not srt.exists():
        srt = workdir / "transcript.srt"
    if not srt.exists():
        raise FileNotFoundError(
            "No transcript SRT in folder — run transcribe (and translate) first."
        )
    return srt


def burn_subtitles(
    workdir: Path,
    style: Optional[str] = None,
    log: LogFn = default_log,
    force: bool = False,
    quality: str = "standard",
    subs: str = "auto",
) -> Path:
    video = find_video(workdir)
    if not video:
        raise FileNotFoundError("No video in folder — run the download step first.")
    srt = _burn_source(workdir, subs)
    if not srt.read_text(encoding="utf-8").strip():
        raise RuntimeError(
            "No speech was detected in this video, so there are no subtitles "
            "to burn. (Music-only or silent videos can't be captioned.)"
        )
    out = workdir / f"final_{workdir.name}.mp4"
    # What the existing final video was made with; older ones have no record
    # and were standard quality.
    settings_file = workdir / "final_settings.json"
    settings = {"quality": quality, "subs": srt.name}
    if out.exists() and not force:
        try:
            made = json.loads(settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            made = {"quality": "standard", "subs": srt.name}
        if made == settings:
            log(f"[burn] {out.name} exists, skipping")
            return out
        log(f"[burn] {out.name} was made with other settings, making it again")
    ffmpeg = ffmpeg_exe()
    burn_file, burn_style = srt, style or DEFAULT_STYLE
    if style is None:
        size = probe_video_size(ffmpeg, video)
        if size:
            burn_file = workdir / "subtitles.ass"
            burn_file.write_text(
                srt_to_ass(srt.read_text(encoding="utf-8"), *size), encoding="utf-8"
            )
            burn_style = None
    video_args: List[str] = []
    if quality == "original":
        kbps = probe_video_kbps(ffmpeg, video)
        video_args = video_encode_args(quality, kbps)
        log(f"[burn] keeping the original quality ({kbps} kb/s)" if kbps
            else "[burn] keeping the original quality (near-lossless setting)")
    # Encode to a side file so a failed or cancelled run keeps the old video.
    part = workdir / f"final_{workdir.name}.part.mp4"
    cmd = burn_cmd(ffmpeg, video, burn_file, part, burn_style, video_args)
    log(f"[burn] burning subtitles from {srt.name} (this re-encodes the video)…")
    try:
        _run_ffmpeg(cmd, log)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    part.replace(out)
    settings_file.write_text(json.dumps(settings), encoding="utf-8")
    log(f"[burn] wrote {out.name}")
    return out


# --------------------------------------------------------------------------
# Subtitle editing
# --------------------------------------------------------------------------

_EDIT_TIME_RE = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{1,2})(?:[,.](\d{1,3}))?$")


def parse_timestamp(text: str) -> float:
    """Seconds from "H:MM:SS,mmm", also taking "MM:SS" and a "." for the comma."""
    m = _EDIT_TIME_RE.match(text.strip())
    if not m:
        raise ValueError(f"'{text}' is not a time like 00:01:02,500")
    h, mi, se, ms = m.groups()
    if int(mi) > 59 or int(se) > 59:
        raise ValueError(f"'{text}' is not a valid time")
    return int(h or 0) * 3600 + int(mi) * 60 + int(se) + int((ms or "0").ljust(3, "0")) / 1000


def load_cues(workdir: Path, track: str) -> List[Dict[str, str]]:
    """A subtitle track as [{start, end, text}] for the editor."""
    srt = workdir / SUB_TRACKS[track]
    if not srt.exists():
        raise FileNotFoundError(f"This video has no {track} subtitles yet.")
    cues = []
    for _, timespan, text in parse_srt(srt.read_text(encoding="utf-8")):
        start, end = (t.strip() for t in timespan.split("-->", 1))
        cues.append({"start": start, "end": end, "text": text})
    return cues


def save_cues(workdir: Path, track: str, cues: Sequence[Dict[str, str]]) -> Path:
    """Write edited cues back to a track's SRT (and VTT).

    The first save keeps the untouched file as <name>.srt.bak. Cues with no
    text are dropped, and cues are put in time order.
    """
    srt = workdir / SUB_TRACKS[track]
    timed = []
    for n, cue in enumerate(cues, start=1):
        text = " ".join(str(cue.get("text") or "").split())
        if not text:
            continue
        try:
            start = parse_timestamp(str(cue.get("start") or ""))
            end = parse_timestamp(str(cue.get("end") or ""))
        except ValueError as exc:
            raise ValueError(f"Line {n}: {exc}") from None
        if end <= start:
            raise ValueError(f"Line {n}: the end time must be after the start time.")
        timed.append((start, end, text))
    timed.sort(key=lambda c: c[0])
    backup = srt.with_name(srt.name + ".bak")
    if srt.exists() and not backup.exists():
        backup.write_bytes(srt.read_bytes())
    srt.write_text(build_srt(timed), encoding="utf-8")
    srt.with_suffix(".vtt").write_text(srt_to_vtt(srt.read_text(encoding="utf-8")),
                                       encoding="utf-8")
    if track == "original":
        (workdir / "transcript.txt").write_text(
            "\n".join(t for _, _, t in timed), encoding="utf-8")
    return srt


def find_subtitled_folders(out_root: Path, limit: int = 500) -> List[Path]:
    """Work folders under out_root that have a video and a transcript."""
    found = []
    if not out_root.is_dir():
        return found
    for srt in sorted(out_root.rglob("transcript.srt")):
        if find_video(srt.parent):
            found.append(srt.parent)
            if len(found) >= limit:
                break
    return found


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

def start_from_file(
    video_path: Path,
    out_root: Path,
    target_lang: str = "en",
    source_lang: str = "auto",
    model_size: str = "small",
    style: Optional[str] = None,
    log: LogFn = default_log,
    quality: str = "standard",
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
                        model_size=model_size, style=style, log=log, quality=quality)


def run_pipeline(
    workdir: Path,
    target_lang: str = "en",
    source_lang: str = "auto",
    model_size: str = "small",
    style: Optional[str] = None,
    log: LogFn = default_log,
    quality: str = "standard",
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
    return burn_subtitles(workdir, style=style, log=log, quality=quality)


def run_all(
    url: str,
    out_root: Path,
    target_lang: str = "en",
    source_lang: str = "auto",
    model_size: str = "small",
    style: Optional[str] = None,
    log: LogFn = default_log,
    quality: str = "standard",
) -> Path:
    """Run the full pipeline for one URL. Returns the final video path."""
    workdir = out_root / slugify(url)
    workdir.mkdir(parents=True, exist_ok=True)
    download_video(url, workdir, log=log)
    return run_pipeline(workdir, target_lang=target_lang, source_lang=source_lang,
                        model_size=model_size, style=style, log=log, quality=quality)
