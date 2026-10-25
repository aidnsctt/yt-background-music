"""YouTube sources and an atomically saved, local favorites library."""

import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import parse_qs, urlparse

STREAM_URL = "https://www.youtube.com/watch?v=rFZHOHl-L8A"
FALLBACK_STREAM_URL = "https://www.youtube.com/@LofiGirl/live"
DEFAULT_SOURCE = {"url": STREAM_URL, "title": "Lofi Girl — beats to relax/study to"}
LIBRARY_PATH = Path.home() / "Library/Application Support/Lofi Player/favorites.json"


def normalize_url(value):
    """Accept individual YouTube videos and explicit channel live endpoints only.

    Strip playlist/tracking/timestamp parameters; favorites identify a video,
    rather than a particular share link. Never pass arbitrary URLs to yt-dlp.
    """
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
        raise ValueError("Paste a full YouTube video or live stream URL.")
    if parsed.netloc.lower() not in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"):
        raise ValueError("Use a youtube.com or youtu.be link.")
    parts = parsed.path.strip("/").split("/")
    video_id = None
    if parsed.hostname == "youtu.be" and len(parts) == 1:
        video_id = parts[0]
    elif parsed.hostname != "youtu.be":
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        elif len(parts) == 2 and parts[0] in ("live", "shorts", "embed"):
            video_id = parts[1]
        elif (len(parts) == 2 and re.fullmatch(r"@[\w.-]+", parts[0]) and parts[1] == "live") or (
            len(parts) == 3 and parts[0] in ("channel", "c", "user")
            and re.fullmatch(r"[\w.-]+", parts[1]) and parts[2] == "live"
        ):
            return "https://www.youtube.com/" + "/".join(parts)
    if not video_id or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Use a video or live stream link, rather than a playlist or channel page.")
    return "https://www.youtube.com/watch?v=" + video_id


class Library:
    def __init__(self, path=LIBRARY_PATH):
        self.path = Path(path)
        self.items = []
        self.error = None
        try:
            data = json.loads(self.path.read_text())
            if data.get("version") != 1 or not isinstance(data.get("favorites"), list):
                raise ValueError("Unsupported favorites file")
            seen = {STREAM_URL}
            for entry in data["favorites"]:
                url = normalize_url(entry["url"])
                title = entry["title"].strip()
                if not title:
                    raise ValueError("A favorite has no name")
                if url not in seen:
                    self.items.append({"url": url, "title": title[:200]})
                    seen.add(url)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.items = []
            self.error = f"Could not read {self.path}: {exc}. The original file has been left untouched."

    def save(self, items):
        # A broken/unsupported file must never be overwritten with an empty list.
        if self.error:
            raise OSError(self.error)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as handle:
                temporary = handle.name
                json.dump({"version": 1, "favorites": items}, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        self.items = [dict(item) for item in items]

    def contains(self, url):
        return url == STREAM_URL or any(item["url"] == url for item in self.items)

    def add(self, source):
        if not self.contains(source["url"]):
            self.save(self.items + [{"url": source["url"], "title": source["title"]}])
