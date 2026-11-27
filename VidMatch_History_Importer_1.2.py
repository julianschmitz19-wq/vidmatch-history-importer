#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VidMatch History Importer
=========================
Rekonstruiert aus einem Google-Takeout-YouTube-Wiedergabeverlauf eine
VidMatch-kompatible Import-JSON.

Prinzip:
- Google liefert Video-ID, Kanal-ID und Startzeitpunkt.
- yt-dlp ergänzt Videolänge, @Handle und Kategorie.
- Watchtime = volle Videolänge, außer das nächste Video startet vorher.
- Bestehende VidMatch-Messwerte werden niemals überschrieben.

Benötigt:
    pip install yt-dlp

BeautifulSoup ist NICHT erforderlich; der Takeout-Parser ist absichtlich
streamlined/regex-basiert, weil große Wiedergabeverlauf.html-Dateien damit
wesentlich schneller verarbeitet werden können.
"""

from __future__ import annotations

import copy
import ctypes
import html
import json
import os
import queue
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

APP_NAME = "VidMatch History Importer"
APP_VERSION = "1.2"
LOCAL_TZ_NAME = "Europe/Berlin"

def fixed_cet_tz(is_summer: bool):
    return timezone(timedelta(hours=2 if is_summer else 1))

def central_europe_tz_for_date(dt: datetime):
    """CET/CEST ohne externe tzdata-Abhängigkeit (wichtig unter Windows)."""
    def last_sunday(year: int, month: int) -> int:
        if month == 12:
            first_next = datetime(year + 1, 1, 1)
        else:
            first_next = datetime(year, month + 1, 1)
        last_day = first_next - timedelta(days=1)
        return last_day.day - ((last_day.weekday() + 1) % 7)

    start = datetime(dt.year, 3, last_sunday(dt.year, 3), 2, 0, 0)
    end = datetime(dt.year, 10, last_sunday(dt.year, 10), 3, 0, 0)
    naive = dt.replace(tzinfo=None)
    return fixed_cet_tz(start <= naive < end)

CARD_MARKER = '<div class="outer-cell'
VIDEO_RE = re.compile(
    r'<a\s+href="https://www\.youtube\.com/watch\?v=([^"&]+)[^"]*">(.*?)</a>\s*(?:&nbsp;|\u00a0| )*angesehen',
    re.IGNORECASE | re.DOTALL,
)
CHANNEL_RE = re.compile(
    r'<a\s+href="https://www\.youtube\.com/channel/(UC[^"/?#]+)[^"]*">(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
DATE_RE = re.compile(
    r'(\d{2}\.\d{2}\.\d{4}),\s*(\d{2}:\d{2}:\d{2})\s*(MESZ|MEZ)',
    re.IGNORECASE,
)
HANDLE_URL_RE = re.compile(r'youtube\.com/@([^/?#]+)', re.IGNORECASE)



DARK_COLORS = {
    "bg": "#15171a", "panel": "#1d2024", "panel2": "#24282d", "field": "#101215",
    "border": "#3a4148", "fg": "#f1f3f5", "muted": "#a6adb4", "accent": "#6f42c1",
    "accent_hover": "#8357d4", "selection": "#4f3b78", "progress": "#7b61a8",
}
LIGHT_COLORS = {
    "bg": "#f3f4f6", "panel": "#ffffff", "panel2": "#e9ebef", "field": "#ffffff",
    "border": "#c7cbd1", "fg": "#15171a", "muted": "#616872", "accent": "#6f42c1",
    "accent_hover": "#5d36a8", "selection": "#d9cef2", "progress": "#6f42c1",
}

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
FALLBACK_CATEGORY_NAMES = {
    "1": "Film & Animation", "2": "Autos & Vehicles", "10": "Music", "15": "Pets & Animals",
    "17": "Sports", "18": "Short Movies", "19": "Travel & Events", "20": "Gaming",
    "21": "Videoblogging", "22": "People & Blogs", "23": "Comedy", "24": "Entertainment",
    "25": "News & Politics", "26": "Howto & Style", "27": "Education",
    "28": "Science & Technology", "29": "Nonprofits & Activism",
}
ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def enable_windows_dark_titlebar(window, enabled: bool) -> None:
    if os.name != "nt":
        return
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if enabled else 0)
        for attribute in (20, 19):
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
                )
                break
            except Exception:
                continue
    except Exception:
        pass


def parse_iso8601_duration(value: str) -> Optional[int]:
    m = ISO_DURATION_RE.match(str(value or "").strip())
    if not m:
        return None
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    seconds = int(m.group("seconds") or 0)
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return max(1, total) if total > 0 else None


def chunks_of(values: List[str], size: int = 50) -> Iterable[List[str]]:
    for i in range(0, len(values), size):
        yield values[i:i + size]


def normalize_api_handle(custom_url: str) -> str:
    value = unquote(str(custom_url or "").strip())
    if not value:
        return ""
    value = value.rsplit("/", 1)[-1]
    if not value:
        return ""
    return value if value.startswith("@") else "@" + value


def youtube_api_get(endpoint: str, params: Dict[str, Any], api_key: str,
                    cancel: threading.Event, stats: BuildStats,
                    log, max_retries: int = 5) -> Dict[str, Any]:
    query = dict(params)
    query["key"] = api_key
    url = f"{YOUTUBE_API_BASE}/{endpoint}?{urlencode(query)}"
    delay = 1.0
    last_error = ""
    for attempt in range(max_retries + 1):
        if cancel.is_set():
            raise Cancelled()
        try:
            stats.api_requests += 1
            req = Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
            with urlopen(req, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            if not isinstance(payload, dict):
                raise RuntimeError("YouTube API lieferte kein JSON-Objekt.")
            return payload
        except HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            last_error = f"HTTP {exc.code}: {body[:500]}"
            retryable_403 = exc.code == 403 and any(
                token in body for token in ("rateLimitExceeded", "userRateLimitExceeded")
            )
            if attempt < max_retries and (exc.code in (429, 500, 502, 503, 504) or retryable_403):
                log(f"API temporär blockiert ({exc.code}); Retry in {delay:.0f}s …")
                time.sleep(delay)
                delay = min(delay * 2, 16)
                continue
            raise RuntimeError(f"YouTube Data API Fehler: {last_error}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
            if attempt < max_retries:
                log(f"API-Netzwerkfehler; Retry in {delay:.0f}s …")
                time.sleep(delay)
                delay = min(delay * 2, 16)
                continue
            raise RuntimeError(f"YouTube Data API Netzwerkfehler: {last_error}") from exc
    raise RuntimeError(f"YouTube Data API Fehler: {last_error}")


@dataclass
class HistoryEvent:
    video_id: str
    title: str
    channel_id: str
    channel_name: str
    started_at: datetime


@dataclass
class Metadata:
    video_id: str
    duration: Optional[int]
    channel_id: str
    channel_handle: str
    video_category: str
    title: str = ""

    @property
    def complete_for_vidmatch(self) -> bool:
        return bool(self.channel_id and self.channel_handle and self.video_category)


@dataclass
class BuildStats:
    cards_seen: int = 0
    watch_entries: int = 0
    filtered_ads: int = 0
    filtered_cutoff: int = 0
    filtered_limit: int = 0
    unique_videos: int = 0
    metadata_from_cache: int = 0
    metadata_fetched: int = 0
    metadata_failed: int = 0
    metadata_api: int = 0
    metadata_ytdlp_fallback: int = 0
    api_requests: int = 0
    api_video_batches: int = 0
    api_channel_batches: int = 0
    api_category_requests: int = 0
    output_events: int = 0
    output_new_keys: int = 0
    preserved_existing: int = 0
    aggregated_same_hour: int = 0
    skipped_missing_fields: int = 0
    known_duration_events: int = 0
    shortened_by_next_video: int = 0
    fallback_events: int = 0
    reconstructed_seconds: int = 0
    timezone_corrected: int = 0


class Cancelled(Exception):
    pass


class SilentLogger:
    def debug(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


def clean_text(raw: str) -> str:
    # Entfernt HTML-Tags aus Titel/Kanalnamen, ohne einen kompletten DOM zu bauen.
    text = re.sub(r"<[^>]+>", "", raw)
    return html.unescape(text).replace("\xa0", " ").strip()


def parse_google_takeout(path: Path, cancel: threading.Event, log) -> Tuple[List[HistoryEvent], BuildStats]:
    stats = BuildStats()
    log(f"Lese Google Takeout: {path.name}")

    # 45+ MB sind hier völlig okay und deutlich schneller als BeautifulSoup über das Gesamtdokument.
    data = path.read_text(encoding="utf-8", errors="replace")
    chunks = data.split(CARD_MARKER)
    stats.cards_seen = max(0, len(chunks) - 1)

    events: List[HistoryEvent] = []
    for idx, chunk in enumerate(chunks[1:], start=1):
        if cancel.is_set():
            raise Cancelled()

        vm = VIDEO_RE.search(chunk)
        if not vm:
            continue

        # Google-Werbeeinträge tauchen teils ebenfalls als watch?v= auf.
        if "Von Google Anzeigen" in chunk:
            stats.filtered_ads += 1
            continue

        dm = DATE_RE.search(chunk)
        if not dm:
            continue

        video_id = vm.group(1).strip()
        title = clean_text(vm.group(2))

        cm = CHANNEL_RE.search(chunk)
        channel_id = cm.group(1).strip() if cm else ""
        channel_name = clean_text(cm.group(2)) if cm else ""

        date_text = f"{dm.group(1)} {dm.group(2)}"
        naive_started = datetime.strptime(date_text, "%d.%m.%Y %H:%M:%S")
        # Takeout-HTML kann bei DST-Zonen alle historischen Einträge mit der Zeitzone
        # versehen, die beim Erstellen des Archivs aktiv war. Wir interpretieren zunächst
        # den angegebenen Offset und rechnen anschließend auf CET/CEST des Ereignisdatums um.
        source_tz = fixed_cet_tz(dm.group(3).upper() == "MESZ")
        target_tz = central_europe_tz_for_date(naive_started)
        source_offset = source_tz.utcoffset(None) or timedelta(0)
        target_offset = target_tz.utcoffset(None) or timedelta(0)
        corrected_naive = naive_started + (target_offset - source_offset)
        if corrected_naive != naive_started:
            stats.timezone_corrected += 1
        started = corrected_naive.replace(tzinfo=target_tz)

        events.append(
            HistoryEvent(
                video_id=video_id,
                title=title,
                channel_id=channel_id,
                channel_name=channel_name,
                started_at=started,
            )
        )

        if idx % 5000 == 0:
            log(f"Parser: {idx:,}/{stats.cards_seen:,} Karten geprüft …".replace(",", "."))

    events.sort(key=lambda e: e.started_at)
    stats.watch_entries = len(events)
    stats.unique_videos = len({e.video_id for e in events})
    log(
        f"Gefunden: {stats.watch_entries:,} Video-Aufrufe, "
        f"{stats.unique_videos:,} unterschiedliche Videos.".replace(",", ".")
    )
    return events, stats


def load_json(path: Optional[Path]) -> Dict[str, Any]:
    if not path:
        return {}
    with path.open("r", encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError("VidMatch-Datei muss am obersten Level ein JSON-Objekt sein.")
    return value


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def earliest_vidmatch_datetime(data: Dict[str, Any]) -> Optional[datetime]:
    candidates: List[datetime] = []
    for bucket, items in data.items():
        try:
            bucket_naive = datetime.fromisoformat(bucket)
            bucket_dt = bucket_naive.replace(tzinfo=central_europe_tz_for_date(bucket_naive))
        except Exception:
            continue
        if not isinstance(items, dict):
            continue
        for record in items.values():
            if not isinstance(record, dict):
                continue
            ts = str(record.get("timestamp") or "")
            tm = re.search(r"(\d{2}):(\d{2}):(\d{2})", ts)
            if tm:
                candidates.append(
                    bucket_dt.replace(
                        hour=int(tm.group(1)),
                        minute=int(tm.group(2)),
                        second=int(tm.group(3)),
                    )
                )
            else:
                candidates.append(bucket_dt)
    return min(candidates) if candidates else None


def existing_channel_lookup(data: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    result: Dict[str, Dict[str, str]] = {}
    for items in data.values():
        if not isinstance(items, dict):
            continue
        for rec in items.values():
            if not isinstance(rec, dict):
                continue
            cid = str(rec.get("channelId") or "")
            if not cid:
                continue
            target = result.setdefault(cid, {})
            handle = str(rec.get("channelHandle") or "")
            category = str(rec.get("videoCategory") or "")
            if handle:
                target["handle"] = handle
            if category:
                target["category"] = category
    return result


def load_cache(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def metadata_from_cache(video_id: str, cache: Dict[str, Any]) -> Optional[Metadata]:
    raw = cache.get(video_id)
    if not isinstance(raw, dict):
        return None
    return Metadata(
        video_id=video_id,
        duration=int(raw["duration"]) if raw.get("duration") not in (None, "") else None,
        channel_id=str(raw.get("channelId") or ""),
        channel_handle=str(raw.get("channelHandle") or ""),
        video_category=str(raw.get("videoCategory") or ""),
        title=str(raw.get("title") or ""),
    )


def cache_record(meta: Metadata) -> Dict[str, Any]:
    return {
        "duration": meta.duration,
        "channelId": meta.channel_id,
        "channelHandle": meta.channel_handle,
        "videoCategory": meta.video_category,
        "title": meta.title,
        "cachedAt": datetime.now().astimezone().isoformat(),
    }


def extract_handle(info: Dict[str, Any]) -> str:
    for key in ("channel_url", "uploader_url"):
        value = str(info.get(key) or "")
        m = HANDLE_URL_RE.search(value)
        if m:
            return "@" + unquote(m.group(1))

    for key in ("uploader_id", "channel_handle"):
        value = str(info.get(key) or "").strip()
        if value.startswith("@"):
            return value
    return ""


def fetch_metadata_one(video_id: str, browser_cookie: str = "") -> Metadata:
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("yt-dlp ist nicht installiert. Bitte: pip install -U yt-dlp") from exc

    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 2,
        "sleep_interval_requests": 0.15,
        "logger": SilentLogger(),
    }
    if browser_cookie:
        # yt-dlp erwartet hier ein Tupel wie ('chrome',) oder ('firefox',).
        opts["cookiesfrombrowser"] = (browser_cookie,)

    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        info = ydl.sanitize_info(info)

    duration_raw = info.get("duration")
    try:
        duration = max(1, int(round(float(duration_raw)))) if duration_raw else None
    except Exception:
        duration = None

    categories = info.get("categories")
    if isinstance(categories, list) and categories:
        category = str(categories[0] or "").strip()
    else:
        category = str(info.get("category") or "").strip()

    return Metadata(
        video_id=video_id,
        duration=duration,
        channel_id=str(info.get("channel_id") or "").strip(),
        channel_handle=extract_handle(info),
        video_category=category,
        title=str(info.get("title") or "").strip(),
    )



def fetch_all_metadata_ytdlp(
    events: List[HistoryEvent], cache_path: Path, workers: int, browser_cookie: str,
    cancel: threading.Event, log, progress, stats: BuildStats,
) -> Tuple[Dict[str, Metadata], Dict[str, Any]]:
    cache = load_cache(cache_path)
    event_by_id: Dict[str, HistoryEvent] = {}
    for e in events:
        event_by_id.setdefault(e.video_id, e)

    result: Dict[str, Metadata] = {}
    missing: List[str] = []
    for video_id in event_by_id:
        cached = metadata_from_cache(video_id, cache)
        if cached and cached.duration and cached.complete_for_vidmatch:
            result[video_id] = cached
            stats.metadata_from_cache += 1
        else:
            missing.append(video_id)

    total = len(event_by_id)
    done = len(result)
    progress(done, total, "Metadaten / yt-dlp")
    log(f"Metadaten: {stats.metadata_from_cache:,} aus Cache, {len(missing):,} per yt-dlp.".replace(",", "."))
    if not missing:
        return result, cache

    completed_since_save = 0

    def task(video_id: str) -> Tuple[str, Optional[Metadata], Optional[str]]:
        if cancel.is_set():
            return video_id, None, "abgebrochen"
        try:
            return video_id, fetch_metadata_one(video_id, browser_cookie), None
        except Exception as exc:
            return video_id, None, str(exc)

    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="ytmeta") as pool:
        futures = {pool.submit(task, vid): vid for vid in missing}
        for future in as_completed(futures):
            if cancel.is_set():
                for f in futures:
                    f.cancel()
                raise Cancelled()
            video_id, meta, error = future.result()
            done += 1
            if meta:
                source = event_by_id[video_id]
                if not meta.channel_id:
                    meta.channel_id = source.channel_id
                result[video_id] = meta
                cache[video_id] = cache_record(meta)
                stats.metadata_fetched += 1
                completed_since_save += 1
            else:
                stats.metadata_failed += 1
                if stats.metadata_failed <= 20:
                    clean_error = re.sub(r"\x1b\[[0-9;]*m", "", error or "")
                    log(f"⚠ Metadaten fehlgeschlagen: {video_id} – {clean_error[:300]}")
            progress(done, total, "Metadaten / yt-dlp")
            if completed_since_save >= 25:
                atomic_write_json(cache_path, cache)
                completed_since_save = 0
            if done % 100 == 0 or done == total:
                log(f"Metadaten: {done:,}/{total:,} fertig …".replace(",", "."))

    atomic_write_json(cache_path, cache)
    return result, cache


def fetch_all_metadata_api(
    events: List[HistoryEvent], cache_path: Path, api_key: str,
    workers: int, browser_cookie: str, api_ytdlp_fallback: bool,
    cancel: threading.Event, log, progress, stats: BuildStats,
) -> Tuple[Dict[str, Metadata], Dict[str, Any]]:
    if not api_key.strip():
        raise RuntimeError("Für den schnellen API-Modus fehlt der YouTube Data API v3 Key.")

    cache = load_cache(cache_path)
    event_by_id: Dict[str, HistoryEvent] = {}
    for e in events:
        event_by_id.setdefault(e.video_id, e)

    result: Dict[str, Metadata] = {}
    missing: List[str] = []
    for video_id in event_by_id:
        cached = metadata_from_cache(video_id, cache)
        if cached and cached.duration and cached.complete_for_vidmatch:
            result[video_id] = cached
            stats.metadata_from_cache += 1
        else:
            missing.append(video_id)

    total = len(event_by_id)
    done = len(result)
    progress(done, total, "API: Videos")
    log(
        f"Metadaten: {stats.metadata_from_cache:,} aus Cache, {len(missing):,} per Data API."
        .replace(",", ".")
    )
    if not missing:
        return result, cache

    # Kategorie-Namen: API zuerst, stabile Standardnamen als Fallback.
    categories = dict(FALLBACK_CATEGORY_NAMES)
    try:
        payload = youtube_api_get(
            "videoCategories", {"part": "snippet", "regionCode": "DE"},
            api_key, cancel, stats, log,
        )
        stats.api_category_requests += 1
        for item in payload.get("items", []):
            cid = str(item.get("id") or "")
            title = str((item.get("snippet") or {}).get("title") or "").strip()
            if cid and title:
                categories[cid] = title
    except Exception as exc:
        log(f"⚠ Kategorien-API nicht verfügbar, nutze Standardmapping: {exc}")

    partial: Dict[str, Dict[str, Any]] = {}
    missing_from_api: set[str] = set()
    batches = list(chunks_of(missing, 50))
    for batch_index, batch in enumerate(batches, start=1):
        if cancel.is_set():
            raise Cancelled()
        payload = youtube_api_get(
            "videos",
            {"part": "snippet,contentDetails", "id": ",".join(batch)},
            api_key, cancel, stats, log,
        )
        stats.api_video_batches += 1
        returned = set()
        for item in payload.get("items", []):
            video_id = str(item.get("id") or "")
            if not video_id:
                continue
            returned.add(video_id)
            snippet = item.get("snippet") or {}
            details = item.get("contentDetails") or {}
            partial[video_id] = {
                "duration": parse_iso8601_duration(str(details.get("duration") or "")),
                "channel_id": str(snippet.get("channelId") or "").strip(),
                "category_id": str(snippet.get("categoryId") or "").strip(),
                "title": str(snippet.get("title") or "").strip(),
            }
        missing_from_api.update(set(batch) - returned)
        done = min(total, stats.metadata_from_cache + batch_index * 50)
        progress(done, total, "API: Videos")
        if batch_index % 20 == 0 or batch_index == len(batches):
            log(f"API Videos: {min(batch_index*50, len(missing)):,}/{len(missing):,} IDs geprüft …".replace(",", "."))

    # Kanal-Metadaten in 50er-Batches.
    channel_ids = sorted({p["channel_id"] for p in partial.values() if p.get("channel_id")})
    channel_handles: Dict[str, str] = {}
    channel_batches = list(chunks_of(channel_ids, 50))
    for batch_index, batch in enumerate(channel_batches, start=1):
        if cancel.is_set():
            raise Cancelled()
        payload = youtube_api_get(
            "channels", {"part": "snippet", "id": ",".join(batch)},
            api_key, cancel, stats, log,
        )
        stats.api_channel_batches += 1
        for item in payload.get("items", []):
            cid = str(item.get("id") or "")
            custom_url = str((item.get("snippet") or {}).get("customUrl") or "")
            handle = normalize_api_handle(custom_url)
            if cid and handle:
                channel_handles[cid] = handle
        progress(batch_index, max(1, len(channel_batches)), "API: Kanäle")
        if batch_index % 20 == 0 or batch_index == len(channel_batches):
            log(f"API Kanäle: {min(batch_index*50, len(channel_ids)):,}/{len(channel_ids):,} geprüft …".replace(",", "."))

    fallback_ids: List[str] = []
    for video_id, p in partial.items():
        source = event_by_id[video_id]
        channel_id = p.get("channel_id") or source.channel_id
        meta = Metadata(
            video_id=video_id,
            duration=p.get("duration"),
            channel_id=channel_id,
            channel_handle=channel_handles.get(channel_id, ""),
            video_category=categories.get(str(p.get("category_id") or ""), ""),
            title=str(p.get("title") or source.title),
        )
        if meta.duration and meta.complete_for_vidmatch:
            result[video_id] = meta
            cache[video_id] = cache_record(meta)
            stats.metadata_api += 1
            stats.metadata_fetched += 1
        elif api_ytdlp_fallback:
            fallback_ids.append(video_id)
        else:
            result[video_id] = meta
            cache[video_id] = cache_record(meta)
            stats.metadata_api += 1
            stats.metadata_fetched += 1

    # Private/gelöschte Videos liefert videos.list absichtlich gar nicht zurück.
    # Für diese verschwenden wir keine tausenden yt-dlp-Requests.
    if missing_from_api:
        stats.metadata_failed += len(missing_from_api)
        log(f"API: {len(missing_from_api):,} private/gelöschte/nicht verfügbare Videos ohne Metadaten.".replace(",", "."))

    atomic_write_json(cache_path, cache)

    # Nur API-Treffer mit unvollständigem Handle/Kategorie/Dauer über yt-dlp nachbessern.
    if fallback_ids:
        log(f"yt-dlp-Fallback nur für {len(fallback_ids):,} unvollständige API-Treffer …".replace(",", "."))
        completed = 0

        def task(video_id: str):
            if cancel.is_set():
                return video_id, None, "abgebrochen"
            try:
                return video_id, fetch_metadata_one(video_id, browser_cookie), None
            except Exception as exc:
                return video_id, None, str(exc)

        with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="ytfallback") as pool:
            futures = {pool.submit(task, vid): vid for vid in fallback_ids}
            for future in as_completed(futures):
                if cancel.is_set():
                    for f in futures:
                        f.cancel()
                    raise Cancelled()
                video_id, meta, error = future.result()
                completed += 1
                if meta:
                    source = event_by_id[video_id]
                    if not meta.channel_id:
                        meta.channel_id = source.channel_id
                    # API-Felder beibehalten, falls yt-dlp etwas nicht liefert.
                    p = partial.get(video_id, {})
                    meta.duration = meta.duration or p.get("duration")
                    meta.video_category = meta.video_category or categories.get(str(p.get("category_id") or ""), "")
                    if not meta.channel_handle:
                        meta.channel_handle = channel_handles.get(meta.channel_id, "")
                    result[video_id] = meta
                    cache[video_id] = cache_record(meta)
                    stats.metadata_ytdlp_fallback += 1
                    stats.metadata_fetched += 1
                else:
                    stats.metadata_failed += 1
                    if stats.metadata_failed <= 20:
                        clean_error = re.sub(r"\x1b\[[0-9;]*m", "", error or "")
                        log(f"⚠ Fallback fehlgeschlagen: {video_id} – {clean_error[:250]}")
                progress(completed, len(fallback_ids), "yt-dlp-Fallback")
                if completed % 25 == 0:
                    atomic_write_json(cache_path, cache)
        atomic_write_json(cache_path, cache)

    log(
        f"API fertig: {stats.metadata_api:,} direkt, {stats.metadata_ytdlp_fallback:,} via yt-dlp-Fallback, "
        f"{stats.api_requests:,} HTTP-Requests.".replace(",", ".")
    )
    return result, cache


def fetch_all_metadata(
    events: List[HistoryEvent], cache_path: Path, workers: int, browser_cookie: str,
    cancel: threading.Event, log, progress, stats: BuildStats,
    metadata_mode: str = "api", api_key: str = "", api_ytdlp_fallback: bool = True,
) -> Tuple[Dict[str, Metadata], Dict[str, Any]]:
    if metadata_mode == "api":
        return fetch_all_metadata_api(
            events, cache_path, api_key, workers, browser_cookie, api_ytdlp_fallback,
            cancel, log, progress, stats,
        )
    return fetch_all_metadata_ytdlp(
        events, cache_path, workers, browser_cookie, cancel, log, progress, stats,
    )


def js_timestamp(dt: datetime) -> str:
    localized = dt
    offset = localized.strftime("%z")
    # Takeout liefert MEZ/MESZ; daraus wurde oben bereits ein korrekter fester Offset gebaut.
    is_summer = localized.utcoffset() == timedelta(hours=2)
    zone_name = "Mitteleuropäische Sommerzeit" if is_summer else "Mitteleuropäische Normalzeit"
    return f"{localized:%H:%M:%S} GMT{offset} ({zone_name})"


def bucket_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:00:00")


def estimate_seconds(
    event: HistoryEvent,
    next_event: Optional[HistoryEvent],
    meta: Optional[Metadata],
    fallback_mode: str,
    stats: BuildStats,
) -> int:
    gap: Optional[int] = None
    if next_event:
        gap = max(0, int((next_event.started_at - event.started_at).total_seconds()))

    if meta and meta.duration:
        stats.known_duration_events += 1
        seconds = meta.duration
        if gap is not None and gap < seconds:
            # Das nächste Video ist bereits gestartet -> dieses Video kann nicht länger aktiv gewesen sein.
            seconds = max(1, gap)
            stats.shortened_by_next_video += 1
        return max(1, int(seconds))

    stats.fallback_events += 1
    if fallback_mode == "gap" and gap is not None:
        # Sicherheitsdeckel: ohne bekannte Videolänge niemals mehr als 30 Minuten erfinden.
        return max(1, min(gap, 30 * 60))
    return 1


def build_output(
    events: List[HistoryEvent],
    metadata: Dict[str, Metadata],
    existing: Dict[str, Any],
    allow_incomplete: bool,
    fallback_mode: str,
    cancel: threading.Event,
    log,
    stats: BuildStats,
) -> Tuple[Dict[str, Any], List[str]]:
    output = copy.deepcopy(existing)
    known_channels = existing_channel_lookup(existing)
    unresolved: List[str] = []

    for idx, event in enumerate(events):
        if cancel.is_set():
            raise Cancelled()

        next_event = events[idx + 1] if idx + 1 < len(events) else None
        meta = metadata.get(event.video_id)

        channel_id = (meta.channel_id if meta else "") or event.channel_id
        handle = meta.channel_handle if meta else ""
        category = meta.video_category if meta else ""

        # Bestehende VidMatch-Daten können fehlende Kanal-Metadaten seriös ergänzen.
        if channel_id in known_channels:
            handle = handle or known_channels[channel_id].get("handle", "")
            category = category or known_channels[channel_id].get("category", "")

        if not channel_id or not handle or not category:
            unresolved.append(event.video_id)
            if not allow_incomplete:
                stats.skipped_missing_fields += 1
                continue
            handle = handle or ""
            category = category or "Unknown"

        seconds = estimate_seconds(event, next_event, meta, fallback_mode, stats)
        bucket = bucket_key(event.started_at)
        bucket_obj = output.setdefault(bucket, {})
        if not isinstance(bucket_obj, dict):
            bucket_obj = {}
            output[bucket] = bucket_obj

        if event.video_id in bucket_obj:
            existing_record = bucket_obj[event.video_id]
            # Wurde dieser Key aus der originalen VidMatch-Datei übernommen, hat gemessene Zeit Vorrang.
            original_bucket = existing.get(bucket)
            if isinstance(original_bucket, dict) and event.video_id in original_bucket:
                stats.preserved_existing += 1
                continue

            # Mehrere Google-Starts desselben Videos in derselben Stunde -> Watchtime addieren.
            if isinstance(existing_record, dict):
                existing_record["secondsSpent"] = int(existing_record.get("secondsSpent") or 0) + seconds
                stats.aggregated_same_hour += 1
                stats.reconstructed_seconds += seconds
                stats.output_events += 1
                continue

        bucket_obj[event.video_id] = {
            "secondsSpent": seconds,
            "channelHandle": handle,
            "channelId": channel_id,
            "videoCategory": category,
            "timestamp": js_timestamp(event.started_at),
        }
        stats.output_new_keys += 1
        stats.output_events += 1
        stats.reconstructed_seconds += seconds

    # Chronologisch sortierte Top-Level-Buckets ergeben eine lesbare/konstante Datei.
    output = dict(sorted(output.items(), key=lambda kv: kv[0]))
    unresolved = sorted(set(unresolved))
    log(f"Importobjekt gebaut: {stats.output_new_keys:,} neue Video/Stunden-Keys.".replace(",", "."))
    return output, unresolved


def format_duration(seconds: int) -> str:
    hours = seconds / 3600
    return f"{hours:,.1f} h".replace(",", "X").replace(".", ",").replace("X", ".")


def write_report(
    path: Path,
    stats: BuildStats,
    settings: Dict[str, Any],
    unresolved: List[str],
    cutoff: Optional[datetime],
) -> None:
    lines = [
        f"{APP_NAME} {APP_VERSION}",
        "=" * 60,
        f"Erstellt: {datetime.now().astimezone().strftime('%d.%m.%Y %H:%M:%S')}",
        "",
        "QUELLEN / EINSTELLUNGEN",
        f"Google-Verlauf: {settings['history']}",
        f"Bestehender VidMatch-Export: {settings.get('existing') or 'nicht verwendet'}",
        f"Metadata-Cache: {settings['cache']}",
        f"Cutoff aktiv: {'ja' if settings['cutoff_enabled'] else 'nein'}",
        f"Cutoff: {cutoff.isoformat() if cutoff else '-'}",
        f"Metadatenmodus: {settings.get('metadata_mode', 'yt-dlp')}",
        f"YouTube API Key: {'gesetzt' if settings.get('api_key') else 'nein'}",
        f"API → yt-dlp Fallback: {'ja' if settings.get('api_ytdlp_fallback') else 'nein'}",
        f"yt-dlp Worker: {settings['workers']}",
        f"Browser-Cookies: {settings.get('browser_cookie') or 'nein'}",
        f"Fallback ohne Videolänge: {settings['fallback_mode']}",
        f"Unvollständige Metadaten importieren: {'ja' if settings['allow_incomplete'] else 'nein'}",
        "",
        "ERGEBNIS",
        f"Takeout-Karten geprüft: {stats.cards_seen}",
        f"Video-Aufrufe erkannt: {stats.watch_entries}",
        f"Werbeeinträge verworfen: {stats.filtered_ads}",
        f"Takeout-DST-Zeitstempel korrigiert: {stats.timezone_corrected}",
        f"Durch Cutoff verworfen: {stats.filtered_cutoff}",
        f"Unterschiedliche Videos im Arbeitsset: {stats.unique_videos}",
        f"Metadaten aus Cache: {stats.metadata_from_cache}",
        f"Metadaten neu geladen: {stats.metadata_fetched}",
        f"Davon direkt per Data API: {stats.metadata_api}",
        f"Davon yt-dlp-Fallback: {stats.metadata_ytdlp_fallback}",
        f"API HTTP-Requests: {stats.api_requests}",
        f"API Video-Batches: {stats.api_video_batches}",
        f"API Channel-Batches: {stats.api_channel_batches}",
        f"Metadaten fehlgeschlagen: {stats.metadata_failed}",
        f"Neue Video/Stunden-Keys: {stats.output_new_keys}",
        f"Google-Aufrufe verarbeitet: {stats.output_events}",
        f"Bestehende VidMatch-Werte geschützt: {stats.preserved_existing}",
        f"Mehrfachstarts innerhalb einer Stunde aggregiert: {stats.aggregated_same_hour}",
        f"Wegen fehlender Pflicht-Metadaten übersprungen: {stats.skipped_missing_fields}",
        f"Aufrufe mit bekannter Videolänge: {stats.known_duration_events}",
        f"Durch früheres Folgevideo gekürzt: {stats.shortened_by_next_video}",
        f"Fallback-Watchtime verwendet: {stats.fallback_events}",
        f"Rekonstruierte Watchtime gesamt: {format_duration(stats.reconstructed_seconds)}",
        "",
        "WICHTIG",
        "Die rekonstruierte Watchtime ist eine Schätzung: volle Videolänge, außer ein folgendes",
        "Video startet vorher. Pausen, Überspringen, Wiedergabegeschwindigkeit oder parallele Tabs",
        "lassen sich aus Google Takeout rückwirkend nicht exakt bestimmen.",
    ]
    if unresolved:
        lines += [
            "",
            f"UNVOLLSTÄNDIGE METADATEN ({len(unresolved)} unterschiedliche Video-IDs)",
            "Die ersten 250 IDs:",
            *unresolved[:250],
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1040x790")
        self.minsize(900, 690)

        self.cancel_event = threading.Event()
        self.ui_queue: queue.Queue = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.style = ttk.Style(self)

        self.history_var = tk.StringVar()
        self.existing_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(Path.cwd() / "vidmatch-import-full.json"))
        self.cache_var = tk.StringVar(value=str(Path.cwd() / "vidmatch_metadata_cache.json"))
        self.cutoff_var = tk.BooleanVar(value=True)
        self.allow_incomplete_var = tk.BooleanVar(value=False)
        self.workers_var = tk.IntVar(value=2)
        self.cookies_var = tk.StringVar(value="Keine")
        self.fallback_var = tk.StringVar(value="1 Sekunde")
        self.limit_var = tk.IntVar(value=0)

        self.metadata_mode_var = tk.StringVar(value="YouTube Data API (schnell)")
        self.api_key_var = tk.StringVar(value=os.environ.get("YOUTUBE_API_KEY", ""))
        self.api_show_var = tk.BooleanVar(value=False)
        self.api_ytdlp_fallback_var = tk.BooleanVar(value=True)
        self.dark_mode_var = tk.BooleanVar(value=True)

        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_label_var = tk.StringVar(value="Bereit")
        self.summary_var = tk.StringVar(value="Google Takeout auswählen und Import bauen.")

        self._build_ui()
        self.apply_theme()
        self.after(100, self._drain_queue)
        self.after(150, lambda: enable_windows_dark_titlebar(self, self.dark_mode_var.get()))

    # ---------------- Theme ----------------
    def apply_theme(self) -> None:
        c = DARK_COLORS if self.dark_mode_var.get() else LIGHT_COLORS
        self.configure(bg=c["bg"])
        try:
            self.style.theme_use("clam")
        except Exception:
            pass
        self.style.configure(".", background=c["bg"], foreground=c["fg"], font=("Segoe UI", 10))
        self.style.configure("TFrame", background=c["bg"])
        self.style.configure("Panel.TFrame", background=c["panel"])
        self.style.configure("TLabel", background=c["bg"], foreground=c["fg"])
        self.style.configure("Muted.TLabel", background=c["bg"], foreground=c["muted"])
        self.style.configure("Header.TLabel", background=c["bg"], foreground=c["fg"], font=("Segoe UI", 18, "bold"))
        self.style.configure("Section.TLabel", background=c["bg"], foreground=c["fg"], font=("Segoe UI", 11, "bold"))
        self.style.configure("Heart.TLabel", background=c["bg"], foreground="#ff4d6d", font=("Segoe UI Symbol", 11, "bold"))
        self.style.configure("TButton", background=c["panel2"], foreground=c["fg"], borderwidth=1, padding=(10, 6))
        self.style.map("TButton", background=[("active", c["border"]), ("pressed", c["selection"])], foreground=[("disabled", c["muted"])])
        self.style.configure("Accent.TButton", background=c["accent"], foreground="#ffffff", font=("Segoe UI", 10, "bold"), padding=(14, 8))
        self.style.map("Accent.TButton", background=[("active", c["accent_hover"]), ("disabled", c["panel2"])])
        self.style.configure("TEntry", fieldbackground=c["field"], foreground=c["fg"], insertcolor=c["fg"], bordercolor=c["border"])
        self.style.configure("TCheckbutton", background=c["bg"], foreground=c["fg"])
        self.style.map("TCheckbutton", background=[("active", c["bg"])], foreground=[("active", c["fg"])])
        self.style.configure("TLabelframe", background=c["bg"], foreground=c["fg"], bordercolor=c["border"])
        self.style.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"])
        self.style.configure("TNotebook", background=c["bg"], borderwidth=0)
        self.style.configure("TNotebook.Tab", background=c["panel2"], foreground=c["fg"], padding=(12, 7))
        self.style.map("TNotebook.Tab", background=[("selected", c["selection"]), ("active", c["border"])])
        self.style.configure("TCombobox", fieldbackground=c["field"], background=c["panel2"], foreground=c["fg"], arrowcolor=c["fg"])
        self.style.map("TCombobox", fieldbackground=[("readonly", c["field"])], background=[("readonly", c["panel2"]), ("active", c["border"])], foreground=[("readonly", c["fg"])])
        self.style.configure("TSpinbox", fieldbackground=c["field"], background=c["panel2"], foreground=c["fg"], arrowcolor=c["fg"])
        self.style.configure("JStudios.Horizontal.TProgressbar", troughcolor=c["field"], background=c["progress"], bordercolor=c["border"], thickness=9)
        try:
            self.option_add("*TCombobox*Listbox.background", c["field"])
            self.option_add("*TCombobox*Listbox.foreground", c["fg"])
            self.option_add("*TCombobox*Listbox.selectBackground", c["selection"])
            self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        except Exception:
            pass
        if hasattr(self, "log_box"):
            self.log_box.configure(
                bg=c["field"], fg=c["fg"], insertbackground=c["fg"],
                selectbackground=c["selection"], selectforeground=c["fg"],
            )
        enable_windows_dark_titlebar(self, self.dark_mode_var.get())

    def _toggle_api_visibility(self) -> None:
        self.api_key_entry.configure(show="" if self.api_show_var.get() else "•")

    # ---------------- GUI ----------------
    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 2))
        ttk.Label(header, text=APP_NAME, style="Header.TLabel").pack(side="left")
        ttk.Checkbutton(header, text="Dark Mode", variable=self.dark_mode_var, command=self.apply_theme).pack(side="right")
        ttk.Label(
            root,
            text="Google Takeout → YouTube Data API / yt-dlp → VidMatch-Import mit rekonstruierter Watchtime",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 12))

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)
        main_tab = ttk.Frame(notebook, padding=14)
        metadata_tab = ttk.Frame(notebook, padding=14)
        advanced_tab = ttk.Frame(notebook, padding=14)
        log_tab = ttk.Frame(notebook, padding=8)
        notebook.add(main_tab, text="Import")
        notebook.add(metadata_tab, text="Metadaten")
        notebook.add(advanced_tab, text="Erweitert")
        notebook.add(log_tab, text="Log")

        self._build_main_tab(main_tab)
        self._build_metadata_tab(metadata_tab)
        self._build_advanced_tab(advanced_tab)

        self.log_box = ScrolledText(log_tab, wrap="word", height=20, font=("Consolas", 9), state="disabled", bd=0)
        self.log_box.pack(fill="both", expand=True)

        bottom = ttk.Frame(root)
        bottom.pack(fill="x", pady=(12, 0))
        ttk.Label(bottom, textvariable=self.progress_label_var).pack(anchor="w")
        self.progress = ttk.Progressbar(bottom, variable=self.progress_var, maximum=100, style="JStudios.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(4, 8))

        button_row = ttk.Frame(bottom)
        button_row.pack(fill="x")
        self.start_button = ttk.Button(button_row, text="Import bauen", style="Accent.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(button_row, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Label(button_row, textvariable=self.summary_var, wraplength=650, justify="right").pack(side="right")

        footer = ttk.Frame(root)
        footer.pack(fill="x", pady=(10, 0))
        branding = ttk.Frame(footer)
        branding.pack(side="left")
        ttk.Label(branding, text="Made with ", style="Muted.TLabel").pack(side="left")
        ttk.Label(branding, text="♥", style="Heart.TLabel").pack(side="left")
        ttk.Label(branding, text=" by Julian Schmitz from JStudios", style="Muted.TLabel").pack(side="left")
        ttk.Label(footer, text="© 2026 JStudios", style="Muted.TLabel").pack(side="right")

    def _path_row(self, parent, row: int, label: str, var: tk.StringVar, command, optional: bool = False) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=(10, 8), pady=5)
        ttk.Button(parent, text="Auswählen…", command=command).grid(row=row, column=2, sticky="e", pady=5)
        if optional:
            ttk.Label(parent, text="optional", style="Muted.TLabel").grid(row=row, column=3, sticky="w", padx=(6, 0))

    def _build_main_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(1, weight=1)
        ttk.Label(tab, text="1. Dateien", style="Section.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        self._path_row(tab, 1, "Wiedergabeverlauf.html", self.history_var, self.pick_history)
        self._path_row(tab, 2, "Vorhandener VidMatch-Export", self.existing_var, self.pick_existing, optional=True)
        self._path_row(tab, 3, "Ausgabe-JSON", self.output_var, self.pick_output)
        self._path_row(tab, 4, "Metadata-Cache", self.cache_var, self.pick_cache)

        ttk.Separator(tab).grid(row=5, column=0, columnspan=4, sticky="ew", pady=14)
        ttk.Label(tab, text="2. Rekonstruktion", style="Section.TLabel").grid(row=6, column=0, columnspan=4, sticky="w", pady=(0, 6))
        ttk.Checkbutton(tab, text="Nur Google-Einträge vor der ersten vorhandenen VidMatch-Messung rekonstruieren", variable=self.cutoff_var).grid(row=7, column=0, columnspan=4, sticky="w", pady=3)
        ttk.Label(tab, text="Watchtime: volle Videolänge; startet das nächste Video früher, wird auf diesen Abstand gekürzt.", wraplength=820).grid(row=8, column=0, columnspan=4, sticky="w", pady=(3, 10))
        ttk.Label(tab, text="Ohne bekannte Videolänge:").grid(row=9, column=0, sticky="w")
        ttk.Combobox(tab, textvariable=self.fallback_var, state="readonly", values=["1 Sekunde", "Abstand zum nächsten Video (max. 30 Min.)"], width=38).grid(row=9, column=1, sticky="w", padx=(10, 0))

        ttk.Separator(tab).grid(row=10, column=0, columnspan=4, sticky="ew", pady=14)
        ttk.Label(tab, text="3. Sicherheit", style="Section.TLabel").grid(row=11, column=0, columnspan=4, sticky="w", pady=(0, 6))
        ttk.Label(tab, text="Bestehende echte VidMatch-Messwerte werden beim Merge niemals durch rekonstruierte Werte überschrieben.", wraplength=820).grid(row=12, column=0, columnspan=4, sticky="w")
        ttk.Label(tab, text="Neben der JSON wird automatisch ein *.report.txt erzeugt.", style="Muted.TLabel").grid(row=13, column=0, columnspan=4, sticky="w", pady=(4, 0))

    def _build_metadata_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(1, weight=1)
        ttk.Label(tab, text="Schneller Metadatenabruf", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(tab, text="Modus:").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Combobox(tab, textvariable=self.metadata_mode_var, state="readonly", values=["YouTube Data API (schnell)", "yt-dlp (ohne API-Key)"], width=34).grid(row=1, column=1, sticky="w", pady=5)
        ttk.Label(tab, text="API-Modus fragt Videos und Kanäle in 50er-Batches ab.", style="Muted.TLabel").grid(row=1, column=2, sticky="w", padx=(10, 0))

        ttk.Label(tab, text="YouTube Data API v3 Key:").grid(row=2, column=0, sticky="w", pady=5)
        api_frame = ttk.Frame(tab)
        api_frame.grid(row=2, column=1, columnspan=2, sticky="ew", pady=5)
        api_frame.columnconfigure(0, weight=1)
        self.api_key_entry = ttk.Entry(api_frame, textvariable=self.api_key_var, show="•")
        self.api_key_entry.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(api_frame, text="anzeigen", variable=self.api_show_var, command=self._toggle_api_visibility).grid(row=0, column=1, padx=(8, 0))

        ttk.Checkbutton(tab, text="Bei unvollständigen API-Treffern gezielt yt-dlp als Fallback nutzen", variable=self.api_ytdlp_fallback_var).grid(row=3, column=0, columnspan=3, sticky="w", pady=(7, 3))
        ttk.Label(tab, text="Private/gelöschte Videos werden nicht tausendfach per yt-dlp nachgefragt; nur API-Treffer mit fehlenden Feldern.", style="Muted.TLabel", wraplength=820).grid(row=4, column=0, columnspan=3, sticky="w")

        ttk.Separator(tab).grid(row=5, column=0, columnspan=3, sticky="ew", pady=14)
        ttk.Label(tab, text="yt-dlp / Fallback", style="Section.TLabel").grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(tab, text="Parallele Worker:").grid(row=7, column=0, sticky="w", pady=4)
        ttk.Spinbox(tab, from_=1, to=8, textvariable=self.workers_var, width=7).grid(row=7, column=1, sticky="w", pady=4)
        ttk.Label(tab, text="Nur für yt-dlp. 2–3 ist konservativ.", style="Muted.TLabel").grid(row=7, column=2, sticky="w", padx=(10, 0))
        ttk.Label(tab, text="Browser-Cookies:").grid(row=8, column=0, sticky="w", pady=4)
        ttk.Combobox(tab, textvariable=self.cookies_var, state="readonly", values=["Keine", "chrome", "edge", "firefox", "brave", "opera", "vivaldi"], width=15).grid(row=8, column=1, sticky="w", pady=4)
        ttk.Label(tab, text="Nur falls YouTube den yt-dlp-Fallback ohne Login blockiert.", style="Muted.TLabel").grid(row=8, column=2, sticky="w", padx=(10, 0))

    def _build_advanced_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(1, weight=1)
        ttk.Label(tab, text="Fehlerfälle", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Checkbutton(tab, text='Videos mit fehlendem Handle/Kategorie trotzdem importieren (Kategorie = "Unknown")', variable=self.allow_incomplete_var).grid(row=1, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(tab, text="Standard AUS: private/gelöschte Videos werden lieber übersprungen, statt Metadaten zu erfinden.", style="Muted.TLabel", wraplength=820).grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 12))
        ttk.Label(tab, text="Testlimit (0 = alle Aufrufe):").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Spinbox(tab, from_=0, to=1000000, increment=100, textvariable=self.limit_var, width=10).grid(row=3, column=1, sticky="w", pady=4)
        ttk.Label(tab, text="Für einen kurzen Test z. B. 100 setzen.", style="Muted.TLabel").grid(row=3, column=2, sticky="w", padx=(10, 0))

    # ---------------- Picker / logging ----------------
    def pick_history(self) -> None:
        p = filedialog.askopenfilename(title="YouTube-Wiedergabeverlauf auswählen", filetypes=[("HTML", "*.html"), ("Alle Dateien", "*.*")])
        if p:
            self.history_var.set(p)
            base = Path(p).parent
            if not self.cache_var.get() or Path(self.cache_var.get()).name == "vidmatch_metadata_cache.json":
                self.cache_var.set(str(base / "vidmatch_metadata_cache.json"))
            if Path(self.output_var.get()).name == "vidmatch-import-full.json":
                self.output_var.set(str(base / "vidmatch-import-full.json"))

    def pick_existing(self) -> None:
        p = filedialog.askopenfilename(title="VidMatch-Export auswählen", filetypes=[("JSON", "*.json"), ("Alle Dateien", "*.*")])
        if p:
            self.existing_var.set(p)

    def pick_output(self) -> None:
        p = filedialog.asksaveasfilename(title="Ausgabe speichern", defaultextension=".json", initialfile="vidmatch-import-full.json", filetypes=[("JSON", "*.json")])
        if p:
            self.output_var.set(p)

    def pick_cache(self) -> None:
        p = filedialog.asksaveasfilename(title="Metadata-Cache", defaultextension=".json", initialfile="vidmatch_metadata_cache.json", filetypes=[("JSON", "*.json")])
        if p:
            self.cache_var.set(p)

    def log(self, msg: str) -> None:
        self.ui_queue.put(("log", msg))

    def set_progress(self, current: int, total: int, label: str) -> None:
        self.ui_queue.put(("progress", current, total, label))

    # ---------------- Pipeline ----------------
    def start(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        history = Path(self.history_var.get().strip()) if self.history_var.get().strip() else None
        if not history or not history.exists():
            messagebox.showerror(APP_NAME, "Bitte zuerst Wiedergabeverlauf.html auswählen.")
            return
        existing = Path(self.existing_var.get().strip()) if self.existing_var.get().strip() else None
        if existing and not existing.exists():
            messagebox.showerror(APP_NAME, "Der ausgewählte VidMatch-Export existiert nicht.")
            return
        output = Path(self.output_var.get().strip())
        cache = Path(self.cache_var.get().strip())
        if not output.name or not cache.name:
            messagebox.showerror(APP_NAME, "Bitte Ausgabe- und Cache-Datei festlegen.")
            return

        mode = "api" if self.metadata_mode_var.get().startswith("YouTube Data API") else "yt-dlp"
        if mode == "api" and not self.api_key_var.get().strip():
            messagebox.showerror(APP_NAME, "Für den schnellen API-Modus bitte einen YouTube Data API v3 Key eintragen.")
            return
        if mode == "yt-dlp" or self.api_ytdlp_fallback_var.get():
            try:
                import yt_dlp  # noqa: F401
            except ImportError:
                messagebox.showerror(APP_NAME, "yt-dlp fehlt. Bitte installieren:\n\npip install -U yt-dlp")
                return

        self.cancel_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress_var.set(0)
        self.summary_var.set("Läuft …")
        self._clear_log()

        settings = {
            "history": history, "existing": existing, "output": output, "cache": cache,
            "cutoff_enabled": bool(self.cutoff_var.get()),
            "allow_incomplete": bool(self.allow_incomplete_var.get()),
            "workers": max(1, int(self.workers_var.get())),
            "browser_cookie": "" if self.cookies_var.get() == "Keine" else self.cookies_var.get(),
            "fallback_mode": "gap" if self.fallback_var.get().startswith("Abstand") else "one",
            "limit": max(0, int(self.limit_var.get())),
            "metadata_mode": mode,
            "api_key": self.api_key_var.get().strip(),
            "api_ytdlp_fallback": bool(self.api_ytdlp_fallback_var.get()),
        }
        self.worker_thread = threading.Thread(target=self._run_pipeline, args=(settings,), daemon=True)
        self.worker_thread.start()

    def stop(self) -> None:
        self.cancel_event.set()
        self.stop_button.configure(state="disabled")
        self.progress_label_var.set("Stop angefordert – laufender Request wird noch beendet …")

    def _run_pipeline(self, settings: Dict[str, Any]) -> None:
        started_wall = time.time()
        try:
            self.log(f"{APP_NAME} {APP_VERSION}")
            self.log("=" * 62)
            self.log("Metadatenmodus: " + ("YouTube Data API – 50er-Batches" if settings["metadata_mode"] == "api" else "yt-dlp – Einzelabrufe"))

            existing_data = load_json(settings["existing"])
            cutoff = earliest_vidmatch_datetime(existing_data) if settings["cutoff_enabled"] and existing_data else None
            if cutoff:
                self.log(f"Schutz-Cutoff aus VidMatch erkannt: {cutoff.strftime('%d.%m.%Y %H:%M:%S')}")

            events, stats = parse_google_takeout(settings["history"], self.cancel_event, self.log)
            if cutoff:
                before = len(events)
                events = [e for e in events if e.started_at < cutoff]
                stats.filtered_cutoff = before - len(events)
                self.log(f"Nach Cutoff: {len(events):,} historische Aufrufe übrig.".replace(",", "."))

            limit = settings["limit"]
            if limit and len(events) > limit:
                stats.filtered_limit = len(events) - limit
                events = events[-limit:]
                self.log(f"Testlimit aktiv: nur {limit} Aufrufe.")

            stats.unique_videos = len({e.video_id for e in events})
            if not events:
                raise RuntimeError("Nach den Filtern sind keine Google-Videoaufrufe übrig.")

            metadata, _cache = fetch_all_metadata(
                events=events, cache_path=settings["cache"], workers=settings["workers"],
                browser_cookie=settings["browser_cookie"], cancel=self.cancel_event,
                log=self.log, progress=self.set_progress, stats=stats,
                metadata_mode=settings["metadata_mode"], api_key=settings["api_key"],
                api_ytdlp_fallback=settings["api_ytdlp_fallback"],
            )
            if self.cancel_event.is_set():
                raise Cancelled()

            self.set_progress(0, 1, "Importdatei bauen")
            output_data, unresolved = build_output(
                events=events, metadata=metadata, existing=existing_data,
                allow_incomplete=settings["allow_incomplete"], fallback_mode=settings["fallback_mode"],
                cancel=self.cancel_event, log=self.log, stats=stats,
            )
            atomic_write_json(settings["output"], output_data)
            report_path = settings["output"].with_suffix(".report.txt")
            report_settings = dict(settings)
            report_settings["api_key"] = "gesetzt" if settings.get("api_key") else ""
            report_settings.update({k: str(v) if isinstance(v, Path) else v for k, v in report_settings.items()})
            write_report(report_path, stats, report_settings, unresolved, cutoff)

            elapsed = time.time() - started_wall
            summary = (
                f"Fertig: {stats.output_new_keys:,} neue Keys, ~{format_duration(stats.reconstructed_seconds)} rekonstruiert, "
                f"{stats.skipped_missing_fields:,} übersprungen."
            ).replace(",", ".")
            self.log("")
            self.log(summary)
            self.log(f"JSON: {settings['output']}")
            self.log(f"Report: {report_path}")
            self.log(f"Laufzeit: {elapsed/60:.1f} Min.")
            self.ui_queue.put(("done", summary, str(settings["output"]), str(report_path)))
        except Cancelled:
            self.ui_queue.put(("cancelled",))
        except Exception as exc:
            self.log("")
            self.log("FEHLER:")
            self.log(str(exc))
            self.log(traceback.format_exc())
            self.ui_queue.put(("error", str(exc)))

    def _clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _append_log(self, msg: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _drain_queue(self) -> None:
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1])
                elif kind == "progress":
                    _, current, total, label = item
                    pct = (current / total * 100.0) if total else 0.0
                    self.progress_var.set(pct)
                    self.progress_label_var.set(f"{label}: {current:,}/{total:,} ({pct:.1f} %)".replace(",", "."))
                elif kind == "done":
                    _, summary, output, report = item
                    self.progress_var.set(100)
                    self.progress_label_var.set("Fertig")
                    self.summary_var.set(summary)
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    messagebox.showinfo(APP_NAME, f"Importdatei ist fertig.\n\n{output}\n\nReport:\n{report}")
                elif kind == "cancelled":
                    self.progress_label_var.set("Abgebrochen – Cache bleibt erhalten")
                    self.summary_var.set("Abgebrochen. Bereits geladene Metadaten bleiben im Cache.")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                elif kind == "error":
                    self.progress_label_var.set("Fehler – Details im Log")
                    self.summary_var.set("Fehler. Siehe Log.")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    messagebox.showerror(APP_NAME, item[1])
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)


if __name__ == "__main__":
    App().mainloop()
