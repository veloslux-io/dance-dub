"""Dance Dub app — local web UI (stdlib only) around the pipeline.

Run `python -m dance_dub.app` (or the packaged exe): starts a localhost server
and auto-opens the browser. No dependencies beyond the pipeline's own.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dance_dub import __version__, pipeline as P, wix_api as W

MAX_LOG_LINES = 500

STAGE_ORDER = ["download", "extract", "transcribe", "translate", "burn"]
STAGE_PREFIX = {s: f"[{s}]" for s in STAGE_ORDER}


class JobManager:
    """Runs one pipeline job at a time on a worker thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.status: dict = self._fresh_status()
        self.log_lines: list[str] = []

    @staticmethod
    def _fresh_status() -> dict:
        return {
            "running": False, "stage": None, "error": None, "done": False,
            "result": None, "started_at": None, "finished_at": None,
            "kind": None, "label": None, "stages_done": [],
            "folder": None, "item": None,
        }

    def _set(self, **kw) -> None:
        with self.lock:
            self.status.update(kw)

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        with self.lock:
            self.log_lines.append(line)
            if len(self.log_lines) > MAX_LOG_LINES:
                del self.log_lines[: len(self.log_lines) - MAX_LOG_LINES]

    def start(self, kind: str, label: str, runner) -> bool:
        """Start a job if none running. `runner(log_fn)` does the work."""
        with self.lock:
            if self.status["running"]:
                return False
            self.status = self._fresh_status()
            self.status.update(
                running=True, started_at=time.time(), kind=kind, label=label
            )
            self.log_lines = []
        self.log(f"▶ Started: {kind} — {label}")

        def log_fn(msg: str) -> None:
            self.log(msg)
            if msg.startswith("[item]"):  # next video of a batch: reset stages
                self._set(stage=None, stages_done=[], item=msg[6:].strip())
                return
            for stage in STAGE_ORDER:
                if msg.startswith(STAGE_PREFIX[stage]):
                    with self.lock:
                        done = list(self.status.get("stages_done") or [])
                        if stage not in done:
                            done.append(stage)
                        self.status["stage"] = stage
                        self.status["stages_done"] = done
                    break

        def worker() -> None:
            try:
                result = runner(log_fn)
                folder = None
                if isinstance(result, dict):  # batch jobs: {"result", "folder"}
                    result, folder = result["result"], result.get("folder")
                self._set(done=True, result=str(result), folder=folder,
                          finished_at=time.time())
                self.log(f"✔ Finished — {result}")
            except Exception as exc:
                self._set(error=str(exc), finished_at=time.time())
                self.log(f"✖ ERROR: {exc}")
            finally:
                self._set(running=False, stage=None)

        threading.Thread(target=worker, daemon=True).start()
        return True

    def snapshot(self) -> dict:
        with self.lock:
            return {"status": dict(self.status), "log": list(self.log_lines)}


JM = JobManager()

APP_DIR = Path.home() / "DanceDub"


def _open_folder(path: str) -> str:
    """Open a folder in the OS file explorer. Returns an error message or ''."""
    folder = Path(path)
    if not folder.is_dir():
        return f"Not a folder: {folder}"
    if sys.platform == "win32":
        os.startfile(folder)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])
    return ""


# Last Wix listing, by item ID: /api/start gets IDs back, not client-sent URLs.
WIX_ITEMS: dict[str, W.VideoItem] = {}


def handle_wix_list(payload: dict) -> dict:
    """POST /api/wix/list — {api_key, site_id, source: media|programs}."""
    source = payload.get("source") or "media"
    try:
        client = W.WixClient(payload.get("api_key") or "", payload.get("site_id") or "")
        if source == "programs":
            items = client.list_program_videos()
        else:
            items = client.list_media_videos()
    except W.WixApiError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:  # malformed responses, network oddities
        return {"ok": False, "message": f"Could not list videos: {exc}"}
    WIX_ITEMS.clear()
    WIX_ITEMS.update({it.id: it for it in items})
    out = {"ok": True, "source": source, "items": [it.to_dict() for it in items]}
    unresolved = {i: s for i, s in getattr(client, "raw_steps", {}).items()
                  if not WIX_ITEMS[i].downloadable}
    if unresolved:
        out["debug_file"] = _save_wix_debug(unresolved)
    return out


def _save_wix_debug(raw_steps: dict) -> str:
    """Keep what Wix returned for steps with no link (no key or site ID in it),
    so it can be checked for a link Dance Dub doesn't know about yet."""
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        path = APP_DIR / "wix-video-steps.json"
        path.write_text(json.dumps(raw_steps, indent=2), encoding="utf-8")
        return str(path)
    except OSError:
        return ""


def wix_workdir(out_root: Path, item: W.VideoItem) -> Path:
    """Where a Wix video's files go: its Wix folders, then one per video."""
    # Media Manager names keep their extension ("warmup.mp4"); drop it.
    name = Path(item.title).stem if item.source == "media" else item.title
    return out_root.joinpath(*item.path, W.safe_name(name))


def run_wix_batch(client: W.WixClient, items: list, out_root: Path,
                  target: str, model: str, log_fn) -> dict:
    """Download and process each selected video in turn; one failure doesn't
    stop the rest."""
    done, failed = [], []
    total = len(items)
    for i, item in enumerate(items, start=1):
        workdir = wix_workdir(out_root, item)
        log_fn(f"[item] {i}/{total} {'/'.join(item.path + [item.title])}")
        try:
            if item.extra.get("needs_ytdlp") and not P.find_video(workdir):
                # A pasted link or an HLS stream: yt-dlp (with the page-scrape
                # fallback) handles pages, playlists and plain files alike.
                P.download_video(item.url, workdir, log=log_fn)
            else:
                url = "" if P.find_video(workdir) else client.download_url(item)
                P.download_file_url(url, workdir, log=log_fn)
            done.append(P.run_pipeline(
                workdir, target_lang=target, model_size=model, log=log_fn
            ))
        except Exception as exc:
            failed.append(item.title)
            log_fn(f"✖ {item.title}: {exc}")
    if not done:
        raise RuntimeError(
            f"None of the {total} video(s) finished. See the progress log."
        )
    summary = f"{len(done)} of {total} video(s) subtitled in {out_root}"
    if failed:
        summary += f" — failed: {', '.join(failed)}"
    return {"result": summary, "folder": str(out_root)}


def handle_start(payload: dict) -> tuple[bool, str]:
    """POST /api/start — {mode: url|file, ...opts}. Returns (ok, message)."""
    mode = payload.get("mode")
    out_root = Path(payload.get("output") or (APP_DIR / "Videos"))
    out_root = Path(os.path.expanduser(str(out_root)))
    model = payload.get("model") or "small"
    target = payload.get("target_lang") or "en"

    if mode == "url":
        url = (payload.get("url") or "").strip()
        if not url:
            return False, "Paste a video URL first."
        workdir = out_root / P.slugify(url)

        def runner(log_fn):
            workdir.mkdir(parents=True, exist_ok=True)
            P.download_video(url, workdir, log=log_fn)
            return P.run_pipeline(
                workdir, target_lang=target, model_size=model, log=log_fn
            )

        started = JM.start(f"Wix URL — {url}", workdir.name, runner)
        return started, "" if started else "A job is already running."

    if mode == "file":
        raw = payload.get("path") or ""
        path = Path(os.path.expanduser(raw))
        if not raw:
            return False, "Choose a video file first."
        if not path.is_file():
            return False, f"File not found: {path}"

        def runner(log_fn):
            return P.start_from_file(
                path, out_root, target_lang=target, model_size=model, log=log_fn
            )

        started = JM.start(f"Local file — {path.name}", path.name, runner)
        return started, "" if started else "A job is already running."

    if mode == "wix":
        ids = payload.get("ids") or []
        items = [WIX_ITEMS[i] for i in ids if i in WIX_ITEMS]
        if not ids:
            return False, "Tick at least one video first."
        if len(items) != len(ids):
            return False, "The video list is out of date. Press Load videos again."
        for i, link in (payload.get("links") or {}).items():
            link = (link or "").strip()
            if i in WIX_ITEMS and link.lower().startswith(("http://", "https://")):
                it = WIX_ITEMS[i]
                it.url, it.file_id, it.downloadable = link, "", True
                it.extra["needs_ytdlp"] = True
                it.note = "Using the link you pasted"
        items = [it for it in items if it.downloadable]
        if not items:
            return False, "None of the ticked videos can be downloaded."
        try:
            client = W.WixClient(payload.get("api_key") or "",
                                 payload.get("site_id") or "")
        except W.WixApiError as exc:
            return False, str(exc)

        def runner(log_fn):
            return run_wix_batch(client, items, out_root, target, model, log_fn)

        label = f"{len(items)} video(s)"
        started = JM.start("Wix site", label, runner)
        return started, "" if started else "A job is already running."

    return False, f"Unknown mode: {mode}"


def _quit() -> None:
    """Stop the app. The packaged exe has no window, so this (the page's Quit
    button) is how users end it; os._exit also stops a job mid-run."""
    time.sleep(0.3)  # let the /api/quit response reach the browser
    os._exit(0)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # silence default request logging
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send(200, build_index().encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._json(JM.snapshot())
        elif self.path == "/api/defaults":
            self._json({"output": str(APP_DIR / "Videos"), "version": __version__})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if self.path.startswith("/api/upload"):
            self._handle_upload()
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, 400)
            return
        if self.path == "/api/wix/list":
            self._json(handle_wix_list(payload))
        elif self.path == "/api/start":
            ok, msg = handle_start(payload)
            self._json({"ok": ok, "message": msg})
        elif self.path == "/api/open":
            err = _open_folder(payload.get("path") or "")
            self._json({"ok": not err, "message": err})
        elif self.path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=_quit, daemon=True).start()
        else:
            self._json({"error": "not found"}, 404)

    def _handle_upload(self) -> None:
        """Stream an uploaded video to APP_DIR/Uploads (browsers hide real paths)."""
        from urllib.parse import urlparse, parse_qs, unquote

        qs = parse_qs(urlparse(self.path).query)
        raw_name = unquote((qs.get("name") or ["video.mp4"])[0])
        safe = P.slugify(Path(raw_name).stem or "upload") + (
            Path(raw_name).suffix.lower() or ".mp4"
        )
        dest_dir = APP_DIR / "Uploads"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe
        n = 1
        while dest.exists():
            dest = dest_dir / f"{dest.stem}-{n}{dest.suffix}"
            n += 1
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._json({"error": "empty upload"}, 400)
            return
        remaining = length
        with open(dest, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if (dest.stat().st_size or 0) == 0:
            dest.unlink(missing_ok=True)
            self._json({"error": "upload failed"}, 500)
            return
        self._json({"ok": True, "path": str(dest), "size": dest.stat().st_size})


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dance Dub</title>
<style>
  :root {
    --bg: #0f1115; --panel: #171a21; --panel2: #1e222b; --line: #2a2f3a;
    --text: #e8eaf0; --muted: #9aa3b2; --accent: #7c5cff; --accent2: #4f8cff;
    --ok: #34c98e; --err: #ff6b6b; --warn: #f5b95f;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, "Segoe UI", Roboto, Inter, sans-serif;
  }
  .wrap { max-width: 760px; margin: 0 auto; padding: 28px 20px 60px; }
  header { display: flex; align-items: center; gap: 12px; margin-bottom: 22px; }
  .logo {
    width: 38px; height: 38px; border-radius: 10px; flex: none;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    display: flex; align-items: center; justify-content: center; font-size: 19px;
  }
  h1 { font-size: 19px; margin: 0; font-weight: 650; }
  .sub { color: var(--muted); font-size: 12.5px; }
  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 14px; padding: 18px; margin-bottom: 16px;
  }
  .modes { display: flex; gap: 8px; margin-bottom: 14px; }
  .modes button {
    flex: 1; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--line);
    background: var(--panel2); color: var(--muted); font-size: 13.5px;
    font-weight: 550; cursor: pointer; transition: all .15s;
  }
  .modes button.active {
    color: var(--text); border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 14%, var(--panel2));
  }
  label { display: block; font-size: 12.5px; color: var(--muted); margin: 10px 0 5px; }
  input[type=text], input[type=url], input[type=password] {
    width: 100%; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--line);
    background: var(--panel2); color: var(--text); font-size: 14px; outline: none;
  }
  input:focus { border-color: var(--accent); }
  .row { display: flex; gap: 10px; }
  .row > * { flex: 1; }
  select {
    width: 100%; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--line);
    background: var(--panel2); color: var(--text); font-size: 13.5px; outline: none;
  }
  .drop {
    border: 1.5px dashed var(--line); border-radius: 12px; padding: 26px 16px;
    text-align: center; color: var(--muted); cursor: pointer; transition: all .15s;
  }
  .drop:hover, .drop.over { border-color: var(--accent); color: var(--text);
    background: color-mix(in srgb, var(--accent) 7%, transparent); }
  .drop .big { font-size: 15px; color: var(--text); font-weight: 550; }
  .drop .path { margin-top: 6px; font-size: 12.5px; color: var(--accent); word-break: break-all; }
  .go {
    width: 100%; margin-top: 16px; padding: 13px; border: none; border-radius: 11px;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    color: #fff; font-size: 15px; font-weight: 650; cursor: pointer;
  }
  .go:disabled { opacity: .45; cursor: default; }
  .stages { display: flex; gap: 6px; margin-top: 4px; }
  .stage {
    flex: 1; text-align: center; padding: 8px 4px; border-radius: 9px;
    background: var(--panel2); border: 1px solid var(--line);
    font-size: 11.5px; color: var(--muted); transition: all .2s;
  }
  .stage .n { font-size: 15px; display:block; margin-bottom: 2px; }
  .stage.active { border-color: var(--accent); color: var(--text);
    background: color-mix(in srgb, var(--accent) 16%, var(--panel2)); }
  .stage.done { border-color: color-mix(in srgb, var(--ok) 60%, var(--line)); color: var(--ok); }
  #log {
    background: #0b0d11; border: 1px solid var(--line); border-radius: 10px;
    padding: 12px; height: 240px; overflow-y: auto;
    font: 12px/1.6 ui-monospace, "Cascadia Mono", Menlo, Consolas, monospace;
    color: #b7c0cf; white-space: pre-wrap; word-break: break-word;
  }
  .banner { padding: 10px 14px; border-radius: 10px; margin-bottom: 12px; font-size: 13.5px; display: none; }
  .banner.ok { display: block; background: color-mix(in srgb, var(--ok) 14%, transparent);
    border: 1px solid var(--ok); color: var(--ok); }
  .banner.err { display: block; background: color-mix(in srgb, var(--err) 12%, transparent);
    border: 1px solid var(--err); color: var(--err); }
  .banner .open-btn {
    float: right; background: none; border: 1px solid currentColor; color: inherit;
    border-radius: 7px; padding: 2px 10px; cursor: pointer; font-size: 12px;
  }
  header .quit {
    margin-left: auto; padding: 7px 14px; border-radius: 9px; cursor: pointer;
    border: 1px solid var(--line); background: var(--panel2); color: var(--muted);
    font-size: 12.5px;
  }
  header .quit:hover { color: var(--text); border-color: var(--err); }
  .hint { color: var(--muted); font-size: 12px; margin-top: 5px; }
  .hint a { color: var(--accent2); }
  .src { display: flex; gap: 8px; margin-top: 12px; }
  .src button, .small-btn {
    padding: 8px 12px; border-radius: 9px; border: 1px solid var(--line);
    background: var(--panel2); color: var(--muted); font-size: 13px; cursor: pointer;
  }
  .src button { flex: 1; }
  .src button.active { color: var(--text); border-color: var(--accent2); }
  .small-btn:hover { color: var(--text); }
  .small-btn:disabled { opacity: .45; cursor: default; }
  .remember { display: flex; align-items: center; gap: 6px; color: var(--muted);
    font-size: 12.5px; margin: 8px 0 0; }
  .remember input { margin: 0; }
  .tree-bar { display: flex; align-items: center; gap: 8px; margin-top: 12px; }
  .tree-bar .count { margin-left: auto; color: var(--muted); font-size: 12.5px; }
  #tree {
    margin-top: 8px; max-height: 360px; overflow-y: auto; border: 1px solid var(--line);
    border-radius: 10px; background: #0b0d11; padding: 6px 4px; display: none;
  }
  #tree details { margin-left: 14px; }
  #tree > details, #tree > .vid { margin-left: 2px; }
  #tree summary { cursor: pointer; padding: 3px 4px; color: var(--text); list-style: none; }
  #tree summary::before { content: "▸"; display: inline-block; width: 14px; color: var(--muted); }
  #tree details[open] > summary::before { content: "▾"; }
  #tree summary .n { color: var(--muted); font-size: 12px; margin-left: 4px; }
  #tree .vid { display: flex; align-items: center; gap: 6px; padding: 3px 4px 3px 32px;
    color: #c9d1dd; font-size: 13px; }
  #tree details > .vid { padding-left: 18px; }
  #tree .vid.off { color: var(--muted); }
  #tree .vid .note { color: var(--warn); font-size: 11.5px; }
  #tree .vid .link-btn { padding: 2px 8px; font-size: 11.5px; border-radius: 7px; }
  #tree .vid .size { color: var(--muted); font-size: 11.5px; margin-left: auto; }
  #tree input[type=checkbox] { accent-color: var(--accent); }
  .now { color: var(--muted); font-size: 12.5px; margin-top: 8px; min-height: 18px; }
  footer { text-align: center; color: var(--muted); font-size: 11.5px; margin-top: 18px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">💃</div>
    <div>
      <h1>Dance Dub</h1>
      <div class="sub">Download Wix videos or pick a file — get them transcribed, translated &amp; subtitled.</div>
    </div>
    <button class="quit" onclick="quitApp()" title="Stop Dance Dub running in the background">Quit</button>
  </header>

  <div id="banner" class="banner"></div>

  <div class="card">
    <div class="modes">
      <button id="m-url" class="active" onclick="setMode('url')">🔗&nbsp; From a Wix URL</button>
      <button id="m-wix" onclick="setMode('wix')">🔑&nbsp; From my Wix site</button>
      <button id="m-file" onclick="setMode('file')">📁&nbsp; From a file on this PC</button>
    </div>

    <div id="url-pane">
      <label>Video page or file URL</label>
      <input type="url" id="url" placeholder="https://your-site.wixsite.com/mysite/video-page" spellcheck="false">
    </div>

    <div id="wix-pane" style="display:none">
      <div class="row">
        <div>
          <label>Wix API key</label>
          <input type="password" id="wixKey" spellcheck="false" autocomplete="off" placeholder="Paste your API key">
        </div>
        <div>
          <label>Site ID</label>
          <input type="text" id="wixSite" spellcheck="false" placeholder="Site ID or your dashboard URL">
        </div>
      </div>
      <div class="hint">
        Create a key at <a href="https://manage.wix.com/account/api-keys" target="_blank" rel="noopener">Wix API Keys</a>
        with the Media Manager and Online Programs permissions. The site ID is the long code after
        <code>/dashboard/</code> in your site's dashboard address; pasting that address works too.
      </div>
      <label class="remember"><input type="checkbox" id="wixRemember" checked> Remember on this computer</label>
      <div class="src">
        <button id="src-media" class="active" onclick="setSource('media')">🎞&nbsp; Media library</button>
        <button id="src-programs" onclick="setSource('programs')">🎓&nbsp; Online Programs</button>
      </div>
      <div class="tree-bar">
        <button class="small-btn" id="wixLoad" onclick="loadWix()">Load videos</button>
        <button class="small-btn" onclick="tickAll(true)">Select all</button>
        <button class="small-btn" onclick="tickAll(false)">Select none</button>
        <span class="count" id="wixCount"></span>
      </div>
      <div id="tree"></div>
    </div>

    <div id="file-pane" style="display:none">
      <label>Video file</label>
      <div class="drop" id="drop">
        <span class="big">Drop a video here</span> or click to browse
        <div class="path" id="filePath"></div>
      </div>
      <input type="file" id="fileInput" accept=".mp4,.mov,.mkv,.webm,.m4v" style="display:none">
    </div>

    <div class="row" style="margin-top:6px">
      <div>
        <label>Translate to</label>
        <select id="target">
          <option value="en" selected>English</option>
          <option value="es">Spanish</option><option value="fr">French</option>
          <option value="de">German</option><option value="it">Italian</option>
          <option value="pt">Portuguese</option><option value="zh-CN">Chinese</option>
          <option value="ja">Japanese</option><option value="ko">Korean</option>
          <option value="ar">Arabic</option><option value="hi">Hindi</option>
          <option value="ru">Russian</option>
        </select>
      </div>
      <div>
        <label>Accuracy</label>
        <select id="model">
          <option value="tiny">Fastest (draft)</option>
          <option value="base">Fast</option>
          <option value="small" selected>Balanced ✓</option>
          <option value="medium">Best (slow)</option>
        </select>
      </div>
      <div>
        <label>Save to</label>
        <input type="text" id="output" spellcheck="false" placeholder="DanceDub folder (set automatically)">
      </div>
    </div>

    <button class="go" id="go" onclick="start()">✨&nbsp; Start</button>

    <div class="now" id="now"></div>
    <div class="stages" style="margin-top:6px">
      <div class="stage" id="st-download"><span class="n">⬇</span>Download</div>
      <div class="stage" id="st-extract"><span class="n">🎧</span>Audio</div>
      <div class="stage" id="st-transcribe"><span class="n">✍️</span>Transcribe</div>
      <div class="stage" id="st-translate"><span class="n">🌐</span>Translate</div>
      <div class="stage" id="st-burn"><span class="n">🎬</span>Subtitles</div>
    </div>
  </div>

  <div class="card">
    <label style="margin-top:0">Progress</label>
    <div id="log">Waiting for a job…</div>
  </div>

  <footer>Dance Dub v__VERSION__ — runs entirely on this computer. First transcription downloads the AI model once (~460&nbsp;MB).</footer>
</div>

<script>
let MODE = 'url', PICKED = null, TIMER = null;

async function init() {
  try {
    const r = await fetch('/api/defaults');
    const j = await r.json();
    if (j.output && !document.getElementById('output').value)
      document.getElementById('output').value = j.output;
  } catch (e) {}
}
init();

function setMode(m) {
  MODE = m;
  ['url', 'wix', 'file'].forEach(k => {
    document.getElementById('m-' + k).classList.toggle('active', m === k);
    document.getElementById(k + '-pane').style.display = m === k ? '' : 'none';
  });
}

// --- Wix site (API) -------------------------------------------------------
let SOURCE = 'media', WIX_ITEMS = [];
const LS_KEY = 'dancedub.wix';

function lsGet() { try { return JSON.parse(localStorage.getItem(LS_KEY) || '{}'); } catch (e) { return {}; } }
function lsSet(v) { try { localStorage.setItem(LS_KEY, JSON.stringify(v)); } catch (e) {} }
(function restoreWix() {
  const saved = lsGet();
  if (saved.key) document.getElementById('wixKey').value = saved.key;
  if (saved.site) document.getElementById('wixSite').value = saved.site;
})();
function wixCreds() {
  const key = document.getElementById('wixKey').value.trim();
  const site = document.getElementById('wixSite').value.trim();
  if (document.getElementById('wixRemember').checked) lsSet({ key, site });
  else lsSet({});
  return { api_key: key, site_id: site };
}

function setSource(s) {
  SOURCE = s;
  document.getElementById('src-media').classList.toggle('active', s === 'media');
  document.getElementById('src-programs').classList.toggle('active', s === 'programs');
  WIX_ITEMS = [];
  renderTree();
}

async function loadWix() {
  const creds = wixCreds();
  if (!creds.api_key) return banner('err', 'Paste your Wix API key first.');
  if (!creds.site_id) return banner('err', 'Enter your Wix site ID first.');
  const btn = document.getElementById('wixLoad');
  btn.disabled = true; btn.textContent = 'Loading…';
  banner('', '');
  try {
    const r = await fetch('/api/wix/list', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ source: SOURCE }, creds))
    });
    const j = await r.json();
    if (!j.ok) { WIX_ITEMS = []; banner('err', j.message || 'Could not load videos.'); }
    else {
      WIX_ITEMS = j.items; LINKS = {};
      if (!WIX_ITEMS.length) banner('err', SOURCE === 'media'
        ? 'No videos found in the Media library.' : 'No video steps found in your Online Programs.');
    }
  } catch (e) { banner('err', 'Could not load videos: ' + e); }
  btn.disabled = false; btn.textContent = 'Reload videos';
  renderTree();
}

function fmtSize(n) {
  if (!n) return '';
  return n > 1e9 ? (n / 1e9).toFixed(1) + ' GB' : Math.max(1, Math.round(n / 1e6)) + ' MB';
}

function renderTree() {
  const tree = document.getElementById('tree');
  tree.innerHTML = '';
  tree.style.display = WIX_ITEMS.length ? 'block' : 'none';
  // Build nested folders from each item's path.
  const root = { dirs: new Map(), vids: [] };
  WIX_ITEMS.forEach(it => {
    let node = root;
    it.path.forEach(p => {
      if (!node.dirs.has(p)) node.dirs.set(p, { dirs: new Map(), vids: [] });
      node = node.dirs.get(p);
    });
    node.vids.push(it);
  });
  const countVids = n => n.vids.length + [...n.dirs.values()].reduce((a, d) => a + countVids(d), 0);
  function build(node, parent, depth) {
    node.dirs.forEach((child, name) => {
      const det = document.createElement('details');
      if (depth < 2) det.open = true;
      const sum = document.createElement('summary');
      const cb = document.createElement('input');
      cb.type = 'checkbox'; cb.className = 'grp';
      cb.onclick = e => {
        e.stopPropagation();
        det.querySelectorAll('input.vid-cb:not(:disabled)').forEach(x => x.checked = cb.checked);
        syncGroups();
      };
      sum.appendChild(cb);
      sum.appendChild(document.createTextNode(' 📂 ' + name));
      const n = document.createElement('span'); n.className = 'n';
      n.textContent = '(' + countVids(child) + ')';
      sum.appendChild(n);
      det.appendChild(sum);
      build(child, det, depth + 1);
      parent.appendChild(det);
    });
    node.vids.forEach(it => {
      const row = document.createElement('label');
      row.className = 'vid' + (it.downloadable ? '' : ' off');
      const cb = document.createElement('input');
      cb.type = 'checkbox'; cb.className = 'vid-cb'; cb.value = it.id;
      cb.disabled = !it.downloadable; cb.onchange = syncGroups;
      row.appendChild(cb);
      row.appendChild(document.createTextNode('🎬 ' + it.title));
      if (it.note) {
        const note = document.createElement('span'); note.className = 'note';
        note.textContent = '— ' + it.note; row.appendChild(note);
      }
      if (!it.downloadable && it.extra && it.extra.wix_video_id) {
        const btn = document.createElement('button');
        btn.type = 'button'; btn.className = 'small-btn link-btn';
        btn.textContent = LINKS[it.id] ? 'Change link' : 'Paste link';
        btn.onclick = e => { e.preventDefault(); pasteLink(it, cb, btn); };
        row.appendChild(btn);
        if (LINKS[it.id]) { cb.disabled = false; row.classList.remove('off'); }
      }
      const size = document.createElement('span'); size.className = 'size';
      size.textContent = fmtSize(it.size); row.appendChild(size);
      parent.appendChild(row);
    });
  }
  build(root, tree, 0);
  syncGroups();
}

let LINKS = {};
function pasteLink(it, cb, btn) {
  const link = prompt(
    'Paste a link to "' + it.title + '".\\n\\n' +
    'To find it: open this step on your live site, press F12, go to the Network tab, ' +
    'play the video, type "wixstatic" or "m3u8" in the filter box, then right-click the ' +
    'request and copy its URL. A link to the page the video plays on also works if the page is public.',
    LINKS[it.id] || '');
  if (link === null) return;
  const v = link.trim();
  if (v && !/^https?:\/\//i.test(v)) return banner('err', 'That is not a web link (it should start with https://).');
  if (v) { LINKS[it.id] = v; cb.disabled = false; cb.checked = true; }
  else { delete LINKS[it.id]; cb.checked = false; cb.disabled = true; }
  cb.closest('.vid').classList.toggle('off', !v);
  btn.textContent = v ? 'Change link' : 'Paste link';
  syncGroups();
}

function syncGroups() {
  document.querySelectorAll('#tree details').forEach(det => {
    const boxes = [...det.querySelectorAll('input.vid-cb:not(:disabled)')];
    const on = boxes.filter(b => b.checked).length;
    const g = det.querySelector(':scope > summary > input.grp');
    g.checked = boxes.length > 0 && on === boxes.length;
    g.indeterminate = on > 0 && on < boxes.length;
    g.disabled = boxes.length === 0;
  });
  const total = WIX_ITEMS.length, picked = selectedIds().length;
  document.getElementById('wixCount').textContent =
    total ? picked + ' of ' + total + ' selected' : '';
}

function selectedIds() {
  return [...document.querySelectorAll('#tree input.vid-cb:checked')].map(b => b.value);
}

function tickAll(on) {
  document.querySelectorAll('#tree input.vid-cb:not(:disabled)').forEach(b => b.checked = on);
  syncGroups();
}

const drop = document.getElementById('drop'), fi = document.getElementById('fileInput');
drop.onclick = () => fi.click();
fi.onchange = () => { if (fi.files.length) pick(fi.files[0]); };
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => {
  e.preventDefault(); drop.classList.remove('over');
  if (e.dataTransfer.files.length) pick(e.dataTransfer.files[0]);
};
function pick(f) {
  if (!/\.(mp4|mov|mkv|webm|m4v)$/i.test(f.name))
    return banner('err', 'That doesn\\'t look like a video file (want .mp4, .mov, .mkv, .webm or .m4v).');
  PICKED = { name: f.name, path: null };
  document.getElementById('filePath').textContent = f.name + ' — uploading…';
  fetch('/api/upload?name=' + encodeURIComponent(f.name), { method: 'POST', body: f })
    .then(r => r.json())
    .then(j => {
      if (j.ok) {
        PICKED.path = j.path;
        document.getElementById('filePath').textContent = f.name + ' ✓ ready';
      } else {
        PICKED = null;
        document.getElementById('filePath').textContent = '';
        banner('err', 'Upload failed: ' + (j.error || 'unknown error'));
      }
    })
    .catch(e => {
      PICKED = null;
      banner('err', 'Upload failed: ' + e);
    });
}

async function quitApp() {
  let running = false;
  try { running = (await (await fetch('/api/status')).json()).status.running; } catch (e) {}
  const msg = running
    ? 'A video is still being processed. Quit anyway? The job will stop.'
    : 'Quit Dance Dub? It stops running in the background.';
  if (!confirm(msg)) return;
  if (TIMER) clearInterval(TIMER);
  try { await fetch('/api/quit', { method: 'POST', body: '{}' }); } catch (e) {}
  document.querySelector('.wrap').innerHTML =
    '<header><div class="logo">💃</div><div><h1>Dance Dub has closed</h1>' +
    '<div class="sub">You can close this tab. Double-click DanceDub.exe to start it again.</div></div></header>';
}

async function start() {
  let body;
  if (MODE === 'url') {
    const url = document.getElementById('url').value.trim();
    if (!url) return banner('err', 'Paste a video URL first.');
    body = { mode: 'url', url };
  } else if (MODE === 'wix') {
    const ids = selectedIds();
    if (!ids.length) return banner('err', 'Load your videos and tick the ones you want.');
    body = Object.assign({ mode: 'wix', ids, links: LINKS }, wixCreds());
  } else {
    if (!PICKED || !PICKED.path) return banner('err', 'Choose or drop a video file first.');
    body = { mode: 'file', path: PICKED.path };
  }
  body.target_lang = document.getElementById('target').value;
  body.model = document.getElementById('model').value;
  body.output = document.getElementById('output').value;

  const r = await fetch('/api/start', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  const j = await r.json();
  if (!j.ok) return banner('err', j.message || 'Could not start.');
  banner('', '');
  document.getElementById('go').disabled = true;
  ['download','extract','transcribe','translate','burn'].forEach(resetStage);
  if (TIMER) clearInterval(TIMER);
  poll();
  TIMER = setInterval(poll, 700);
}

function resetStage(s) {
  const el = document.getElementById('st-' + s);
  el.classList.remove('active', 'done');
}

async function poll() {
  const r = await fetch('/api/status');
  const j = await r.json();
  const st = j.status;

  const log = document.getElementById('log');
  log.textContent = j.log.length ? j.log.join('\\n') : 'Waiting…';
  log.scrollTop = log.scrollHeight;

  document.getElementById('now').textContent =
    st.running && st.item ? 'Now: ' + st.item : '';

  const order = ['download','extract','transcribe','translate','burn'];
  const doneIdx = order.indexOf(st.stages_done ? st.stages_done[st.stages_done.length-1] : null);
  order.forEach((s, i) => {
    const el = document.getElementById('st-' + s);
    el.classList.toggle('done', i <= doneIdx && !!(st.stages_done && st.stages_done.length));
    el.classList.toggle('active', st.running && s === st.stage);
  });

  if (!st.running && TIMER) {
    clearInterval(TIMER); TIMER = null;
    document.getElementById('go').disabled = false;
    if (st.error) banner('err', st.error);
    else if (st.done) {
      const dir = st.folder ? st.folder : st.result ? st.result.replace(/[\\\\/][^\\\\/]+$/, '') : '';
      banner('ok', '✔ Done! ' + st.result, dir);
    }
  }
}

function banner(kind, msg, dir) {
  const b = document.getElementById('banner');
  b.className = 'banner ' + kind;
  b.innerHTML = '';
  if (msg) {
    const span = document.createElement('span');
    span.textContent = msg;
    b.appendChild(span);
  }
  if (kind === 'ok' && dir) {
    const btn = document.createElement('button');
    btn.className = 'open-btn';
    btn.textContent = '📁 Open folder';
    btn.onclick = () => fetch('/api/open', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: dir })
    });
    b.appendChild(btn);
    b.appendChild(document.createElement('div')).style.clear = 'both';
  }
  return false;
}

</script>
</body>
</html>"""


def build_index() -> str:
    return INDEX_HTML.replace("__VERSION__", __version__)


def main() -> None:
    # Overridable so CI can test the exe without clashing with a copy the
    # user already has open, and without popping browser tabs.
    port = int(os.environ.get("DANCE_DUB_PORT") or 8756)
    Handler.protocol_version = "HTTP/1.1"

    url = f"http://127.0.0.1:{port}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        # Port taken: most likely Dance Dub is already running (the exe has
        # no window, so people double-click it again). Show that copy.
        print(f"Port {port} is in use; opening the running copy at {url}")
        if not os.environ.get("DANCE_DUB_NO_BROWSER"):
            webbrowser.open(url)
        return
    print(f"Dance Dub v{__version__} — serving on {url}")
    if not os.environ.get("DANCE_DUB_NO_BROWSER"):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
