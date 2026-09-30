"""List a Wix site's videos through the Wix REST APIs (stdlib only).

Two sources, both returned as a flat list of `VideoItem`s whose `path` keeps
the site's own structure, so downloads land in matching folders:

- Media Manager: every video file, under the Media Manager folders it sits in.
- Online Programs: program → section → video step, in the program's order.

Auth is a site owner's API key (created in the Wix API Keys Manager) plus the
site ID; the key is sent as-is in the Authorization header.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, Iterator, List, Optional

API_BASE = "https://www.wixapis.com"
MEDIA_ROOT = "media-root"

# (method, url, headers, body bytes or None) -> parsed JSON dict
Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], dict]

_GUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_BAD_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class WixApiError(RuntimeError):
    pass


@dataclass
class VideoItem:
    id: str
    title: str
    path: List[str]
    source: str  # "media" or "programs"
    url: str = ""  # best direct MP4 we know of; may be upgraded at download
    file_id: str = ""  # Media Manager file ID, for an original-quality link
    size: int = 0
    downloadable: bool = True
    note: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def parse_site_id(text: str) -> str:
    """Accept a bare site ID or a pasted dashboard URL containing one."""
    m = _GUID_RE.search(text or "")
    return m.group(0).lower() if m else (text or "").strip()


def safe_name(text: str, fallback: str = "untitled", limit: int = 80) -> str:
    """A readable folder/file name that is valid on Windows, macOS and Linux."""
    name = _BAD_PATH_CHARS.sub(" ", text or "")
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    if name.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
        r"(?i)(COM|LPT)\d", name
    ):
        name = f"_{name}"
    return name[:limit].rstrip(". ") or fallback


def best_video_url(video: Optional[dict]) -> str:
    """Pick the highest-resolution MP4 from a VideoV2 object's resolutions."""
    if not video:
        return ""
    mp4s = [
        r for r in video.get("resolutions") or []
        if r.get("url") and "mp4" in str(r.get("format") or r.get("url")).lower()
    ]
    if not mp4s:
        return ""
    return max(mp4s, key=lambda r: int(r.get("height") or 0))["url"]


_LINK_RES = [
    re.compile(r"https?://video\.wixstatic\.com/video/[^\s\"'<>]+?\.mp4", re.I),
    re.compile(r"https?://[\w.-]*wixmp\.com/[^\s\"'<>]+?\.m3u8[^\s\"'<>]*", re.I),
]
_WV_ITEM_TAG = "_wv_itemId_"


def _wix_video_item_id(file_descriptor: dict) -> str:
    """Wix Video app item a Media Manager file belongs to, from the
    `_wv_itemId_<id>` internal tag Wix puts on files uploaded through it."""
    for tag in file_descriptor.get("internalTags") or []:
        if isinstance(tag, str) and tag.startswith(_WV_ITEM_TAG):
            return tag[len(_WV_ITEM_TAG):].lower()
    return ""


def _strings(obj) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def find_video_link(obj) -> str:
    """First direct video link anywhere in an API object (MP4, then HLS)."""
    texts = list(_strings(obj))
    for rx in _LINK_RES:
        for t in texts:
            m = rx.search(t)
            if m:
                return m.group(0)
    return ""


def _match_key(title: str) -> str:
    """Loose title key: case, extension, punctuation and spacing ignored."""
    title = re.sub(r"\.(mp4|mov|m4v|webm|mkv|avi)$", "", (title or "").strip(), flags=re.I)
    return re.sub(r"[\W_]+", " ", title).strip().lower()


def _numbered(i: int, total: int, title: str) -> str:
    width = max(2, len(str(total)))
    return f"{i:0{width}d} {safe_name(title)}"


def _urllib_transport(method: str, url: str, headers: Dict[str, str],
                      body: Optional[bytes]) -> dict:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code in (401, 403):
            raise WixApiError(
                f"Wix refused the request ({exc.code}). Check the API key, the "
                "site ID, and that the key has the permissions this needs "
                "(Media Manager / Online Programs)."
            ) from exc
        if exc.code == 404:
            raise WixApiError(
                f"Wix returned 404 for {urllib.parse.urlparse(url).path}. "
                "The site may not have this app installed."
            ) from exc
        raise WixApiError(f"Wix API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise WixApiError(f"Could not reach Wix: {exc.reason}") from exc
    return json.loads(raw or b"{}")


class WixClient:
    def __init__(self, api_key: str, site_id: str,
                 transport: Optional[Transport] = None) -> None:
        api_key = (api_key or "").strip()
        site_id = parse_site_id(site_id)
        if not api_key:
            raise WixApiError("Enter your Wix API key.")
        if not site_id:
            raise WixApiError("Enter your Wix site ID.")
        self.api_key = api_key
        self.site_id = site_id
        self.transport = transport or _urllib_transport

    def _call(self, method: str, path: str, params: Optional[dict] = None,
              body: Optional[dict] = None) -> dict:
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {
            "Authorization": self.api_key,
            "wix-site-id": self.site_id,
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        return self.transport(method, url, headers, data)

    # --- Media Manager -------------------------------------------------------

    def _paged_get(self, path: str, key: str, params: dict) -> Iterator[dict]:
        cursor = None
        while True:
            p = dict(params, **{"paging.limit": 100})
            if cursor:
                p["paging.cursor"] = cursor
            resp = self._call("GET", path, p)
            yield from resp.get(key) or []
            cursor = ((resp.get("nextCursor") or {}).get("cursors") or {}).get("next")
            if not cursor:
                return

    def list_media_videos(self, log: Callable[[str], None] = lambda m: None
                          ) -> List[VideoItem]:
        """Every video in the Media Manager, walking folders depth-first."""
        items: List[VideoItem] = []
        seen_folders = set()

        def walk(folder_id: str, path: List[str]) -> None:
            if folder_id in seen_folders:
                return
            seen_folders.add(folder_id)
            for f in self._paged_get(
                "/site-media/v1/files", "files",
                {"parentFolderId": folder_id, "mediaTypes": "VIDEO",
                 "sort.fieldName": "displayName", "sort.order": "ASC"},
            ):
                if f.get("state") == "DELETED":
                    continue
                video = (f.get("media") or {}).get("video") or {}
                ready = f.get("operationStatus") in (None, "READY")
                items.append(VideoItem(
                    id=f"media:{f.get('id')}",
                    title=f.get("displayName") or video.get("filename") or "video",
                    path=list(path),
                    source="media",
                    url=best_video_url(video),
                    file_id=f.get("id") or "",
                    size=int(f.get("sizeInBytes") or 0),
                    downloadable=ready,
                    note="" if ready else "Still processing in Wix",
                    extra={"wix_video_item": _wix_video_item_id(f)},
                ))
            for sub in self._paged_get(
                "/site-media/v1/folders", "folders",
                {"parentFolderId": folder_id,
                 "sort.fieldName": "displayName", "sort.order": "ASC"},
            ):
                if sub.get("state") == "DELETED" or not sub.get("id"):
                    continue
                name = safe_name(sub.get("displayName") or "folder")
                log(f"[wix] scanning folder {'/'.join(path + [name])}")
                walk(sub["id"], path + [name])

        log("[wix] scanning Media Manager")
        walk(MEDIA_ROOT, [])
        return items

    def download_url(self, item: VideoItem) -> str:
        """Best link to fetch: the original upload if Wix grants it, else the
        top resolution the listing gave us."""
        if item.file_id:
            try:
                resp = self._call(
                    "POST", "/site-media/v1/files/generate-file-download-url",
                    body={"fileId": item.file_id},
                )
                for entry in resp.get("downloadUrls") or []:
                    if entry.get("url"):
                        return entry["url"]
            except WixApiError:
                pass  # key may lack "Manage Media Manager"; use the listing URL
        return item.url

    # --- Online Programs -----------------------------------------------------

    def _query(self, path: str, key: str, query: dict, limit: int = 100,
               extra: Optional[dict] = None) -> Iterator[dict]:
        cursor = None
        while True:
            if cursor:  # later pages carry only the cursor, per Wix paging rules
                q = {"cursorPaging": {"limit": limit, "cursor": cursor}}
            else:
                q = dict(query, cursorPaging={"limit": limit})
            resp = self._call("POST", path, body=dict(extra or {}, query=q))
            yield from resp.get(key) or []
            cursor = ((resp.get("pagingMetadata") or {}).get("cursors") or {}).get("next")
            if not cursor:
                return

    def list_program_videos(self, log: Callable[[str], None] = lambda m: None
                            ) -> List[VideoItem]:
        """Every video step of every program: Program / NN Section / NN Step."""
        items: List[VideoItem] = []
        self.raw_steps: Dict[str, dict] = {}
        log("[wix] listing Online Programs")
        programs = list(self._query(
            "/online-programs/v3/programs/query", "programs",
            {"sort": [{"fieldName": "description.title", "order": "ASC"}]},
            limit=50,
        ))
        for program in programs:
            pid = program.get("id")
            if not pid or program.get("status") == "ARCHIVED":
                continue
            ptitle = safe_name((program.get("description") or {}).get("title") or "Program")
            log(f"[wix] reading program {ptitle}")
            steps = list(self._query(
                "/online-programs/v3/steps/query", "steps",
                {"filter": {"programId": {"$eq": pid}},
                 "sort": [{"fieldName": "ordering", "order": "ASC"}]},
                limit=500,
                extra={"programId": pid, "descriptionFieldSet": "EXTENDED"},
            ))
            sections: Dict[str, dict] = {}
            for sid in dict.fromkeys(s.get("sectionId") for s in steps):
                if not sid:
                    continue
                try:
                    sections[sid] = self._call(
                        "GET", f"/online-programs/v3/sections/{sid}"
                    ).get("section") or {}
                except WixApiError:
                    sections[sid] = {}
            ordered_sids = sorted(
                sections, key=lambda s: float(sections[s].get("ordering") or 0)
            )
            for si, sid in enumerate(ordered_sids, start=1):
                stitle = (sections[sid].get("description") or {}).get("title") or "Section"
                sfolder = _numbered(si, len(ordered_sids), stitle)
                in_section = sorted(
                    (s for s in steps if s.get("sectionId") == sid),
                    key=lambda s: float(s.get("ordering") or 0),
                )
                for ti, step in enumerate(in_section, start=1):
                    if step.get("stepType") != "VIDEO":
                        continue
                    opts = step.get("videoOptions") or {}
                    title = (step.get("description") or {}).get("title") or "Video"
                    item = VideoItem(
                        id=f"step:{step.get('id')}",
                        title=_numbered(ti, len(in_section), title),
                        path=[ptitle, sfolder],
                        source="programs",
                        extra={"programId": pid, "sectionId": sid},
                    )
                    media_video = opts.get("video")
                    if opts.get("deleted"):
                        item.downloadable, item.note = False, "Video was deleted from Media Manager"
                    elif media_video:
                        item.url = best_video_url(media_video)
                        item.file_id = media_video.get("id") or ""
                        if not item.url and not item.file_id:
                            item.downloadable, item.note = False, "No downloadable file"
                    elif opts.get("wixVideo"):
                        item.downloadable = False
                        item.note = "Wix Video app video; the API gives no file link"
                        item.extra.update(wix_video_id=opts["wixVideo"].get("id"),
                                          step_title=title)
                        self.raw_steps[item.id] = step
                    else:
                        item.downloadable, item.note = False, "No video attached"
                    items.append(item)
        self._resolve_wix_video_steps(items, log)
        return items

    def _resolve_wix_video_steps(self, items: List[VideoItem],
                                 log: Callable[[str], None]) -> None:
        """Wix publishes no API for the Wix Video app's library, so these
        steps carry only a Wix Video item ID. Files uploaded through Wix Video
        also live in the Media Manager, tagged with that item ID, so match on
        the tag; then fall back to a link in the step data, then a name match."""
        pending = [it for it in items if it.extra.get("wix_video_id")]
        if not pending:
            return
        log(f"[wix] {len(pending)} step(s) use Wix Video app videos; "
            "looking them up in the Media Manager")
        try:
            media = self.list_media_videos()
        except WixApiError as exc:
            log(f"[wix] could not read the Media Manager: {exc}")
            media = []
        self._media_cache = media
        by_item = {m.extra["wix_video_item"]: m for m in media
                   if m.downloadable and m.extra.get("wix_video_item")}
        for it in pending:
            m = by_item.get(str(it.extra["wix_video_id"]).lower())
            if m:
                it.url, it.file_id, it.size = m.url, m.file_id, m.size
                it.downloadable = True
                it.note = f"Wix Video app video, found in the Media library as {m.title}"
                continue
            link = find_video_link(self.raw_steps.get(it.id) or {})
            if link:
                it.url, it.downloadable = link, True
                it.extra["needs_ytdlp"] = link.lower().split("?")[0].endswith(".m3u8")
                it.note = "Wix Video app video, link found in the step data"
        self._match_wix_video_steps(items, log)

    def _match_wix_video_steps(self, items: List[VideoItem],
                               log: Callable[[str], None]) -> None:
        """Last resort for steps still without a file: a Media Manager video
        with the same name as the step, when exactly one has it."""
        pending = [it for it in items
                   if it.extra.get("wix_video_id") and not it.downloadable]
        if not pending:
            return
        media = getattr(self, "_media_cache", None) or []
        by_key: Dict[str, List[VideoItem]] = {}
        for m in media:
            if m.downloadable:
                by_key.setdefault(_match_key(m.title), []).append(m)
        for it in pending:
            matches = by_key.get(_match_key(it.extra["step_title"])) or []
            if len(matches) == 1:
                m = matches[0]
                it.url, it.file_id, it.size = m.url, m.file_id, m.size
                it.downloadable = True
                where = "/".join(m.path + [m.title])
                it.note = f"Wix Video app video, matched by name to Media library: {where}"
            elif len(matches) > 1:
                it.note = ("Wix Video app video; several Media library files "
                           "share its name, so none was picked")
            else:
                it.note = ("Wix Video app video; Wix gives no file link. "
                           "Paste the video's link to include it")
