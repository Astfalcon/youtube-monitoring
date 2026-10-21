import csv
import io
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
import streamlit as st
import streamlit.components.v1 as components
import isodate
from dotenv import load_dotenv

from summarizer import summarize_video, SummarizerError
from transcript import fetch_transcript, TranscriptError
from github_sync import sync_db_to_github, is_github_sync_configured, GitHubSyncError


# =========================================================
# Basic settings
# =========================================================

load_dotenv()

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
YOUTUBE_API_BASE_URL = "https://www.googleapis.com/youtube/v3"
DB_PATH = Path(__file__).parent / "youtube_monitor.db"


# =========================================================
# Exceptions
# =========================================================

class YouTubeAPIError(Exception):
    pass


# =========================================================
# Database functions
# =========================================================

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            youtube_channel_id TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            url TEXT,
            thumbnail_url TEXT,
            uploads_playlist_id TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            youtube_video_id TEXT UNIQUE NOT NULL,
            channel_db_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            published_at TEXT,
            duration_seconds INTEGER,
            is_short INTEGER DEFAULT 0,
            description TEXT,
            thumbnail_url TEXT,
            summary_status TEXT DEFAULT 'not_started',
            summary_text TEXT,
            summary_model TEXT,
            summary_updated_at TIMESTAMP,
            transcript_text TEXT,
            transcript_lang TEXT,
            notified INTEGER DEFAULT 0,
            seen INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(channel_db_id) REFERENCES channels(id)
        )
        """
    )

    # Add missing columns for compatibility with existing databases.
    _ensure_column(cur, "videos", "summary_text", "TEXT")
    _ensure_column(cur, "videos", "summary_model", "TEXT")
    _ensure_column(cur, "videos", "summary_updated_at", "TIMESTAMP")
    _ensure_column(cur, "videos", "transcript_text", "TEXT")
    _ensure_column(cur, "videos", "transcript_lang", "TEXT")
    _ensure_column(cur, "videos", "notified", "INTEGER DEFAULT 0")
    _ensure_column(cur, "videos", "seen", "INTEGER DEFAULT 0")
    _ensure_column(cur, "videos", "summary_retry_count", "INTEGER DEFAULT 0")
    _ensure_column(cur, "channels", "notify_enabled", "INTEGER DEFAULT 1")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_videos_channel_short_published
        ON videos(channel_db_id, is_short, published_at DESC)
        """
    )

    conn.commit()
    conn.close()


def _ensure_column(cur, table: str, column: str, definition: str):
    cur.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    if column not in existing:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def upsert_channel(channel: Dict) -> int:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO channels (
            youtube_channel_id,
            title,
            url,
            thumbnail_url,
            uploads_playlist_id,
            active
        )
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(youtube_channel_id) DO UPDATE SET
            title = excluded.title,
            url = excluded.url,
            thumbnail_url = excluded.thumbnail_url,
            uploads_playlist_id = excluded.uploads_playlist_id,
            active = 1
        """,
        (
            channel["youtube_channel_id"],
            channel["title"],
            channel["url"],
            channel.get("thumbnail_url"),
            channel["uploads_playlist_id"],
        ),
    )

    conn.commit()

    cur.execute(
        """
        SELECT id
        FROM channels
        WHERE youtube_channel_id = ?
        """,
        (channel["youtube_channel_id"],),
    )

    row = cur.fetchone()
    conn.close()

    return int(row["id"])


@st.cache_data(ttl=30)
def get_channels() -> List[Dict]:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT *
        FROM channels
        WHERE active = 1
        ORDER BY created_at DESC
        """
    )

    rows = [dict(row) for row in cur.fetchall()]
    conn.close()

    return rows


@st.cache_data(ttl=30)
def get_inactive_channels() -> List[Dict]:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT *
        FROM channels
        WHERE active = 0
        ORDER BY created_at DESC
        """
    )

    rows = [dict(row) for row in cur.fetchall()]
    conn.close()

    return rows


def reactivate_channel(channel_db_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE channels SET active = 1 WHERE id = ?", (channel_db_id,))
    conn.commit()
    conn.close()


def set_channel_notify(channel_db_id: int, enabled: bool):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE channels SET notify_enabled = ? WHERE id = ?",
        (1 if enabled else 0, channel_db_id),
    )
    conn.commit()
    conn.close()


# ===== Settings (key-value) =====

def get_setting(key: str, default: str = "") -> str:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()
    conn.close()


def get_int_setting(key: str, default: int) -> int:
    try:
        return int(get_setting(key, str(default)))
    except Exception:
        return default


def upsert_videos(channel_db_id: int, videos: List[Dict]) -> Dict[str, int]:
    """Return save results as {'new': n, 'updated': m}."""
    conn = get_connection()
    cur = conn.cursor()

    new_count = 0
    updated_count = 0

    for video in videos:
        cur.execute(
            "SELECT id FROM videos WHERE youtube_video_id = ?",
            (video["youtube_video_id"],),
        )
        existing = cur.fetchone()

        if existing:
            cur.execute(
                """
                UPDATE videos SET
                    title = ?,
                    url = ?,
                    published_at = ?,
                    duration_seconds = ?,
                    is_short = ?,
                    description = ?,
                    thumbnail_url = ?
                WHERE youtube_video_id = ?
                """,
                (
                    video["title"],
                    video["url"],
                    video.get("published_at"),
                    video.get("duration_seconds"),
                    1 if video.get("is_short") else 0,
                    video.get("description"),
                    video.get("thumbnail_url"),
                    video["youtube_video_id"],
                ),
            )
            updated_count += 1
        else:
            cur.execute(
                """
                INSERT INTO videos (
                    youtube_video_id,
                    channel_db_id,
                    title,
                    url,
                    published_at,
                    duration_seconds,
                    is_short,
                    description,
                    thumbnail_url,
                    summary_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'not_started')
                """,
                (
                    video["youtube_video_id"],
                    channel_db_id,
                    video["title"],
                    video["url"],
                    video.get("published_at"),
                    video.get("duration_seconds"),
                    1 if video.get("is_short") else 0,
                    video.get("description"),
                    video.get("thumbnail_url"),
                ),
            )
            new_count += 1

    conn.commit()
    conn.close()

    return {"new": new_count, "updated": updated_count}


@st.cache_data(ttl=30)
def get_videos_by_channel(channel_db_id: int, include_shorts: bool = False) -> List[Dict]:
    conn = get_connection()
    cur = conn.cursor()

    if include_shorts:
        cur.execute(
            """
            SELECT
                id,
                youtube_video_id,
                channel_db_id,
                title,
                url,
                published_at,
                duration_seconds,
                is_short,
                thumbnail_url,
                summary_status,
                seen
            FROM videos
            WHERE channel_db_id = ?
            ORDER BY published_at DESC
            """,
            (channel_db_id,),
        )
    else:
        cur.execute(
            """
            SELECT
                id,
                youtube_video_id,
                channel_db_id,
                title,
                url,
                published_at,
                duration_seconds,
                is_short,
                thumbnail_url,
                summary_status,
                seen
            FROM videos
            WHERE channel_db_id = ?
              AND is_short = 0
            ORDER BY published_at DESC
            """,
            (channel_db_id,),
        )

    rows = [dict(row) for row in cur.fetchall()]
    conn.close()

    return rows


@st.cache_data(ttl=30)
def get_channel_video_stats(include_shorts: bool = False) -> Dict[int, Dict[str, int]]:
    """Quickly fetch video and new-item counts for the channel selector."""
    conn = get_connection()
    cur = conn.cursor()

    if include_shorts:
        join_condition = "v.channel_db_id = c.id"
    else:
        join_condition = "v.channel_db_id = c.id AND v.is_short = 0"

    cur.execute(
        f"""
        SELECT
            c.id AS channel_id,
            COUNT(v.id) AS video_count,
            COALESCE(SUM(CASE WHEN v.seen = 0 AND v.id IS NOT NULL THEN 1 ELSE 0 END), 0) AS new_count
        FROM channels c
        LEFT JOIN videos v ON {join_condition}
        WHERE c.active = 1
        GROUP BY c.id
        """,
    )

    rows = {
        int(row["channel_id"]): {
            "video_count": int(row["video_count"] or 0),
            "new_count": int(row["new_count"] or 0),
        }
        for row in cur.fetchall()
    }
    conn.close()
    return rows


def get_video(video_db_id: int) -> Optional[Dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM videos WHERE id = ?", (video_db_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def update_video_summary(
    video_db_id: int,
    summary_text: str,
    model: str,
    transcript_text: Optional[str],
    transcript_lang: Optional[str],
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE videos SET
            summary_text = ?,
            summary_model = ?,
            summary_status = 'done',
            summary_updated_at = CURRENT_TIMESTAMP,
            transcript_text = COALESCE(?, transcript_text),
            transcript_lang = COALESCE(?, transcript_lang)
        WHERE id = ?
        """,
        (summary_text, model, transcript_text, transcript_lang, video_db_id),
    )
    conn.commit()
    conn.close()


def update_video_summary_status(video_db_id: int, status: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE videos SET summary_status = ? WHERE id = ?",
        (status, video_db_id),
    )
    conn.commit()
    conn.close()


def mark_video_seen(video_db_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE videos SET seen = 1 WHERE id = ?", (video_db_id,))
    conn.commit()
    conn.close()


@st.cache_data(ttl=30)
def get_unseen_videos(limit: int = 50) -> List[Dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT v.*, c.title AS channel_title
        FROM videos v
        JOIN channels c ON c.id = v.channel_db_id
        WHERE v.seen = 0
          AND v.is_short = 0
          AND c.active = 1
        ORDER BY v.published_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


@st.cache_data(ttl=30)
def search_videos(query: str, include_shorts: bool = False, limit: int = 100) -> List[Dict]:
    """Search titles, summaries, and descriptions; return up to limit newest results."""
    if not query or not query.strip():
        return []
    q = f"%{query.strip()}%"
    conn = get_connection()
    cur = conn.cursor()
    short_filter = "" if include_shorts else " AND v.is_short = 0"
    cur.execute(
        f"""
        SELECT v.id, v.youtube_video_id, v.title, v.url, v.published_at,
               v.duration_seconds, v.is_short, v.thumbnail_url,
               v.summary_status, v.summary_text, v.summary_model,
               c.title AS channel_title, c.id AS channel_db_id
        FROM videos v
        JOIN channels c ON c.id = v.channel_db_id
        WHERE c.active = 1
          {short_filter}
          AND (
            v.title LIKE ?
            OR v.summary_text LIKE ?
            OR v.description LIKE ?
          )
        ORDER BY v.published_at DESC
        LIMIT ?
        """,
        (q, q, q, limit),
    )
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


@st.cache_data(ttl=60)
def get_videos_in_period(start_iso: str, end_iso: str, include_shorts: bool = False) -> List[Dict]:
    conn = get_connection()
    cur = conn.cursor()
    if include_shorts:
        cur.execute(
            """
            SELECT v.*, c.title AS channel_title
            FROM videos v
            JOIN channels c ON c.id = v.channel_db_id
            WHERE c.active = 1
              AND v.published_at >= ?
              AND v.published_at < ?
            ORDER BY v.published_at DESC
            """,
            (start_iso, end_iso),
        )
    else:
        cur.execute(
            """
            SELECT v.*, c.title AS channel_title
            FROM videos v
            JOIN channels c ON c.id = v.channel_db_id
            WHERE c.active = 1
              AND v.is_short = 0
              AND v.published_at >= ?
              AND v.published_at < ?
            ORDER BY v.published_at DESC
            """,
            (start_iso, end_iso),
        )
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


def delete_channel(channel_db_id: int):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE channels
        SET active = 0
        WHERE id = ?
        """,
        (channel_db_id,),
    )

    conn.commit()
    conn.close()


# =========================================================
# YouTube API functions
# =========================================================

def ensure_api_key():
    if not YOUTUBE_API_KEY:
        raise YouTubeAPIError(
            "YOUTUBE_API_KEY is not set. Add an API key to your .env file."
        )


def youtube_get(endpoint: str, params: Dict) -> Dict:
    ensure_api_key()

    params = {
        **params,
        "key": YOUTUBE_API_KEY,
    }

    response = requests.get(
        f"{YOUTUBE_API_BASE_URL}/{endpoint}",
        params=params,
        timeout=20,
    )

    if response.status_code != 200:
        raise YouTubeAPIError(
            f"YouTube API request failed: {response.status_code}\n{response.text}"
        )

    return response.json()


def parse_youtube_channel_input(user_input: str) -> Dict[str, Optional[str]]:
    value = user_input.strip()

    if not value:
        raise ValueError("Enter a channel URL or channel ID.")

    if re.match(r"^UC[a-zA-Z0-9_-]{20,}$", value):
        return {"type": "channel_id", "value": value}

    if value.startswith("@"):
        return {"type": "handle", "value": value[1:]}

    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        path = parsed.path.strip("/")

        match = re.search(r"channel/(UC[a-zA-Z0-9_-]+)", path)
        if match:
            return {"type": "channel_id", "value": match.group(1)}

        match = re.search(r"@([^/]+)", path)
        if match:
            return {"type": "handle", "value": match.group(1)}

        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in ["c", "user"]:
            return {"type": "search", "value": parts[1]}

        if path:
            return {"type": "search", "value": path.split("/")[-1]}

    return {"type": "search", "value": value}


def normalize_channel(item: Dict) -> Dict:
    snippet = item.get("snippet", {})
    content_details = item.get("contentDetails", {})
    related_playlists = content_details.get("relatedPlaylists", {})

    channel_id = item["id"]
    uploads_playlist_id = related_playlists.get("uploads")

    if not uploads_playlist_id:
        raise YouTubeAPIError("Could not find this channel's uploads playlist ID.")

    thumbnails = snippet.get("thumbnails", {})
    thumbnail_url = None

    for key in ["high", "medium", "default"]:
        if key in thumbnails:
            thumbnail_url = thumbnails[key].get("url")
            break

    return {
        "youtube_channel_id": channel_id,
        "title": snippet.get("title", "Untitled Channel"),
        "url": f"https://www.youtube.com/channel/{channel_id}",
        "thumbnail_url": thumbnail_url,
        "uploads_playlist_id": uploads_playlist_id,
    }


def get_channel_by_id(channel_id: str) -> Dict:
    data = youtube_get(
        "channels",
        {
            "part": "snippet,contentDetails",
            "id": channel_id,
            "maxResults": 1,
        },
    )

    items = data.get("items", [])

    if not items:
        raise YouTubeAPIError("Could not find a channel with that channel ID.")

    return normalize_channel(items[0])


def get_channel_by_handle(handle: str) -> Dict:
    try:
        data = youtube_get(
            "channels",
            {
                "part": "snippet,contentDetails",
                "forHandle": handle,
                "maxResults": 1,
            },
        )

        items = data.get("items", [])

        if items:
            return normalize_channel(items[0])

    except YouTubeAPIError:
        pass

    return search_channel(handle)


def search_channel(query: str) -> Dict:
    data = youtube_get(
        "search",
        {
            "part": "snippet",
            "q": query,
            "type": "channel",
            "maxResults": 1,
        },
    )

    items = data.get("items", [])

    if not items:
        raise YouTubeAPIError("No channel was found in the search results.")

    channel_id = items[0]["snippet"]["channelId"]

    return get_channel_by_id(channel_id)


def resolve_channel(user_input: str) -> Dict:
    parsed = parse_youtube_channel_input(user_input)

    if parsed["type"] == "channel_id":
        return get_channel_by_id(parsed["value"])

    if parsed["type"] == "handle":
        return get_channel_by_handle(parsed["value"])

    return search_channel(parsed["value"])


def get_recent_video_ids_from_uploads_playlist(
    uploads_playlist_id: str,
    max_results: int = 20,
    page_token: Optional[str] = None,
) -> Tuple[List[str], Optional[str]]:
    """Return (video_ids, next_page_token); None means there are no more pages."""
    params = {
        "part": "contentDetails",
        "playlistId": uploads_playlist_id,
        "maxResults": min(max_results, 50),
    }
    if page_token:
        params["pageToken"] = page_token

    data = youtube_get("playlistItems", params)

    video_ids = []
    for item in data.get("items", []):
        video_id = item.get("contentDetails", {}).get("videoId")
        if video_id:
            video_ids.append(video_id)

    return video_ids, data.get("nextPageToken")


def parse_iso8601_duration_to_seconds(duration_iso: str) -> int:
    try:
        duration = isodate.parse_duration(duration_iso)
        return int(duration.total_seconds())
    except Exception:
        return 0


def detect_short_form(title: str, description: str, duration_seconds: int) -> bool:
    title_lower = title.lower() if title else ""
    description_lower = description.lower() if description else ""

    if duration_seconds <= 60:
        return True

    if "#shorts" in title_lower or "#shorts" in description_lower:
        return True

    return False


def get_video_details(video_ids: List[str]) -> List[Dict]:
    if not video_ids:
        return []

    data = youtube_get(
        "videos",
        {
            "part": "snippet,contentDetails",
            "id": ",".join(video_ids),
            "maxResults": len(video_ids),
        },
    )

    videos = []

    for item in data.get("items", []):
        snippet = item.get("snippet", {})
        content_details = item.get("contentDetails", {})

        video_id = item["id"]
        title = snippet.get("title", "Untitled Video")
        description = snippet.get("description", "")
        published_at = snippet.get("publishedAt")

        duration_iso = content_details.get("duration", "PT0S")
        duration_seconds = parse_iso8601_duration_to_seconds(duration_iso)

        is_short = detect_short_form(
            title=title,
            description=description,
            duration_seconds=duration_seconds,
        )

        thumbnails = snippet.get("thumbnails", {})
        thumbnail_url = None

        for key in ["high", "medium", "default"]:
            if key in thumbnails:
                thumbnail_url = thumbnails[key].get("url")
                break

        videos.append(
            {
                "youtube_video_id": video_id,
                "title": title,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "published_at": published_at,
                "duration_seconds": duration_seconds,
                "is_short": is_short,
                "description": description,
                "thumbnail_url": thumbnail_url,
            }
        )

    return videos


def fetch_recent_videos_for_channel(
    uploads_playlist_id: str,
    max_results: int = 20,
    longform_only: bool = True,
    max_pages: int = 10,
) -> List[Dict]:
    """Collect videos by iterating through the uploads playlist page by page.

    - longform_only=True: Skip Shorts and continue until max_results long-form videos are collected.
    - longform_only=False: Return only the latest max_results videos, including Shorts.
    - max_pages: Maximum pages to scan (up to 50 videos per page; 500 by default).
    """
    collected: List[Dict] = []
    page_token: Optional[str] = None
    pages_scanned = 0

    while pages_scanned < max_pages:
        page_size = min(max_results - len(collected), 50) if not longform_only else 50
        if page_size <= 0:
            break

        video_ids, next_token = get_recent_video_ids_from_uploads_playlist(
            uploads_playlist_id=uploads_playlist_id,
            max_results=page_size,
            page_token=page_token,
        )
        pages_scanned += 1

        if not video_ids:
            break

        page_videos = get_video_details(video_ids)

        if longform_only:
            page_videos = [v for v in page_videos if not v.get("is_short")]

        collected.extend(page_videos)

        if len(collected) >= max_results:
            collected = collected[:max_results]
            break

        if not next_token:
            break
        page_token = next_token

    return collected


def format_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "-"

    seconds = int(seconds)

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    remain_seconds = seconds % 60

    if hours > 0:
        return f"{hours}h {minutes}m {remain_seconds}s"

    if minutes > 0:
        return f"{minutes}m {remain_seconds}s"

    return f"{remain_seconds}s"


def format_published_at(published_at: Optional[str]) -> str:
    if not published_at:
        return "-"
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return published_at


# =========================================================
# Streamlit UI functions
# =========================================================

def sync_db_after_change(reason: str, show_success: bool = False):
    """Sync database changes made on Streamlit Cloud to the GitHub repository.

    If REPO_SYNC_TOKEN is not set, keep local behavior and skip silently.
    """
    if not is_github_sync_configured():
        return

    with st.spinner("Syncing the database to GitHub..."):
        try:
            synced, message = sync_db_to_github(DB_PATH, reason)
        except GitHubSyncError as e:
            st.warning(f"The app database was updated, but GitHub sync failed: {e}")
            return

    if synced and show_success:
        st.caption(message)


def handle_register_channel(channel_input: str, max_results: int, longform_only: bool = True):
    if not channel_input.strip():
        st.warning("Enter a channel URL, @handle, or channel ID.")
        return

    with st.spinner("Checking channel details and fetching videos..."):
        try:
            channel = resolve_channel(channel_input)

            channel_db_id = upsert_channel(channel)

            videos = fetch_recent_videos_for_channel(
                uploads_playlist_id=channel["uploads_playlist_id"],
                max_results=max_results,
                longform_only=longform_only,
            )

            result = upsert_videos(channel_db_id, videos)
            st.cache_data.clear()
            sync_db_after_change(f"register channel: {channel['title']}")

            mode = "long-form only" if longform_only else "including Shorts"
            st.success(
                f"Channel added ({mode}): {channel['title']} / {result['new']} new, {result['updated']} updated"
            )

        except YouTubeAPIError as e:
            st.error(str(e))

        except Exception as e:
            st.error(f"An unexpected error occurred: {e}")


def handle_refresh_all_channels(max_results: int, longform_only: bool = True):
    channels = get_channels()

    if not channels:
        st.warning("No channels have been added.")
        return

    total_new = 0
    total_updated = 0

    progress_bar = st.progress(0)
    status_area = st.empty()

    for index, channel in enumerate(channels, start=1):
        status_area.info(f"Refreshing: {channel['title']}")

        try:
            videos = fetch_recent_videos_for_channel(
                uploads_playlist_id=channel["uploads_playlist_id"],
                max_results=max_results,
                longform_only=longform_only,
            )

            result = upsert_videos(channel["id"], videos)
            total_new += result["new"]
            total_updated += result["updated"]

        except Exception as e:
            st.warning(f"Error processing {channel['title']}: {e}")

        progress_bar.progress(index / len(channels))

    st.cache_data.clear()
    sync_db_after_change("manual refresh all channels")
    status_area.success(
        f"Refresh complete. {total_new} new, {total_updated} updated"
    )


def render_channel_header(channel: Dict):
    # Hover, slide toggle, and three-row distributed layout styles.
    st.markdown(
        """
        <style>
        .ch-link {
            color: #1a73e8;
            text-decoration: none;
            font-weight: 500;
            position: relative;
            transition: color 0.15s ease;
            padding-bottom: 2px;
        }
        .ch-link:hover { color: #0d47a1; }
        .ch-link::after {
            content: '';
            position: absolute;
            left: 0;
            bottom: 0;
            width: 0;
            height: 1.5px;
            background: #0d47a1;
            transition: width 0.25s ease;
        }
        .ch-link:hover::after { width: 100%; }
        .ch-link .arrow { display:inline-block; transition: transform 0.2s ease; }
        .ch-link:hover .arrow { transform: translate(3px, -2px); }

        /* Channel ID slide toggle */
        .id-toggle { display: none; }
        .id-label {
            cursor: pointer;
            color: #888;
            font-size: 0.82rem;
            padding: 1px 8px;
            border: 1px solid #ddd;
            border-radius: 999px;
            user-select: none;
            transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease;
            vertical-align: middle;
        }
        .id-label:hover { background: #f5f5f5; color: #555; border-color: #bbb; }
        .id-toggle:checked + .id-label {
            background: #e8f0fe;
            color: #1a73e8;
            border-color: #1a73e8;
        }
        .id-value {
            display: inline-block;
            max-width: 0;
            overflow: hidden;
            white-space: nowrap;
            vertical-align: middle;
            opacity: 0;
            color: #888;
            font-family: ui-monospace, Menlo, monospace;
            font-size: 0.82rem;
            margin-left: 0;
            transition:
                max-width 0.45s cubic-bezier(0.4, 0, 0.2, 1),
                margin-left 0.45s cubic-bezier(0.4, 0, 0.2, 1),
                opacity 0.3s ease 0.05s;
        }
        .id-toggle:checked + .id-label + .id-value {
            max-width: 600px;
            margin-left: 8px;
            opacity: 1;
        }

        /* Distribute three information rows between the top and bottom of the thumbnail. */
        [data-testid="stElementContainer"]:has(> div > div[data-ch-spacer]) {
            flex: 1 1 0 !important;
            min-height: 4px !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Three columns: thumbnail, details, and deactivate button.
    col_thumb, col_info, col_btn = st.columns(
        [1, 7.5, 1.5], vertical_alignment="top", gap="small"
    )

    with col_thumb:
        thumb = channel.get("thumbnail_url") or ""
        if thumb:
            st.markdown(
                f'<img src="{thumb}" style="width:100%; aspect-ratio:1/1; '
                f'object-fit:cover; border-radius:10px; display:block;" />',
                unsafe_allow_html=True,
            )

    with col_info:
        # Row 1: channel name, aligned with the top of the thumbnail.
        st.markdown(
            f"""
            <div style="font-size:1.4rem; font-weight:700; line-height:1.25;
                        white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
                        margin:0; padding:0;">
              {channel['title']}
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Flex spacer between rows 1 and 2.
        st.markdown('<div data-ch-spacer="1"></div>', unsafe_allow_html=True)

        # Row 2: channel link and Channel ID toggle, centered vertically.
        cid = channel['youtube_channel_id']
        toggle_id = f"id-toggle-{channel['id']}"
        st.markdown(
            f"""
            <div style="line-height:1.25; font-size:0.92rem; margin:0; padding:0;">
              <a href="{channel['url']}" target="_blank" class="ch-link">
                Open channel <span class="arrow">↗</span>
              </a>
              &nbsp;·&nbsp;
              <input type="checkbox" id="{toggle_id}" class="id-toggle">
              <label for="{toggle_id}" class="id-label">Channel ID</label>
              <span class="id-value">{cid}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Flex spacer between rows 2 and 3.
        st.markdown('<div data-ch-spacer="2"></div>', unsafe_allow_html=True)

        # Row 3: notification toggle, aligned with the bottom of the thumbnail.
        current_notify = bool(channel.get("notify_enabled", 1))
        new_notify = st.toggle(
            "🔔 New video notifications",
            value=current_notify,
            key=f"notify_toggle_{channel['id']}",
            help="When disabled, new videos from this channel will not trigger Telegram or email notifications, but will still appear in the app.",
        )
        if new_notify != current_notify:
            set_channel_notify(channel["id"], new_notify)
            st.cache_data.clear()
            sync_db_after_change(
                f"{'enable' if new_notify else 'disable'} notify: {channel['title']}"
            )
            st.rerun()

    with col_btn:
        confirm_key = f"confirm_deactivate_{channel['id']}"
        if st.session_state.get(confirm_key):
            cc1, cc2 = st.columns(2)
            if cc1.button("✓", key=f"confirm_yes_{channel['id']}",
                          type="primary", help="Confirm deactivation"):
                delete_channel(channel["id"])
                st.session_state[confirm_key] = False
                st.cache_data.clear()
                sync_db_after_change(f"deactivate channel: {channel['title']}")
                st.rerun()
            if cc2.button("✕", key=f"confirm_no_{channel['id']}", help="Cancel"):
                st.session_state[confirm_key] = False
                st.rerun()
        else:
            if st.button("Deactivate", key=f"delete_{channel['id']}",
                         use_container_width=True, disabled=_is_guest()):
                st.session_state[confirm_key] = True
                st.rerun()


def handle_summarize_video(video: Dict):
    """Run the transcript-to-summary pipeline."""
    update_video_summary_status(video["id"], "in_progress")

    with st.spinner("Fetching transcript and generating summary..."):
        try:
            transcript_text, lang = fetch_transcript(video["youtube_video_id"])
        except TranscriptError as e:
            update_video_summary_status(video["id"], "failed")
            st.error(f"Could not fetch transcript: {e}")
            return

        try:
            summary, model = summarize_video(
                title=video["title"],
                transcript=transcript_text,
                description=video.get("description") or "",
            )
        except SummarizerError as e:
            update_video_summary_status(video["id"], "failed")
            st.error(f"Summary generation failed: {e}")
            return

        update_video_summary(
            video_db_id=video["id"],
            summary_text=summary,
            model=model,
            transcript_text=transcript_text,
            transcript_lang=lang,
        )

    st.cache_data.clear()
    sync_db_after_change(f"manual summarize video: {video['title']}")
    st.success("Summary generated.")
    st.rerun()


def render_video_detail(video: Dict):
    """Render video details, including its link, thumbnail, summary, and description."""
    col1, col2 = st.columns([1, 3])

    with col1:
        if video.get("thumbnail_url"):
            st.image(video["thumbnail_url"], use_container_width=True)

    with col2:
        st.markdown(f"**Uploaded:** {format_published_at(video.get('published_at'))}")
        st.markdown(f"**Duration:** {format_duration(video.get('duration_seconds'))}")
        st.markdown(f"[Open on YouTube]({video['url']})")

    st.divider()

    summary_status = video.get("summary_status") or "not_started"
    summary_text = video.get("summary_text")

    if summary_status == "done" and summary_text:
        st.markdown("##### Summary")
        if video.get("summary_model"):
            st.caption(f"Model: {video['summary_model']} · Updated: {video.get('summary_updated_at') or '-'}")
        st.write(summary_text)

        if st.button("Regenerate summary", key=f"resummarize_{video['id']}", disabled=_is_guest()):
            handle_summarize_video(video)
    else:
        st.info(
            {
                "not_started": "No summary has been generated yet.",
                "in_progress": "Generating summary...",
                "failed": "The previous summary attempt failed.",
            }.get(summary_status, "Summary status is unknown.")
        )

        if st.button("Generate summary", key=f"summarize_{video['id']}", type="primary", disabled=_is_guest()):
            handle_summarize_video(video)

    with st.expander("Show original description"):
        description = video.get("description") or "No description available."
        st.write(description[:3000])


def render_video_list(channel_db_id: int, videos: List[Dict]):
    """Render a title-focused list with expandable details."""
    if not videos:
        st.info("No saved videos. Refresh the channel to fetch videos.")
        return

    # Compact, single-line video rows.
    st.markdown(
        """
        <style>
        /* Reduce the container border and inner padding. */
        div[class*="st-key-video_row_"] {
            margin-bottom: 4px !important;
        }
        div[class*="st-key-video_row_"] > div {
            padding: 2px 10px !important;
        }
        div[class*="st-key-video_row_"] [data-testid="stVerticalBlock"] {
            gap: 0 !important;
        }
        div[class*="st-key-video_row_"] [data-testid="stHorizontalBlock"] {
            gap: 0.5rem !important;
        }
        /* Remove extra margins from element containers inside each row. */
        div[class*="st-key-video_row_"] [data-testid="stElementContainer"] {
            margin: 0 !important;
            padding: 0 !important;
        }
        /* Borderless, left-aligned, single-line buttons with truncation. */
        div[class*="st-key-video_row_"] button,
        div[class*="st-key-video_row_"] button > div,
        div[class*="st-key-video_row_"] button > div > div {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            text-align: left !important;
            justify-content: flex-start !important;
            align-items: center !important;
            width: 100% !important;
        }
        div[class*="st-key-video_row_"] button {
            min-height: 0 !important;
            height: auto !important;
            padding: 0 !important;
            margin: 0 !important;
            font-weight: 500 !important;
            color: inherit !important;
            display: flex !important;
            line-height: 1.25 !important;
        }
        div[class*="st-key-video_row_"] button p,
        div[class*="st-key-video_row_"] button span,
        div[class*="st-key-video_row_"] button div {
            text-align: left !important;
            white-space: nowrap !important;
            overflow: hidden !important;
            text-overflow: ellipsis !important;
            max-width: 100% !important;
            margin: 0 !important;
            line-height: 1.25 !important;
        }
        div[class*="st-key-video_row_"] button:hover {
            background: rgba(0, 0, 0, 0.04) !important;
            color: #1a73e8 !important;
        }
        /* Highlight the selected row. */
        div[class*="st-key-video_row_selected_"] {
            border-color: #FF3B30 !important;
        }
        /* Single-line metadata matching the button height. */
        .video-meta {
            font-size: 0.78rem;
            color: #888;
            line-height: 1.25;
            text-align: left;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            margin: 0;
            padding: 0;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    selected_key = f"selected_video_{channel_db_id}"
    selected_id = st.session_state.get(selected_key)

    for video in videos:
        is_selected = selected_id == video["id"]
        is_new = not video.get("seen")

        title_prefix = "🔵 " if is_new else ""
        summary_badge = "📝 " if video.get("summary_status") == "done" else ""
        label = f"{title_prefix}{summary_badge}{video['title']}"

        published = format_published_at(video.get("published_at"))
        duration_text = format_duration(video.get("duration_seconds"))

        # Each row uses a bordered container.
        container_key = f"video_row_{'selected_' if is_selected else ''}{video['id']}"
        with st.container(border=True, key=container_key):
            # Allow enough width for a timestamp and duration.
            col_title, col_meta = st.columns([7, 1.7], vertical_alignment="center", gap="small")

            with col_title:
                if st.button(label, key=f"video_btn_{video['id']}", use_container_width=True):
                    if is_selected:
                        st.session_state[selected_key] = None
                    else:
                        st.session_state[selected_key] = video["id"]
                        # Skip database writes when marking items as seen in guest mode.
                        if is_new and not _is_guest():
                            mark_video_seen(video["id"])
                    st.rerun()

            with col_meta:
                st.markdown(
                    f'<div class="video-meta">{published} · {duration_text}</div>',
                    unsafe_allow_html=True,
                )

        if is_selected:
            with st.container(border=True):
                fresh = get_video(video["id"]) or video
                render_video_detail(fresh)


def render_channel_thumbnail_grid(
    channels: List[Dict],
    stats: Dict[int, Dict[str, int]],
    selected_channel_id: int,
    cols_per_row: int = 6,
    key_suffix: str = "",
):
    """Render channel thumbnails; clicking one changes selected_channel_id.
    The selected channel has a red border.

    key_suffix prevents widget key collisions when rendering the grid in multiple tabs.
    """
    # Reduce channel-name font size on all devices and show four columns on mobile.
    st.markdown(
        """
        <style>
        /* Reduce channel button font size and padding on all devices. */
        div[class*="st-key-channel_thumb_grid"] button {
          font-size: 0.72rem !important;
          padding: 2px 4px !important;
          min-height: 0 !important;
          line-height: 1.2 !important;
        }
        div[class*="st-key-channel_thumb_grid"] button p,
        div[class*="st-key-channel_thumb_grid"] button div,
        div[class*="st-key-channel_thumb_grid"] button span {
          font-size: 0.72rem !important;
          line-height: 1.2 !important;
          white-space: nowrap !important;
          overflow: hidden !important;
          text-overflow: ellipsis !important;
        }
        /* Force four columns only on mobile; desktop uses cols_per_row. */
        @media (max-width: 768px) {
          div[class*="st-key-channel_thumb_grid"] [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap !important;
            gap: 0.4rem !important;
          }
          div[class*="st-key-channel_thumb_grid"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            flex: 0 0 calc(25% - 0.3rem) !important;
            min-width: calc(25% - 0.3rem) !important;
            width: calc(25% - 0.3rem) !important;
          }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.container(key=f"channel_thumb_grid{key_suffix}"):
        for i in range(0, len(channels), cols_per_row):
            row = channels[i:i + cols_per_row]
            cols = st.columns(cols_per_row)
            for j, channel in enumerate(row):
                with cols[j]:
                    stat = stats.get(channel["id"], {"video_count": 0, "new_count": 0})
                    is_selected = channel["id"] == selected_channel_id
                    border_color = "#FF3B30" if is_selected else "transparent"
                    thumb = channel.get("thumbnail_url") or ""

                    # Show a circular thumbnail with a red border when selected.
                    st.markdown(
                        f"""
                        <div style="text-align:center;">
                          <div style="display:inline-block; padding:3px; border-radius:50%;
                                      border:3px solid {border_color}; background:{border_color};">
                            {'<img src="' + thumb + '" style="width:72px; height:72px; border-radius:50%; object-fit:cover; display:block;" />'
                             if thumb else '<div style="width:72px;height:72px;border-radius:50%;background:#ddd;"></div>'}
                          </div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

                    # Show the channel name and new-item badge (truncate for mobile).
                    title = channel["title"]
                    if len(title) > 7:
                        title_display = title[:6] + "…"
                    else:
                        title_display = title
                    new_count = stat["new_count"]
                    btn_label = f"🔵 {title_display}" if new_count else title_display
                    if st.button(
                        btn_label,
                        key=f"thumb_select{key_suffix}_{channel['id']}",
                        use_container_width=True,
                        type="primary" if is_selected else "secondary",
                        help=f"{title} · {stat['video_count']} videos" + (f" · NEW {new_count}" if new_count else ""),
                    ):
                        if st.session_state.get("selected_channel_id") != channel["id"]:
                            st.session_state["selected_channel_id"] = channel["id"]
                            st.rerun()


def render_channels(include_shorts: bool = False):
    channels = get_channels()

    st.subheader("Channels / Videos")

    if not channels:
        st.info("No channels have been added. Add a YouTube channel from the sidebar.")
        return

    stats = get_channel_video_stats(include_shorts=include_shorts)
    channel_ids = [channel["id"] for channel in channels]
    channel_by_id = {channel["id"]: channel for channel in channels}

    if st.session_state.get("selected_channel_id") not in channel_ids:
        st.session_state["selected_channel_id"] = channel_ids[0]

    # 1) Thumbnail grid for quick visual selection.
    render_channel_thumbnail_grid(channels, stats, st.session_state["selected_channel_id"])

    # 2) Select box as a keyboard-accessible alternative.
    def format_channel_option(channel_id: int) -> str:
        channel = channel_by_id[channel_id]
        stat = stats.get(channel_id, {"video_count": 0, "new_count": 0})
        new_label = f" · 🔵 NEW {stat['new_count']}" if stat["new_count"] else ""
        return f"{channel['title']} · {stat['video_count']} videos{new_label}"

    selected_channel_id = st.selectbox(
        "Select a channel",
        options=channel_ids,
        format_func=format_channel_option,
        key="selected_channel_id",
    )

    selected_channel = channel_by_id[selected_channel_id]
    render_channel_header(selected_channel)

    # Space between the channel header and video list.
    st.markdown(
        '<div style="height:18px;"></div>',
        unsafe_allow_html=True,
    )

    videos = get_videos_by_channel(
        channel_db_id=selected_channel_id,
        include_shorts=include_shorts,
    )
    render_video_list(selected_channel_id, videos)


def render_inactive_channels():
    """Render inactive channels with reactivation buttons."""
    inactive = get_inactive_channels()
    if not inactive:
        st.info("There are no inactive channels.")
        return

    st.subheader(f"💤 Inactive channels ({len(inactive)})")
    st.caption("The worker no longer checks inactive channels. Reactivate one to resume monitoring.")

    for channel in inactive:
        with st.container(border=True):
            col1, col2, col3 = st.columns([1, 4, 1])
            with col1:
                if channel.get("thumbnail_url"):
                    st.image(channel["thumbnail_url"], width=60)
            with col2:
                st.markdown(f"**{channel['title']}**")
                st.caption(f"[Open channel]({channel['url']}) · `{channel['youtube_channel_id']}`")
            with col3:
                if st.button("Reactivate", key=f"reactivate_{channel['id']}", type="primary", disabled=_is_guest()):
                    reactivate_channel(channel["id"])
                    st.cache_data.clear()
                    sync_db_after_change(f"reactivate channel: {channel['title']}")
                    st.rerun()


def render_notifications():
    unseen = get_unseen_videos(limit=20)

    if not unseen:
        return

    st.subheader(f"🔔 New video notifications ({len(unseen)})")

    for v in unseen[:10]:
        with st.container(border=True):
            st.markdown(f"**[{v['channel_title']}]** {v['title']}")
            st.caption(
                f"{format_published_at(v.get('published_at'))} · {format_duration(v.get('duration_seconds'))} · [Open]({v['url']})"
            )


def videos_to_csv(videos: List[Dict]) -> bytes:
    """Convert a video list to UTF-8 BOM CSV bytes for spreadsheet compatibility."""
    if not videos:
        return b""
    fields = [
        "channel_title", "title", "published_at", "duration_seconds",
        "url", "summary_text", "summary_status",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for v in videos:
        writer.writerow({f: v.get(f, "") for f in fields})
    return ("﻿" + buf.getvalue()).encode("utf-8")


def render_search_tab(include_shorts: bool = False):
    st.subheader("🔍 Search videos")
    st.caption("Search video titles, summaries, and descriptions by keyword.")

    query = st.text_input(
        "Search query",
        placeholder="For example: gaming, AI, indie games",
        key="search_query",
    )

    if not query or not query.strip():
        st.info("Enter a search query.")
        return

    results = search_videos(query=query, include_shorts=include_shorts, limit=100)

    if not results:
        st.warning(f"No videos found for '{query}'.")
        return

    col_a, col_b = st.columns([4, 1])
    with col_a:
        st.caption(f"Search results: **{len(results)}**")
    with col_b:
        st.download_button(
            "📥 Download CSV",
            data=videos_to_csv(results),
            file_name=f"search_{query.strip()[:20]}_{datetime.utcnow().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    for v in results:
        with st.container(border=True):
            col1, col2 = st.columns([1, 4])
            with col1:
                if v.get("thumbnail_url"):
                    st.image(v["thumbnail_url"], use_container_width=True)
            with col2:
                st.markdown(f"**[{v['channel_title']}]** {v['title']}")
                st.caption(
                    f"{format_published_at(v.get('published_at'))} · "
                    f"{format_duration(v.get('duration_seconds'))} · "
                    f"[Open on YouTube]({v['url']})"
                )
                summary = v.get("summary_text") or ""
                if summary:
                    with st.expander("Show summary"):
                        st.write(summary)
                else:
                    st.caption(f"No summary (status: {v.get('summary_status') or 'not_started'})")


def render_trends_tab():
    st.subheader("AI Trend Summary")

    period_mode = st.radio(
        "Time period",
        options=["Weekly", "Monthly"],
        horizontal=True,
        key="trend_period_mode",
    )

    now = datetime.utcnow()
    if period_mode == "Weekly":
        start = now - timedelta(days=7)
        cache_key = "trend_summary_week"
    else:
        start = now - timedelta(days=30)
        cache_key = "trend_summary_month"

    videos = get_videos_in_period(
        start_iso=start.isoformat() + "Z",
        end_iso=(now + timedelta(days=1)).isoformat() + "Z",
        include_shorts=False,
    )

    if not videos:
        st.info("No long-form videos were collected during this period.")
        return

    st.caption(
        f"**Period:** {start.strftime('%Y-%m-%d')} - {now.strftime('%Y-%m-%d')} · {len(videos)} videos"
    )

    col_a, col_b, col_c = st.columns([1, 1, 3])
    with col_a:
        clicked = st.button("Generate summary", key=f"trend_btn_{cache_key}", type="primary", disabled=_is_guest())
    with col_b:
        st.download_button(
            "📥 CSV",
            data=videos_to_csv(videos),
            file_name=f"trend_{period_mode}_{datetime.utcnow().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            key=f"trend_csv_{cache_key}",
        )
    with col_c:
        cached = st.session_state.get(cache_key)
        if cached:
            st.caption(f"Last generated: {cached.get('generated_at', '-')} (model: {cached.get('model', '-')})")

    if clicked:
        with st.spinner("Analyzing trends with Gemini..."):
            try:
                from summarizer import summarize_trend
                summary, model = summarize_trend(videos)
                st.session_state[cache_key] = {
                    "summary": summary,
                    "model": model,
                    "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                }
                st.rerun()
            except SummarizerError as e:
                st.error(f"Trend summary failed: {e}")
            except Exception as e:
                st.error(f"Error: {type(e).__name__}: {e}")

    cached = st.session_state.get(cache_key)
    if cached:
        st.markdown("---")
        st.markdown(cached["summary"])


# =========================================================
# Main app
# =========================================================

def _get_expected_password() -> str:
    """Read the password from environment variables or st.secrets, or return an empty string."""
    val = os.getenv("APP_PASSWORD", "")
    if val:
        return val
    try:
        return st.secrets.get("APP_PASSWORD", "") or ""
    except Exception:
        return ""


def _is_guest() -> bool:
    """Return whether guest mode is enabled; guest mode disables data-changing widgets."""
    return bool(st.session_state.get("guest_mode", False))


def _check_password() -> bool:
    """Provide a single-password gate for public environments such as Streamlit Cloud.
    Skip the gate when APP_PASSWORD is empty (for local use).
    The 'Browse as guest' button grants read-only access (guest_mode=True).
    """
    expected = _get_expected_password()
    if not expected:
        return True

    if st.session_state.get("auth_ok"):
        return True

    col_logo, col_title = st.columns([1, 9])
    with col_logo:
        logo_path = Path(__file__).parent / "assets" / "logo.png"
        if logo_path.exists():
            st.image(str(logo_path), width=80)
    with col_title:
        st.markdown(
            """
            <div style="display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; margin-top:8px;">
              <span style="font-size:2.5rem; font-weight:700;">🔒 Game YouTube Monitoring</span>
              <span style="font-size:1.6rem; color:#6e6e6e; font-weight:500;">made by Adwait Tarpe</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    pwd = st.text_input("Password", type="password", key="pwd_input")
    col_login, col_guest = st.columns([1, 1])
    with col_login:
        if st.button("Log in", use_container_width=True, type="primary"):
            if pwd == expected:
                st.session_state["auth_ok"] = True
                st.session_state["guest_mode"] = False
                st.rerun()
            else:
                st.error("Incorrect password.")
    with col_guest:
        if st.button(
            "👀 Browse as guest (read-only)",
            use_container_width=True,
            help="Continue without a password. Adding, changing, and deleting data are disabled.",
        ):
            st.session_state["auth_ok"] = True
            st.session_state["guest_mode"] = True
            st.rerun()
    return False


PWA_MANIFEST_INLINE = """
<link rel="manifest" href="data:application/json;base64,eyJuYW1lIjoiWVQgTW9uaXRvciIsInNob3J0X25hbWUiOiJZVCBNb25pdG9yIiwic3RhcnRfdXJsIjoiLi8iLCJkaXNwbGF5Ijoic3RhbmRhbG9uZSIsImJhY2tncm91bmRfY29sb3IiOiIjMGUxMTE3IiwidGhlbWVfY29sb3IiOiIjRkYwMDAwIiwiaWNvbnMiOlt7InNyYyI6Imh0dHBzOi8vd3d3LnlvdXR1YmUuY29tL2Zhdmljb24uaWNvIiwic2l6ZXMiOiI2NHg2NCIsInR5cGUiOiJpbWFnZS9pY28ifV19">
<meta name="theme-color" content="#FF0000">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="YT Monitor">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="https://www.youtube.com/s/desktop/d743f8e9/img/favicon_144x144.png">
"""


def render_overview_tab(include_shorts: bool = False):
    """Render the overview tab with a compact event calendar and channel thumbnail grid."""
    # Compact styles used only by the overview tab.
    st.markdown(
        """
        <style>
        /* Compact overview header. */
        .overview-header {
          font-size: 1.0rem;
          font-weight: 600;
          margin: 4px 0 8px 0;
          color: inherit;
        }
        /* Channel thumbnails in the overview, from mobile to laptop widths. */
        div[class*="st-key-channel_thumb_grid_overview"] img[style*="border-radius:50%"] {
          width: 56px !important;
          height: 56px !important;
        }
        div[class*="st-key-channel_thumb_grid_overview"] div[style*="border-radius:50%"] > div {
          /* Placeholder when no thumbnail is available. */
          width: 56px !important;
          height: 56px !important;
        }
        /* Enlarge icons and text on full-width screens (1200px and wider). */
        @media (min-width: 1200px) {
          div[class*="st-key-channel_thumb_grid_overview"] img[style*="border-radius:50%"],
          div[class*="st-key-channel_thumb_grid_overview"] div[style*="border-radius:50%"] > div {
            width: 88px !important;
            height: 88px !important;
          }
          div[class*="st-key-channel_thumb_grid_overview"] button {
            font-size: 0.85rem !important;
          }
          div[class*="st-key-channel_thumb_grid_overview"] button p,
          div[class*="st-key-channel_thumb_grid_overview"] button div,
          div[class*="st-key-channel_thumb_grid_overview"] button span {
            font-size: 0.85rem !important;
          }
        }
        /* Reduce divider spacing. */
        div[data-testid="stVerticalBlock"] > div > hr {
          margin: 8px 0 !important;
        }
          /* At medium widths, wrap the overview grid into eight columns.
              At 1200px and wider, use Streamlit's default twelve columns. */
        @media (min-width: 769px) and (max-width: 1199px) {
          div[class*="st-key-channel_thumb_grid_overview"] [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap !important;
            gap: 0.4rem !important;
          }
          div[class*="st-key-channel_thumb_grid_overview"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            flex: 0 0 calc(12.5% - 0.35rem) !important;
            min-width: calc(12.5% - 0.35rem) !important;
            width: calc(12.5% - 0.35rem) !important;
          }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # 1) Compact calendar at the top.
    render_event_calendar(compact=True)

    st.divider()

    # 2) Channel thumbnails at the bottom.
    channels = get_channels()
    st.markdown('<div class="overview-header">🎬 Channels</div>', unsafe_allow_html=True)
    if not channels:
        st.info("No channels have been added. Add one from the 'Channels / Videos' tab.")
    else:
        stats = get_channel_video_stats(include_shorts=include_shorts)
        channel_ids = [c["id"] for c in channels]
        if st.session_state.get("selected_channel_id") not in channel_ids:
            st.session_state["selected_channel_id"] = channel_ids[0]
        render_channel_thumbnail_grid(
            channels,
            stats,
            st.session_state["selected_channel_id"],
            cols_per_row=12,  # Twelve columns on desktop; CSS forces four on mobile.
            key_suffix="_overview",
        )
        st.caption("👉 View videos in the '📺 Channels / Videos' tab")


def render_event_calendar(compact: bool = False):
    """Render the game events calendar using its public Google Calendar URL.

    compact=True reduces the header, height, and caption for the overview tab.
    """
    # Public Google Calendar URL used by the calendar publisher.
    cal_src = (
        "c_58bf32be64488847f1007ad21615b0fcdf767a96764ea45106af7b5b75974456"
        "%40group.calendar.google.com"
    )
    gcal_url = (
        f"https://calendar.google.com/calendar/embed?src={cal_src}"
        "&ctz=Asia/Seoul&wkst=2&hl=en&mode=MONTH"
    )

    if compact:
        st.markdown(
            '<div class="overview-header">📅 Gaming Events Calendar</div>',
            unsafe_allow_html=True,
        )
        components.iframe(gcal_url, height=480, scrolling=True)
    else:
        st.subheader("📅 Gaming Events Calendar")
        components.iframe(gcal_url, height=700, scrolling=True)
        st.markdown(
            '<div style="font-size:0.78rem; color:#888; margin-top:6px;">'
            'Calendar source: public Google Calendar</div>',
            unsafe_allow_html=True,
        )


def main():
    st.set_page_config(
        page_title="Game YouTube Monitoring",
        page_icon=str(Path(__file__).parent / "assets" / "logo.png")
            if (Path(__file__).parent / "assets" / "logo.png").exists()
            else "🎮",
        layout="wide",
    )

    # Inject the PWA manifest and mobile metadata.
    st.markdown(PWA_MANIFEST_INLINE, unsafe_allow_html=True)

    if not _check_password():
        return

    init_db()

    # Guest mode notice.
    if _is_guest():
        st.markdown(
            '<div style="background:#fff4d6; border:1px solid #f0c040; '
            'border-radius:6px; padding:8px 12px; margin-bottom:10px; font-size:0.9rem;">'
            '👀 <b>Guest mode</b> — You are browsing without a password. '
            'Data-changing actions such as adding channels, refreshing, generating summaries, and changing notification settings are disabled.'
            '</div>',
            unsafe_allow_html=True,
        )

    col_logo, col_title = st.columns([1, 9])
    with col_logo:
        logo_path = Path(__file__).parent / "assets" / "logo.png"
        if logo_path.exists():
            st.image(str(logo_path), width=80)
    with col_title:
        st.markdown(
            """
            <div style="display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; margin-top:8px;">
              <span style="font-size:2.5rem; font-weight:700;">Game YouTube Monitoring</span>
              <span style="font-size:1.6rem; color:#6e6e6e; font-weight:500;">made by Adwait Tarpe</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

    is_guest = _is_guest()

    with st.sidebar:
        st.header("Add a channel")
        if is_guest:
            st.caption("🔒 Guest mode — input disabled")

        channel_input = st.text_input(
            "YouTube channel URL / @handle / channel ID",
            placeholder="For example: https://www.youtube.com/@handle or @handle",
            disabled=is_guest,
        )

        max_results = st.slider(
            "Number of recent videos to fetch",
            min_value=5,
            max_value=200,
            value=30,
            step=5,
            disabled=is_guest,
        )

        longform_only_fetch = st.checkbox(
            "Fetch long-form videos only (do not save Shorts)",
            value=True,
            help="When checked, skip Shorts and fetch additional pages until the selected number of long-form videos is collected.",
            disabled=is_guest,
        )

        register_clicked = st.button(
            "Add channel and fetch videos",
            use_container_width=True,
            disabled=is_guest,
        )

        st.divider()

        # View options are available in guest mode.
        include_shorts = st.checkbox(
            "Include Shorts",
            value=False,
        )

        show_inactive = st.checkbox(
            "Show inactive channels",
            value=False,
            help="View and reactivate inactive channels.",
        )

        refresh_all_clicked = st.button(
            "Refresh all channels",
            use_container_width=True,
            disabled=is_guest,
        )

        st.divider()

        with st.expander("🔔 Notification settings", expanded=False):
            if is_guest:
                st.caption("🔒 Guest mode — settings are read-only")
            current_mode = get_setting("notify_mode", "instant")
            mode = st.radio(
                "Notification mode",
                options=["instant", "digest"],
                format_func=lambda x: "Send immediately" if x == "instant" else "Daily digest",
                index=0 if current_mode == "instant" else 1,
                key="notify_mode_radio",
                help="A digest groups new videos and sends them together at the scheduled time.",
                disabled=is_guest,
            )
            if not is_guest and mode != current_mode:
                set_setting("notify_mode", mode)
                st.cache_data.clear()
                sync_db_after_change(f"set notify_mode={mode}")
                st.rerun()

            quiet_on = get_int_setting("quiet_hours_enabled", 0) == 1
            quiet_on_new = st.checkbox("Enable quiet hours", value=quiet_on, key="quiet_on", disabled=is_guest)
            if not is_guest and quiet_on_new != quiet_on:
                set_setting("quiet_hours_enabled", "1" if quiet_on_new else "0")
                sync_db_after_change(f"set quiet_hours_enabled={quiet_on_new}")
                st.rerun()

            if quiet_on_new:
                qs = get_int_setting("quiet_start_kst", 23)
                qe = get_int_setting("quiet_end_kst", 7)
                col_qs, col_qe = st.columns(2)
                qs_new = col_qs.number_input(
                    "Quiet hours start (KST, hour)", min_value=0, max_value=23, value=qs, key="qs_inp",
                    disabled=is_guest,
                )
                qe_new = col_qe.number_input(
                    "Quiet hours end (KST, hour)", min_value=0, max_value=23, value=qe, key="qe_inp",
                    disabled=is_guest,
                )
                if not is_guest and (qs_new != qs or qe_new != qe):
                    set_setting("quiet_start_kst", str(int(qs_new)))
                    set_setting("quiet_end_kst", str(int(qe_new)))
                    sync_db_after_change(f"set quiet_hours {qs_new}-{qe_new} KST")
                    st.rerun()

            if mode == "digest":
                dh = get_int_setting("digest_hour_kst", 9)
                dh_new = st.number_input(
                    "Digest send time (KST, hour)",
                    min_value=0, max_value=23, value=dh, key="dh_inp",
                    disabled=is_guest,
                )
                if not is_guest and dh_new != dh:
                    set_setting("digest_hour_kst", str(int(dh_new)))
                    sync_db_after_change(f"set digest_hour={dh_new}")
                    st.rerun()

            pending = get_int_setting("pending_notify_count", 0)
            if pending:
                st.caption(f"⏳ Pending notifications: {pending}")

        with st.expander("🗂️ Data retention", expanded=False):
            if is_guest:
                st.caption("🔒 Guest mode — settings are read-only")
            retention_on = get_int_setting("retention_enabled", 0) == 1
            retention_on_new = st.checkbox(
                "Automatically remove old videos",
                value=retention_on,
                key="retention_on",
                help="When the worker runs, automatically delete videos older than the selected retention period if they have already been notified.",
                disabled=is_guest,
            )
            if not is_guest and retention_on_new != retention_on:
                set_setting("retention_enabled", "1" if retention_on_new else "0")
                sync_db_after_change(f"set retention_enabled={retention_on_new}")
                st.rerun()
            if retention_on_new:
                months = get_int_setting("data_retention_months", 12)
                months_new = st.number_input(
                    "Retention period (months)",
                    min_value=1, max_value=60, value=months, key="ret_months",
                    disabled=is_guest,
                )
                if not is_guest and months_new != months:
                    set_setting("data_retention_months", str(int(months_new)))
                    sync_db_after_change(f"set retention={months_new} months")
                    st.rerun()

        st.divider()

        st.markdown("### Current version")
        st.caption("v0.3")
        st.caption("Retries / search / channel notifications / quiet hours / digests")

    if register_clicked:
        handle_register_channel(channel_input, max_results, longform_only_fetch)

    if refresh_all_clicked:
        handle_refresh_all_channels(max_results, longform_only_fetch)

    tab_home, tab_channels, tab_search, tab_notifications, tab_trends, tab_calendar = st.tabs(
        ["🏠 Home", "📺 Channels / Videos", "🔍 Search", "🔔 Notifications", "📊 Trends", "📅 Events Calendar"]
    )

    with tab_home:
        render_overview_tab(include_shorts=include_shorts)

    with tab_channels:
        if show_inactive:
            render_inactive_channels()
            st.divider()
        render_channels(include_shorts=include_shorts)

    with tab_search:
        render_search_tab(include_shorts=include_shorts)

    with tab_notifications:
        render_notifications()

    with tab_trends:
        render_trends_tab()

    with tab_calendar:
        render_event_calendar()


if __name__ == "__main__":
    main()
