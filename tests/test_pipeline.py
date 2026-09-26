"""Offline unit tests for dance_dub.pipeline (all externals mocked)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dance_dub import pipeline as P  # noqa: E402


# --- slugify ---------------------------------------------------------------

def test_slugify_wix_page_url():
    url = "https://my-site.wixsite.com/dance/performances-2024"
    assert P.slugify(url) == "performances-2024"


def test_slugify_direct_mp4():
    assert P.slugify("https://video.wixstatic.com/video/827c6d_ab..12/1080p/mp4/file.mp4") == "file"


def test_slugify_fallback():
    assert P.slugify("https://example.com/") == "example"


# --- SRT formatting ----------------------------------------------------------

def test_srt_timestamp():
    assert P.srt_timestamp(0) == "00:00:00,000"
    assert P.srt_timestamp(3661.5) == "01:01:01,500"
    assert P.srt_timestamp(59.9996) == "00:01:00,000"  # rounds up


def test_build_srt_skips_empty_text():
    srt = P.build_srt([(0.0, 1.0, "  "), (1.0, 2.0, "hello")])
    assert srt == "1\n00:00:01,000 --> 00:00:02,000\nhello\n"


def test_build_srt_numbering():
    srt = P.build_srt([(0, 1, "a"), (1, 2, "b"), (2, 3, "c")])
    assert srt.count("\n\n") == 2
    assert "1\n" in srt and "3\n" in srt


# --- SRT parse/write round-trip ----------------------------------------------

def test_parse_and_write_round_trip():
    original = (
        "1\n00:00:01,000 --> 00:00:02,000\nHello there\n\n"
        "2\n00:00:03,500 --> 00:00:05,250\nSecond line\n"
    )
    blocks = P.parse_srt(original)
    assert len(blocks) == 2
    assert blocks[0][1] == "00:00:01,000 --> 00:00:02,000"
    assert blocks[1][2] == "Second line"
    rewritten = P.write_srt(blocks)
    assert P.parse_srt(rewritten) == blocks


def test_parse_srt_tolerates_missing_index():
    text = "00:00:01,000 --> 00:00:02,000\nNo index here\n"
    blocks = P.parse_srt(text)
    assert len(blocks) == 1
    assert blocks[0][2] == "No index here"


# --- ffmpeg command construction ----------------------------------------------

def test_extract_audio_cmd():
    cmd = P.extract_audio_cmd("ffmpeg", Path("v.mp4"), Path("a.wav"))
    assert cmd[0] == "ffmpeg"
    for flag, val in ("-ar", "16000"), ("-ac", "1"), ("-acodec", "pcm_s16le"):
        assert cmd[cmd.index(flag) + 1] == val


def test_burn_cmd_windows_path_escaping():
    srt = Path(r"C:\Users\she\Videos\my subs.srt")
    cmd = P.burn_cmd("ffmpeg", Path("v.mp4"), srt, Path("out.mp4"))
    vf = cmd[cmd.index("-vf") + 1]
    # drive colon escaped, backslashes converted, inner quote escaped
    assert "C\\:/Users/she/Videos/my subs.srt" in vf
    assert "force_style" in vf
    assert cmd[cmd.index("-c:a") + 1] == "copy"


def test_burn_cmd_regular_path():
    cmd = P.burn_cmd("ffmpeg", Path("v.mp4"), Path("sub.srt"), Path("out.mp4"))
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("subtitles='")


# --- stage skip/force behaviour ------------------------------------------------

def test_extract_audio_requires_video(tmp_path):
    with pytest.raises(FileNotFoundError):
        P.extract_audio(tmp_path, log=lambda m: None)


def test_burn_requires_srt(tmp_path):
    (tmp_path / "video.mp4").write_bytes(b"x")
    with pytest.raises(FileNotFoundError):
        P.burn_subtitles(tmp_path, log=lambda m: None)


def test_find_video_prefers_mp4(tmp_path):
    (tmp_path / "video.mkv").write_bytes(b"x")
    (tmp_path / "video.mp4").write_bytes(b"x")
    assert P.find_video(tmp_path) == tmp_path / "video.mp4"


# --- transcribe with mocked model ------------------------------------------------

class _FakeSeg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class _FakeModel:
    def transcribe(self, path, vad_filter=None):
        segs = [_FakeSeg(0.0, 1.25, " Hello"), _FakeSeg(1.5, 2.0, "world")]
        info = type("Info", (), {"language": "es", "language_probability": 0.99})()
        return iter(segs), info


def test_transcribe_audio_writes_files(tmp_path):
    (tmp_path / "audio.wav").write_bytes(b"x")
    srt = P.transcribe_audio(
        tmp_path, log=lambda m: None, model_factory=lambda size: _FakeModel()
    )
    assert srt == tmp_path / "transcript.srt"
    content = srt.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:01,250" in content
    assert "Hello" in content
    txt = (tmp_path / "transcript.txt").read_text(encoding="utf-8")
    assert txt == "Hello\nworld"


def test_transcribe_falls_back_to_cpu_when_cuda_libs_missing(tmp_path, monkeypatch):
    import faster_whisper

    class _NoCudaModel(_FakeModel):
        def transcribe(self, path, vad_filter=None):
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")

    devices = []

    def fake_whisper(size, device, compute_type):
        devices.append(device)
        return _NoCudaModel() if device == "auto" else _FakeModel()

    monkeypatch.setattr(faster_whisper, "WhisperModel", fake_whisper)
    (tmp_path / "audio.wav").write_bytes(b"x")
    srt = P.transcribe_audio(tmp_path, log=lambda m: None)
    assert devices == ["auto", "cpu"]
    assert "Hello" in srt.read_text(encoding="utf-8")


def test_transcribe_skips_when_srt_exists(tmp_path):
    (tmp_path / "audio.wav").write_bytes(b"x")
    (tmp_path / "transcript.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nold\n")
    called = []
    P.transcribe_audio(
        tmp_path,
        log=lambda m: None,
        model_factory=lambda size: called.append(1) or _FakeModel(),
    )
    assert called == []  # skipped, factory never invoked
    assert "old" in (tmp_path / "transcript.srt").read_text()


# --- translate with mocked translator ---------------------------------------------

class _UpperTranslator:
    def __init__(self, source, target):
        self.source, self.target = source, target

    def translate(self, text):
        return text.upper()


def test_translate_srt_preserves_timestamps(tmp_path):
    srt = tmp_path / "transcript.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nhola mundo\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nbuenos días\n",
        encoding="utf-8",
    )
    out = P.translate_srt_file(
        srt, "en", log=lambda m: None,
        translator_factory=lambda s, t: _UpperTranslator(s, t),
    )
    assert out == tmp_path / "transcript_translated.srt"
    blocks = P.parse_srt(out.read_text(encoding="utf-8"))
    assert blocks[0][2] == "HOLA MUNDO"
    assert blocks[1][2] == "BUENOS DÍAS"
    assert blocks[0][1] == "00:00:01,000 --> 00:00:02,000"  # untouched


def test_translate_uses_detected_language_as_source(tmp_path):
    srt = tmp_path / "transcript.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nbonjour\n", encoding="utf-8")
    (tmp_path / "detected_language.txt").write_text("fr", encoding="utf-8")
    seen = []

    def factory(source, target):
        seen.append((source, target))
        return _UpperTranslator(source, target)

    P.translate_srt_file(srt, "en", log=lambda m: None, translator_factory=factory)
    assert seen == [("fr", "en")]


def test_translate_keeps_text_when_already_target_language(tmp_path):
    srt = tmp_path / "transcript.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")
    (tmp_path / "detected_language.txt").write_text("en", encoding="utf-8")
    out = P.translate_srt_file(
        srt, "en", log=lambda m: None,
        translator_factory=lambda s, t: (_ for _ in ()).throw(AssertionError()),
    )
    assert P.parse_srt(out.read_text(encoding="utf-8"))[0][2] == "hello"


def test_translate_skips_existing(tmp_path):
    srt = tmp_path / "transcript.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhola\n")
    out = tmp_path / "transcript_translated.srt"
    out.write_text("existing\n")
    P.translate_srt_file(
        srt, "en", log=lambda m: None,
        translator_factory=lambda s, t: (_ for _ in ()).throw(AssertionError()),
    )
    assert out.read_text() == "existing\n"


def test_translate_fallback_when_separator_lost(tmp_path):
    class Mangler:
        def translate(self, text):
            return text.replace("\n@@@\n", " ")  # separator destroyed

    srt = tmp_path / "transcript.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nuno\n\n2\n00:00:03,000 --> 00:00:04,000\ndos\n"
    )
    out = P.translate_srt_file(
        srt, "en", log=lambda m: None,
        translator_factory=lambda s, t: Mangler(),
    )
    blocks = P.parse_srt(out.read_text())
    assert [b[2] for b in blocks] == ["uno", "dos"]  # per-cue fallback ran


# --- download with mocked yt-dlp ---------------------------------------------------

def test_find_wix_video_urls_orders_by_quality():
    html = (
        "src='https://video.wixstatic.com/video/827c6d_abc/480p/mp4/file.mp4' "
        "data='https://video.wixstatic.com/video/827c6d_abc/1080p/mp4/file.mp4' "
        "again='https://video.wixstatic.com/video/827c6d_abc/480p/mp4/file.mp4'"
    )
    urls = P.find_wix_video_urls("https://x.wixsite.com/y", html)
    assert urls[0].endswith("/1080p/mp4/file.mp4")
    assert len(urls) == 2  # deduped


def test_find_wix_video_urls_empty():
    assert P.find_wix_video_urls("https://x", "<html>no videos</html>") == []


class _FakeYDL:
    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def download(self, urls):
        import pathlib

        tmpl = pathlib.Path(self.options["outtmpl"])
        tmpl.with_suffix(".mp4").write_bytes(b"fake")


def test_download_video_writes_video(tmp_path, monkeypatch):
    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)
    video = P.download_video("https://x.wixsite.com/y/z", tmp_path, log=lambda m: None)
    assert video == tmp_path / "video.mp4"
    assert video.exists()


def test_download_skips_existing(tmp_path, monkeypatch):
    (tmp_path / "video.mp4").write_bytes(b"old")
    import yt_dlp

    def boom(options):
        raise AssertionError("should not be called")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", boom)
    video = P.download_video("https://x/y", tmp_path, log=lambda m: None)
    assert video.read_bytes() == b"old"


# --- orchestrator wiring ------------------------------------------------------------

def test_run_all_order(tmp_path, monkeypatch):
    calls = []

    monkeypatch.setattr(
        P, "download_video", lambda url, wd, log=None: calls.append("download")
        or (wd / "video.mp4").write_bytes(b"v")
        or wd / "video.mp4",
    )
    monkeypatch.setattr(
        P, "extract_audio",
        lambda wd, log=None: calls.append("extract")
        or (wd / "audio.wav").write_bytes(b"a")
        or wd / "audio.wav",
    )
    monkeypatch.setattr(
        P, "transcribe_audio",
        lambda wd, model_size="small", log=None: calls.append("transcribe")
        or (wd / "transcript.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n")
        or wd / "transcript.srt",
    )
    monkeypatch.setattr(
        P, "translate_srt_file",
        lambda srt, target_lang, source_lang="auto", log=None: calls.append("translate")
        or srt.with_name("transcript_translated.srt"),
    )
    monkeypatch.setattr(
        P, "burn_subtitles",
        lambda wd, style=None, log=None: calls.append("burn")
        or wd / f"final_{wd.name}.mp4",
    )

    out = P.run_all("https://s.wixsite.com/d/videos/my-dance", tmp_path)
    assert calls == ["download", "extract", "transcribe", "translate", "burn"]
    assert out == tmp_path / "my-dance" / "final_my-dance.mp4"
