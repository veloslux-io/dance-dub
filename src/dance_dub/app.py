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

from dance_dub import __version__, pipeline as P

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
                self._set(done=True, result=str(result), finished_at=time.time())
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

    return False, f"Unknown mode: {mode}"


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
        if self.path == "/api/start":
            ok, msg = handle_start(payload)
            self._json({"ok": ok, "message": msg})
        elif self.path == "/api/open":
            err = _open_folder(payload.get("path") or "")
            self._json({"ok": not err, "message": err})
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
  input[type=text], input[type=url] {
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
  footer { text-align: center; color: var(--muted); font-size: 11.5px; margin-top: 18px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">💃</div>
    <div>
      <h1>Dance Dub</h1>
      <div class="sub">Download a Wix video or pick a file — get it transcribed, translated &amp; subtitled.</div>
    </div>
  </header>

  <div id="banner" class="banner"></div>

  <div class="card">
    <div class="modes">
      <button id="m-url" class="active" onclick="setMode('url')">🔗&nbsp; From a Wix URL</button>
      <button id="m-file" onclick="setMode('file')">📁&nbsp; From a file on this PC</button>
    </div>

    <div id="url-pane">
      <label>Video page or file URL</label>
      <input type="url" id="url" placeholder="https://your-site.wixsite.com/mysite/video-page" spellcheck="false">
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

    <div class="stages" style="margin-top:14px">
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
  document.getElementById('m-url').classList.toggle('active', m === 'url');
  document.getElementById('m-file').classList.toggle('active', m === 'file');
  document.getElementById('url-pane').style.display = m === 'url' ? '' : 'none';
  document.getElementById('file-pane').style.display = m === 'file' ? '' : 'none';
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

async function start() {
  let body;
  if (MODE === 'url') {
    const url = document.getElementById('url').value.trim();
    if (!url) return banner('err', 'Paste a video URL first.');
    body = { mode: 'url', url };
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
      const dir = st.result ? st.result.replace(/[\\\\/][^\\\\/]+$/, '') : '';
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

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"Dance Dub v{__version__} — serving on {url}")
    if not os.environ.get("DANCE_DUB_NO_BROWSER"):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
