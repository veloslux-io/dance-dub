"""Offline tests for the app's subtitle editor endpoints."""

import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dance_dub import app as A  # noqa: E402
from dance_dub import pipeline as P  # noqa: E402


@pytest.fixture
def workdir(tmp_path):
    wd = tmp_path / "Videos" / "Course" / "lesson"
    wd.mkdir(parents=True)
    (wd / "video.mp4").write_bytes(bytes(range(256)) * 4)
    (wd / "transcript.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhola\n",
                                       encoding="utf-8")
    return wd


def test_subs_list_load_save(workdir):
    out = workdir.parents[1]
    listing = A.handle_subs_list({"output": str(out)})
    assert listing["folders"] == [{"folder": str(workdir), "name": "Course/lesson",
                                   "translated": False, "final": False}]
    got = A.handle_subs_load({"folder": str(workdir), "track": "original"})
    assert got["cues"] == [{"start": "00:00:00,000", "end": "00:00:01,000", "text": "hola"}]
    assert not A.handle_subs_load({"folder": str(workdir), "track": "translated"})["ok"]
    saved = A.handle_subs_save({"folder": str(workdir), "track": "original",
                                "cues": [dict(got["cues"][0], text="hola amigos")]})
    assert saved["ok"]
    assert "hola amigos" in (workdir / "transcript.srt").read_text(encoding="utf-8")
    bad = A.handle_subs_save({"folder": str(workdir), "track": "original",
                              "cues": [{"start": "x", "end": "0:01", "text": "a"}]})
    assert not bad["ok"] and "Line 1" in bad["message"]


def test_subs_rejects_folders_that_are_not_work_folders(tmp_path):
    assert not A.handle_subs_load({"folder": str(tmp_path), "track": "original"})["ok"]
    assert not A.handle_subs_load({"folder": "", "track": "original"})["ok"]
    ok, _ = A.handle_subs_remake({"folder": str(tmp_path)})
    assert not ok


def test_remake_burns_again_with_chosen_settings(workdir, monkeypatch):
    seen = {}

    def fake_burn(wd, log=None, force=False, quality="standard", subs="auto"):
        seen.update(wd=wd, force=force, quality=quality, subs=subs)
        return wd / "final.mp4"

    monkeypatch.setattr(P, "burn_subtitles", fake_burn)
    ok, msg = A.handle_subs_remake({"folder": str(workdir), "subs": "original",
                                    "quality": "original"})
    assert ok, msg
    for _ in range(200):
        if not A.JM.snapshot()["status"]["running"]:
            break
        threading.Event().wait(0.01)
    assert seen == {"wd": workdir, "force": True, "quality": "original", "subs": "original"}


def test_quality_payload_is_checked():
    assert A._quality({"quality": "original"}) == "original"
    assert A._quality({"quality": "ultra"}) == "standard"
    assert A._quality({}) == "standard"


def test_media_serves_ranges(workdir):
    A.Handler.protocol_version = "HTTP/1.1"
    server = ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        from urllib.parse import quote
        url = f"http://127.0.0.1:{server.server_port}/api/media?folder={quote(str(workdir))}"
        data = (workdir / "video.mp4").read_bytes()
        with urllib.request.urlopen(url) as r:
            assert r.status == 200 and r.read() == data
            assert r.headers["Content-Type"] == "video/mp4"
        req = urllib.request.Request(url, headers={"Range": "bytes=10-19"})
        with urllib.request.urlopen(req) as r:
            assert r.status == 206 and r.read() == data[10:20]
            assert r.headers["Content-Range"] == f"bytes 10-19/{len(data)}"
        req = urllib.request.Request(url, headers={"Range": "bytes=-4"})
        with urllib.request.urlopen(req) as r:
            assert r.read() == data[-4:]
        bad = url.replace(quote(str(workdir)), quote(str(workdir.parent)))
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(bad)
    finally:
        server.shutdown()


def test_retranslate_keeps_a_backup_and_forces(workdir, monkeypatch):
    translated = workdir / "transcript_translated.srt"
    translated.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
    seen = {}

    def fake_translate(srt, target_lang, log=None, force=False):
        seen.update(srt=srt, target=target_lang, force=force)
        translated.write_text("new", encoding="utf-8")
        return translated

    monkeypatch.setattr(P, "translate_srt_file", fake_translate)
    ok, msg = A.handle_subs_retranslate({"folder": str(workdir), "target_lang": "fr"})
    assert ok, msg
    for _ in range(200):
        if not A.JM.snapshot()["status"]["running"]:
            break
        threading.Event().wait(0.01)
    assert seen == {"srt": workdir / "transcript.srt", "target": "fr", "force": True}
    assert "hello" in (workdir / "transcript_translated.srt.bak").read_text(encoding="utf-8")
