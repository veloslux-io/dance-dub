"""Offline tests for the Wix API listing and the app's Wix batch mode."""

import json
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dance_dub import app as A  # noqa: E402
from dance_dub import pipeline as P  # noqa: E402
from dance_dub import wix_api as W  # noqa: E402

SITE = "3ecba886-4267-11ee-be56-0242ac120002"


def _video(height_urls):
    return {"resolutions": [
        {"url": u, "height": h, "format": f"{h}p.mp4"} for h, u in height_urls
    ] + [{"url": "https://x/hls.m3u8", "height": 2160, "format": "hls"}]}


class FakeWix:
    """Answers the handful of Wix endpoints the client calls."""

    def __init__(self):
        self.calls = []
        self.folders = {
            "media-root": [{"id": "f-classes", "displayName": "Classes", "state": "OK"}],
            "f-classes": [{"id": "f-salsa", "displayName": "Salsa: Level 1", "state": "OK"}],
            "f-salsa": [],
        }
        self.files = {
            "media-root": [{"id": "root.mp4", "displayName": "intro.mp4", "state": "OK",
                            "operationStatus": "READY", "sizeInBytes": "2000000",
                            "media": {"video": _video([(480, "https://v/intro-480.mp4"),
                                                       (1080, "https://v/intro-1080.mp4")])}}],
            "f-classes": [],
            "f-salsa": [
                {"id": "a.mp4", "displayName": "basic step.mp4", "state": "OK",
                 "operationStatus": "READY",
                 "media": {"video": _video([(720, "https://v/a-720.mp4")])}},
                {"id": "b.mp4", "displayName": "turns.mp4", "state": "OK",
                 "operationStatus": "PENDING", "media": {"video": {}}},
            ],
        }
        self.grant_original = True
        self.extra_step_fields = {}  # step ID -> fields merged into its step

    def __call__(self, method, url, headers, body):
        assert headers["Authorization"] == "KEY"
        assert headers["wix-site-id"] == SITE
        parsed = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        payload = json.loads(body) if body else None
        self.calls.append((method, parsed.path, q, payload))
        path = parsed.path
        if path == "/site-media/v1/folders":
            return {"folders": self.folders[q["parentFolderId"]]}
        if path == "/site-media/v1/files":
            assert q["mediaTypes"] == "VIDEO"
            files = self.files[q["parentFolderId"]]
            # Two-page response for the salsa folder to exercise paging.
            if q["parentFolderId"] == "f-salsa":
                if "paging.cursor" not in q:
                    return {"files": files[:1], "nextCursor": {"cursors": {"next": "c2"}}}
                return {"files": files[1:], "nextCursor": {"cursors": {"next": ""}}}
            return {"files": files}
        if path == "/site-media/v1/files/generate-file-download-url":
            if not self.grant_original:
                raise W.WixApiError("403")
            return {"downloadUrls": [{"url": f"https://orig/{payload['fileId']}",
                                      "assetKey": "src"}]}
        if path == "/online-programs/v3/programs/query":
            return {"programs": [
                {"id": "p1", "status": "PUBLISHED", "description": {"title": "Bachata 101"}},
                {"id": "p2", "status": "ARCHIVED", "description": {"title": "Old"}},
            ]}
        if path == "/online-programs/v3/steps/query":
            assert payload["query"]["filter"] == {"programId": {"$eq": "p1"}}
            return {"steps": [dict(st, **self.extra_step_fields.get(st["id"], {})) for st in [
                {"id": "s3", "sectionId": "sec2", "ordering": 10, "stepType": "VIDEO",
                 "description": {"title": "Footwork"},
                 "videoOptions": {"video": {"id": "m3", **_video([(720, "https://v/s3.mp4")])}}},
                {"id": "s1", "sectionId": "sec1", "ordering": 10, "stepType": "VIDEO",
                 "description": {"title": "Welcome"},
                 "videoOptions": {"video": {"id": "m1", **_video([(1080, "https://v/s1.mp4")])}}},
                {"id": "s2", "sectionId": "sec1", "ordering": 20, "stepType": "ARTICLE",
                 "description": {"title": "Reading"}},
                {"id": "s4", "sectionId": "sec1", "ordering": 30, "stepType": "VIDEO",
                 "description": {"title": "Guest class"},
                 "videoOptions": {"wixVideo": {"id": "wv1"}}},
            ]]}
        if path.startswith("/online-programs/v3/sections/"):
            sid = path.rsplit("/", 1)[-1]
            return {"section": {
                "sec1": {"id": "sec1", "ordering": 10, "description": {"title": "Week 1"}},
                "sec2": {"id": "sec2", "ordering": 20, "description": {"title": "Week 2"}},
            }[sid]}
        raise AssertionError(f"unexpected call {method} {url}")


@pytest.fixture
def fake():
    return FakeWix()


@pytest.fixture
def client(fake):
    return W.WixClient("KEY", SITE, transport=fake)


# --- helpers -----------------------------------------------------------------

def test_parse_site_id_from_dashboard_url():
    url = f"https://manage.wix.com/dashboard/{SITE.upper()}/home"
    assert W.parse_site_id(url) == SITE


def test_safe_name_windows_invalid():
    assert W.safe_name('Salsa: Level 1 / "Basics"?') == "Salsa Level 1 Basics"
    assert W.safe_name("CON") == "_CON"
    assert W.safe_name("...") == "untitled"


def test_best_video_url_prefers_tallest_mp4():
    assert W.best_video_url(_video([(480, "a"), (1080, "b"), (720, "c")])) == "b"
    assert W.best_video_url({"resolutions": [{"url": "h", "format": "hls"}]}) == ""


def test_client_requires_key_and_site():
    with pytest.raises(W.WixApiError):
        W.WixClient("", SITE)
    with pytest.raises(W.WixApiError):
        W.WixClient("KEY", "")


# --- Media Manager -------------------------------------------------------------

def test_media_listing_walks_folders_and_pages(client):
    items = client.list_media_videos()
    by_title = {it.title: it for it in items}
    assert set(by_title) == {"intro.mp4", "basic step.mp4", "turns.mp4"}
    assert by_title["intro.mp4"].path == []
    assert by_title["intro.mp4"].url == "https://v/intro-1080.mp4"
    assert by_title["intro.mp4"].size == 2000000
    assert by_title["basic step.mp4"].path == ["Classes", "Salsa Level 1"]
    assert by_title["turns.mp4"].downloadable is False


def test_download_url_prefers_original_then_listing(client, fake):
    item = next(it for it in client.list_media_videos() if it.title == "intro.mp4")
    assert client.download_url(item) == "https://orig/root.mp4"
    fake.grant_original = False
    assert client.download_url(item) == "https://v/intro-1080.mp4"


# --- Online Programs ---------------------------------------------------------------

def test_program_listing_keeps_structure_and_order(client):
    items = client.list_program_videos()
    got = [(it.path, it.title, it.downloadable) for it in items]
    assert got == [
        (["Bachata 101", "01 Week 1"], "01 Welcome", True),
        (["Bachata 101", "01 Week 1"], "03 Guest class", False),
        (["Bachata 101", "02 Week 2"], "01 Footwork", True),
    ]
    assert items[0].url == "https://v/s1.mp4"
    assert "Wix Video" in items[1].note


# --- app: listing endpoint and batch job ---------------------------------------------

def test_handle_wix_list_reports_errors():
    out = A.handle_wix_list({"api_key": "", "site_id": SITE})
    assert out["ok"] is False and "API key" in out["message"]


def test_handle_start_wix_requires_fresh_listing(monkeypatch):
    A.WIX_ITEMS.clear()
    ok, msg = A.handle_start({"mode": "wix", "process_ids": ["media:x"], "api_key": "K",
                              "site_id": SITE})
    assert not ok and "Load videos" in msg


def test_run_wix_batch_mirrors_folders(tmp_path, client, monkeypatch):
    items = [it for it in client.list_program_videos() if it.downloadable]
    fetched = []

    def fake_download(url, workdir, log=None, force=False):
        fetched.append(url)
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "video.mp4").write_bytes(b"v")
        return workdir / "video.mp4"

    monkeypatch.setattr(P, "download_file_url", fake_download)
    monkeypatch.setattr(
        P, "run_pipeline",
        lambda wd, target_lang, model_size, log: wd / f"final_{wd.name}.mp4",
    )
    logs = []
    jobs = [(it, "process") for it in items]
    result = A.run_wix_batch(client, jobs, tmp_path, "en", "small", logs.append)
    assert (tmp_path / "Bachata 101" / "01 Week 1" / "01 Welcome" / "video.mp4").exists()
    assert (tmp_path / "Bachata 101" / "02 Week 2" / "01 Footwork" / "video.mp4").exists()
    assert fetched == ["https://orig/m1", "https://orig/m3"]
    assert result["folder"] == str(tmp_path)
    assert "2 of 2" in result["result"]
    assert any(m.startswith("[item] 1/2 Process:") for m in logs)


def test_run_wix_batch_continues_after_a_failure(tmp_path, client, monkeypatch):
    items = [it for it in client.list_program_videos() if it.downloadable]

    def flaky(url, workdir, log=None, force=False):
        if "m1" in url:
            raise RuntimeError("network down")
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir / "video.mp4"

    monkeypatch.setattr(P, "download_file_url", flaky)
    monkeypatch.setattr(P, "run_pipeline", lambda wd, **kw: wd / "final.mp4")
    jobs = [(it, "process") for it in items]
    result = A.run_wix_batch(client, jobs, tmp_path, "en", "small", lambda m: None)
    assert "1 of 2" in result["result"] and "01 Welcome" in result["result"]


def test_media_workdir_drops_extension(tmp_path, client):
    item = next(it for it in client.list_media_videos() if it.title == "basic step.mp4")
    assert A.wix_workdir(tmp_path, item) == tmp_path / "Classes" / "Salsa Level 1" / "basic step"


def test_download_file_url_rejects_tiny_files(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "_download_direct", lambda url, dest: dest.write_bytes(b"x"))
    with pytest.raises(RuntimeError):
        P.download_file_url("https://x/v.mp4", tmp_path, log=lambda m: None)
    assert not (tmp_path / "video.mp4").exists()


def test_wix_video_step_matched_to_media_file_by_name(client, fake):
    fake.files["f-classes"] = [{
        "id": "guest.mp4", "displayName": "Guest_Class.MP4", "state": "OK",
        "operationStatus": "READY", "media": {"video": _video([(720, "https://v/g.mp4")])},
    }]
    guest = next(it for it in client.list_program_videos() if "Guest" in it.title)
    assert guest.downloadable is True
    assert guest.url == "https://v/g.mp4" and guest.file_id == "guest.mp4"
    assert "Classes/Guest_Class.MP4" in guest.note
    assert guest.path == ["Bachata 101", "01 Week 1"]  # keeps the program's structure


def test_wix_video_step_not_matched_when_name_is_ambiguous(client, fake):
    dup = {"state": "OK", "operationStatus": "READY",
           "media": {"video": _video([(720, "https://v/x.mp4")])}}
    fake.files["f-classes"] = [dict(dup, id="1.mp4", displayName="guest class.mp4"),
                               dict(dup, id="2.mov", displayName="Guest class.mov")]
    guest = next(it for it in client.list_program_videos() if "Guest" in it.title)
    assert guest.downloadable is False and "several" in guest.note


def test_steps_query_asks_for_extended_fields(client, fake):
    client.list_program_videos()
    steps_call = next(c for c in fake.calls if c[1] == "/online-programs/v3/steps/query")
    assert steps_call[3]["descriptionFieldSet"] == "EXTENDED"


def _guest(client):
    return next(it for it in client.list_program_videos() if "Guest" in it.title)


def test_wix_video_step_found_by_media_manager_tag(client, fake):
    # Files uploaded through Wix Video carry a _wv_itemId_<item id> tag.
    fake.files["f-classes"] = [{
        "id": "ed6f86_acd.mp4", "displayName": "217490.mp4", "state": "OK",
        "operationStatus": "READY", "sizeInBytes": "9008932",
        "internalTags": ["_wv_itemId_WV1", "_wv_videoContent"],
        "media": {"video": _video([(1080, "https://v/217490-1080.mp4")])},
    }]
    guest = _guest(client)
    assert guest.downloadable is True
    assert guest.url == "https://v/217490-1080.mp4" and guest.file_id == "ed6f86_acd.mp4"
    assert guest.size == 9008932 and "217490.mp4" in guest.note
    assert guest.path == ["Bachata 101", "01 Week 1"]


def test_wix_video_link_found_in_step_data(client, fake):
    fake.extra_step_fields["s4"] = {"description": {"title": "Guest class", "details":
        '{"src":"https://repackager.wixmp.com/video.wixstatic.com/video/ab_1/,720p,/mp4/file.mp4.urlset/master.m3u8"}'}}
    guest = _guest(client)
    assert guest.downloadable is True
    assert guest.url.endswith("master.m3u8") and guest.extra["needs_ytdlp"] is True


def test_unresolved_steps_offer_paste_and_save_debug(client, fake, monkeypatch, tmp_path):
    monkeypatch.setattr(A, "APP_DIR", tmp_path)
    monkeypatch.setattr(W, "_urllib_transport", fake)
    out = A.handle_wix_list({"api_key": "KEY", "site_id": SITE, "source": "programs"})
    guest = next(i for i in out["items"] if "Guest" in i["title"])
    assert guest["downloadable"] is False and "Paste" in guest["note"]
    saved = json.loads(Path(out["debug_file"]).read_text())
    assert list(saved) == [guest["id"]]


def test_pasted_link_is_used_with_ytdlp(client, fake, monkeypatch, tmp_path):
    monkeypatch.setattr(W, "_urllib_transport", fake)
    A.handle_wix_list({"api_key": "KEY", "site_id": SITE, "source": "programs"})
    guest = next(i for i, it in A.WIX_ITEMS.items() if "Guest" in it.title)
    started = {}
    monkeypatch.setattr(A.JM, "start", lambda kind, label, runner: started.update(r=runner) or True)
    ok, msg = A.handle_start({"mode": "wix", "process_ids": [guest], "api_key": "KEY",
                              "site_id": SITE, "output": str(tmp_path),
                              "links": {guest: " https://site.com/v.m3u8 "}})
    assert ok, msg
    seen = []
    monkeypatch.setattr(P, "download_video", lambda url, wd, log=None: seen.append(url))
    monkeypatch.setattr(P, "download_file_url", lambda *a, **k: pytest.fail("wrong downloader"))
    monkeypatch.setattr(P, "run_pipeline", lambda wd, **kw: wd / "final.mp4")
    started["r"](lambda m: None)
    assert seen == ["https://site.com/v.m3u8"]


# --- disk state and download/process jobs ------------------------------------------

def _listed(fake, monkeypatch, tmp_path, source="programs"):
    monkeypatch.setattr(W, "_urllib_transport", fake)
    monkeypatch.setattr(A, "APP_DIR", tmp_path / "app")
    out = A.handle_wix_list({"api_key": "KEY", "site_id": SITE, "source": source,
                             "output": str(tmp_path)})
    return {i["title"]: i for i in out["items"]}


def test_state_follows_the_output_folder(fake, monkeypatch, tmp_path):
    items = _listed(fake, monkeypatch, tmp_path)
    welcome = items["01 Welcome"]
    assert welcome["state"] == {"downloaded": False, "processed": False}
    wd = tmp_path / "Bachata 101" / "01 Week 1" / "01 Welcome"
    wd.mkdir(parents=True)
    (wd / "video.mp4").write_bytes(b"v")
    states = A.handle_wix_state({"output": str(tmp_path)})["states"]
    assert states[welcome["id"]] == {"downloaded": True, "processed": False}
    (wd / "final_01 Welcome.mp4").write_bytes(b"f")
    assert A.handle_wix_state({"output": str(tmp_path)})["states"][welcome["id"]]["processed"]
    (wd / "video.mp4").unlink()  # deleted outside the app: not downloaded any more
    assert A.handle_wix_state({"output": str(tmp_path)})["states"][welcome["id"]] == {
        "downloaded": False, "processed": False}


def _start(monkeypatch, tmp_path, **ids):
    started = {}
    monkeypatch.setattr(A.JM, "start",
                        lambda kind, label, runner: started.update(r=runner) or True)
    ok, msg = A.handle_start(dict({"mode": "wix", "api_key": "KEY", "site_id": SITE,
                                   "output": str(tmp_path)}, **ids))
    return ok, msg, started.get("r")


def test_download_only_skips_the_pipeline(fake, monkeypatch, tmp_path):
    items = _listed(fake, monkeypatch, tmp_path)
    ok, msg, runner = _start(monkeypatch, tmp_path,
                             download_ids=[items["01 Welcome"]["id"]])
    assert ok, msg
    got = []
    monkeypatch.setattr(P, "download_file_url",
                        lambda url, wd, log=None, force=False: got.append(wd.name))
    monkeypatch.setattr(P, "run_pipeline", lambda *a, **k: pytest.fail("processed"))
    result = runner(lambda m: None)
    assert got == ["01 Welcome"] and "1 downloaded" in result["result"]


def test_process_wins_over_download_and_runs_everything(fake, monkeypatch, tmp_path):
    items = _listed(fake, monkeypatch, tmp_path)
    wid, fid = items["01 Welcome"]["id"], items["01 Footwork"]["id"]
    ok, msg, runner = _start(monkeypatch, tmp_path, download_ids=[wid, fid],
                             process_ids=[wid])
    assert ok, msg
    ran = []
    monkeypatch.setattr(P, "download_file_url", lambda url, wd, log=None, force=False: None)
    monkeypatch.setattr(P, "run_pipeline", lambda wd, **k: ran.append(wd.name))
    result = runner(lambda m: None)
    assert ran == ["01 Welcome"]
    assert "1 processed, 1 downloaded" in result["result"]


def test_download_of_a_file_already_on_disk_is_skipped(fake, monkeypatch, tmp_path):
    items = _listed(fake, monkeypatch, tmp_path)
    wd = tmp_path / "Bachata 101" / "01 Week 1" / "01 Welcome"
    wd.mkdir(parents=True)
    (wd / "video.mp4").write_bytes(b"v")
    ok, msg, _ = _start(monkeypatch, tmp_path, download_ids=[items["01 Welcome"]["id"]])
    assert not ok and "None of the ticked" in msg


def test_video_on_disk_can_be_processed_without_a_wix_link(fake, monkeypatch, tmp_path):
    items = _listed(fake, monkeypatch, tmp_path)
    guest = items["03 Guest class"]
    assert guest["downloadable"] is False
    wd = tmp_path / "Bachata 101" / "01 Week 1" / "03 Guest class"
    wd.mkdir(parents=True)
    (wd / "video.mp4").write_bytes(b"v")
    ok, msg, runner = _start(monkeypatch, tmp_path, process_ids=[guest["id"]])
    assert ok, msg
    monkeypatch.setattr(P, "run_pipeline", lambda wd, **k: None)
    assert "1 of 1" in runner(lambda m: None)["result"]
