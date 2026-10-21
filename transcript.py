"""YouTube transcript extraction module.

First: youtube-transcript-api (fast, but cloud IPs are often blocked).
Second: yt-dlp (emulates a browser and often works in the cloud).
"""

import json
import logging
import tempfile
from pathlib import Path
from typing import List, Tuple, Optional

try:
    from youtube_transcript_api import (
        YouTubeTranscriptApi,
        NoTranscriptFound,
        TranscriptsDisabled,
        VideoUnavailable,
    )
except ImportError as e:
    raise ImportError(
        "The youtube-transcript-api package is required. Install it with `pip install youtube-transcript-api`."
    ) from e

log = logging.getLogger(__name__)

PREFERRED_LANGS: List[str] = ["ko", "en", "ja", "zh-Hans", "zh-Hant"]


class TranscriptError(Exception):
    pass


def fetch_transcript(video_id: str) -> Tuple[str, str]:
    """Fetch a transcript for the given video ID and return its text and language.
    Fall back to yt-dlp if youtube-transcript-api fails.
    """
    try:
        return _fetch_via_transcript_api(video_id)
    except TranscriptError as e:
        log.info("youtube-transcript-api failed (%s); trying yt-dlp", e)
    except Exception as e:
        log.info("youtube-transcript-api raised an exception (%s); trying yt-dlp", e)

    return _fetch_via_yt_dlp(video_id)


def _fetch_via_transcript_api(video_id: str) -> Tuple[str, str]:
    api = YouTubeTranscriptApi()

    # Try list() first to distinguish manual and automatic transcripts.
    try:
        transcript_list = api.list(video_id)
    except TranscriptsDisabled:
        raise TranscriptError("Transcripts are disabled for this video.")
    except VideoUnavailable:
        raise TranscriptError("Video not found.")
    except Exception:
        # Fall back directly to fetch() if list() fails.
        return _fallback_fetch(api, video_id)

    # Prefer manually created transcripts.
    for lang in PREFERRED_LANGS:
        try:
            t = transcript_list.find_manually_created_transcript([lang])
            return _join(t.fetch()), lang
        except Exception:
            continue

    # Then try auto-generated transcripts.
    for lang in PREFERRED_LANGS:
        try:
            t = transcript_list.find_generated_transcript([lang])
            return _join(t.fetch()), lang
        except Exception:
            continue

    # Accept any available language as a final attempt.
    try:
        for t in transcript_list:
            return _join(t.fetch()), getattr(t, "language_code", "unknown")
    except Exception:
        pass

    return _fallback_fetch(api, video_id)


def _fallback_fetch(api: "YouTubeTranscriptApi", video_id: str) -> Tuple[str, str]:
    try:
        fetched = api.fetch(video_id, languages=PREFERRED_LANGS)
    except TranscriptsDisabled:
        raise TranscriptError("Transcripts are disabled for this video.")
    except NoTranscriptFound:
        raise TranscriptError("No available transcript was found.")
    except VideoUnavailable:
        raise TranscriptError("Video not found.")
    except Exception as e:
        raise TranscriptError(f"Failed to fetch transcript: {e}")

    return _join(fetched), getattr(fetched, "language_code", "unknown")


def _join(segments) -> str:
    """Convert a list of snippets or FetchedTranscript into a single text string."""
    parts = []

    # FetchedTranscript has a .snippets attribute (v1.0+).
    items = getattr(segments, "snippets", None) or segments

    for seg in items:
        text = ""
        if isinstance(seg, dict):
            text = seg.get("text", "").strip()
        else:
            text = getattr(seg, "text", "").strip()
        if text:
            parts.append(text)

    return " ".join(parts)


# ===== yt-dlp fallback =====

def _fetch_via_yt_dlp(video_id: str) -> Tuple[str, str]:
    """Fetch transcripts with yt-dlp, which often works in cloud environments.
    Prefer manual transcripts, then automatic ones. Parse the JSON3 format.
    """
    try:
        from yt_dlp import YoutubeDL
    except ImportError as e:
        raise TranscriptError("The yt-dlp package is required.") from e

    url = f"https://www.youtube.com/watch?v={video_id}"

    with tempfile.TemporaryDirectory() as tmp:
        outtmpl = str(Path(tmp) / "%(id)s.%(ext)s")
        last_err: Optional[Exception] = None

        # Try manual transcripts first, then automatically generated ones.
        for use_auto in (False, True):
            opts = {
                "skip_download": True,
                "writesubtitles": not use_auto,
                "writeautomaticsub": use_auto,
                "subtitleslangs": PREFERRED_LANGS,
                "subtitlesformat": "json3",
                "outtmpl": outtmpl,
                "quiet": True,
                "no_warnings": True,
                "ignoreerrors": False,
            }
            try:
                with YoutubeDL(opts) as ydl:
                    ydl.download([url])
            except Exception as e:
                last_err = e
                continue

            # Find downloaded transcript files.
            for lang in PREFERRED_LANGS:
                for ext in ("json3", "json"):
                    candidate = Path(tmp) / f"{video_id}.{lang}.{ext}"
                    if candidate.exists():
                        text = _parse_json3(candidate.read_text(encoding="utf-8"))
                        if text:
                            return text, lang
            # Accept a transcript in any language as a final attempt.
            for p in Path(tmp).iterdir():
                if p.suffix in (".json3", ".json"):
                    text = _parse_json3(p.read_text(encoding="utf-8"))
                    if text:
                        lang = p.stem.split(".")[-1] or "unknown"
                        return text, lang

        raise TranscriptError(
            f"yt-dlp could not find a transcript either: {last_err if last_err else 'none'}"
        )


def _parse_json3(text: str) -> str:
    """Convert a YouTube JSON3 transcript to a single text string."""
    try:
        data = json.loads(text)
    except Exception:
        return ""
    parts = []
    for event in data.get("events", []) or []:
        for seg in event.get("segs", []) or []:
            t = (seg.get("utf8") or "").strip()
            if t:
                parts.append(t)
    return " ".join(parts).strip()
