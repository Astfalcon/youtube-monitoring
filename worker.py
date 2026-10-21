"""Worker for periodically checking for new videos.

Intended to run periodically through Windows Task Scheduler.
Usage:
    python worker.py            # Run once and exit
    python worker.py --no-mail  # Update the database without sending notifications
"""

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List

# Import modules from the same directory.
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
from notifier import send_new_videos_email, NotifierError, is_email_configured  # noqa: E402
from telegram_notifier import (  # noqa: E402
    send_new_videos_telegram,
    TelegramNotifierError,
    is_telegram_configured,
)
from transcript import fetch_transcript, TranscriptError  # noqa: E402
from summarizer import summarize_video, SummarizerError  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(ROOT / "worker.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("worker")


def detect_new_videos(channel: Dict, max_results: int, longform_only: bool = True) -> List[Dict]:
    """Fetch recent channel videos and return only items not already in the database.
    Add channel_title and notify_enabled to each video dictionary."""
    videos = app_module.fetch_recent_videos_for_channel(
        uploads_playlist_id=channel["uploads_playlist_id"],
        max_results=max_results,
        longform_only=longform_only,
    )

    # Fetch existing IDs.
    conn = app_module.get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT youtube_video_id FROM videos WHERE channel_db_id = ?",
        (channel["id"],),
    )
    existing_ids = {row[0] for row in cur.fetchall()}
    conn.close()

    new_videos = [v for v in videos if v["youtube_video_id"] not in existing_ids]

    # Upsert all videos.
    app_module.upsert_videos(channel["id"], videos)

    # Only long-form videos are eligible for notifications.
    new_longform = [v for v in new_videos if not v.get("is_short")]

    # Add notification metadata, including the channel-level notification toggle.
    notify_enabled = bool(channel.get("notify_enabled", 1))
    for v in new_longform:
        v["channel_title"] = channel["title"]
        v["notify_enabled"] = notify_enabled

    return new_longform


def auto_summarize(video_db_id: int, youtube_video_id: str, title: str, description: str) -> bool:
    """Summarize with Gemini; fall back to the description if transcript retrieval fails."""
    app_module.update_video_summary_status(video_db_id, "in_progress")

    transcript_text = ""
    lang = ""
    transcript_ok = False

    try:
        transcript_text, lang = fetch_transcript(youtube_video_id)
        transcript_ok = True
    except TranscriptError as e:
        log.warning("Transcript extraction failed [%s] (falling back to description): %s", title, e)
    except Exception as e:
        log.warning("Transcript extraction raised an exception [%s] (falling back to description): %s", title, e)

    if not transcript_ok and not (description or "").strip():
        app_module.update_video_summary_status(video_db_id, "failed")
        log.warning("No transcript or description for [%s]; skipping summary", title)
        return False

    try:
        summary, model = summarize_video(
            title=title,
            transcript=transcript_text,
            description=description or "",
        )
    except SummarizerError as e:
        app_module.update_video_summary_status(video_db_id, "failed")
        log.error("Summary generation failed [%s]: %s", title, e)
        return False

    app_module.update_video_summary(
        video_db_id=video_db_id,
        summary_text=summary,
        model=model,
        transcript_text=transcript_text if transcript_ok else None,
        transcript_lang=lang if transcript_ok else None,
    )
    source = "transcript" if transcript_ok else "description"
    log.info("Summary complete [%s] (model: %s, source: %s)", title, model, source)
    return True


def get_video_db_id(youtube_video_id: str) -> int:
    conn = app_module.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM videos WHERE youtube_video_id = ?", (youtube_video_id,))
    row = cur.fetchone()
    conn.close()
    return int(row["id"]) if row else 0


def get_retry_candidates(limit: int = 10, max_retries: int = 3) -> List[Dict]:
    """Fetch long-form videos with summary_status='failed' and retry_count below max_retries."""
    conn = app_module.get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT v.id, v.youtube_video_id, v.title, v.description,
               COALESCE(v.summary_retry_count, 0) AS summary_retry_count
        FROM videos v
        JOIN channels c ON c.id = v.channel_db_id
        WHERE v.summary_status = 'failed'
          AND v.is_short = 0
          AND c.active = 1
          AND COALESCE(v.summary_retry_count, 0) < ?
        ORDER BY v.published_at DESC
        LIMIT ?
        """,
        (max_retries, limit),
    )
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


def increment_retry_count(video_db_id: int):
    conn = app_module.get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE videos SET summary_retry_count = COALESCE(summary_retry_count, 0) + 1 WHERE id = ?",
        (video_db_id,),
    )
    conn.commit()
    conn.close()


def mark_notified(video_ids: List[str]):
    if not video_ids:
        return
    conn = app_module.get_connection()
    cur = conn.cursor()
    placeholders = ",".join(["?"] * len(video_ids))
    cur.execute(
        f"UPDATE videos SET notified = 1 WHERE youtube_video_id IN ({placeholders})",
        video_ids,
    )
    conn.commit()
    conn.close()


# ===== Notification settings (quiet hours / digest) =====

def _now_kst_hour() -> int:
    """Convert the current UTC time to KST (+9) and return the hour (0-23)."""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=9)).hour


def is_within_quiet_hours() -> bool:
    if app_module.get_int_setting("quiet_hours_enabled", 0) != 1:
        return False
    qs = app_module.get_int_setting("quiet_start_kst", 23)
    qe = app_module.get_int_setting("quiet_end_kst", 7)
    h = _now_kst_hour()
    if qs == qe:
        return False
    if qs < qe:
        return qs <= h < qe
    # Handle ranges that cross midnight (for example, 23-7).
    return h >= qs or h < qe


def is_digest_time() -> bool:
    """Return whether digest mode is active and the current KST hour is the send time."""
    if app_module.get_setting("notify_mode", "instant") != "digest":
        return False
    target = app_module.get_int_setting("digest_hour_kst", 9)
    return _now_kst_hour() == target


def cleanup_old_videos() -> int:
    """Delete notified videos older than data_retention_months; return 0 when disabled."""
    if app_module.get_int_setting("retention_enabled", 0) != 1:
        return 0
    months = app_module.get_int_setting("data_retention_months", 12)
    if months <= 0:
        return 0

    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30 * months)).isoformat()

    conn = app_module.get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM videos
        WHERE published_at < ?
          AND notified = 1
        """,
        (cutoff,),
    )
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted or 0


def get_pending_unnotified() -> List[Dict]:
    """Fetch unnotified long-form videos from active channels with notifications enabled."""
    conn = app_module.get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT v.id, v.youtube_video_id, v.title, v.url, v.published_at,
               v.duration_seconds, v.is_short, v.description, v.thumbnail_url,
               v.summary_text, c.title AS channel_title
        FROM videos v
        JOIN channels c ON c.id = v.channel_db_id
        WHERE v.notified = 0
          AND v.is_short = 0
          AND c.active = 1
          AND c.notify_enabled = 1
        ORDER BY v.published_at DESC
        LIMIT 100
        """
    )
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    for v in rows:
        v["notify_enabled"] = True
    return rows


def run_once(
    max_results: int = 30,
    send_mail: bool = True,
    longform_only: bool = True,
    auto_summarize_new: bool = True,
) -> Dict:
    app_module.init_db()

    channels = app_module.get_channels()
    if not channels:
        log.info("No channels have been added.")
        return {"channels": 0, "new": 0, "summarized": 0, "mail_sent": False}

    all_new: List[Dict] = []

    for channel in channels:
        try:
            new_videos = detect_new_videos(channel, max_results, longform_only)
            log.info(
                "Channel [%s]: %d new long-form video(s)",
                channel["title"],
                len(new_videos),
            )
            all_new.extend(new_videos)
        except Exception as e:
            log.error("Error processing channel [%s]: %s", channel["title"], e)

    # Generate summaries automatically.
    summarized_count = 0
    if all_new and auto_summarize_new:
        log.info("Starting automatic summaries for %d video(s)", len(all_new))
        for v in all_new:
            video_db_id = get_video_db_id(v["youtube_video_id"])
            if not video_db_id:
                continue
            ok = auto_summarize(
                video_db_id=video_db_id,
                youtube_video_id=v["youtube_video_id"],
                title=v["title"],
                description=v.get("description") or "",
            )
            if ok:
                summarized_count += 1
                # Include the summary in the in-memory object for notification messages.
                fresh = app_module.get_video(video_db_id)
                if fresh:
                    v["summary_text"] = fresh.get("summary_text")
            else:
                increment_retry_count(video_db_id)

    # Retry previously failed summaries.
    retried_count = 0
    if auto_summarize_new:
        retry_candidates = get_retry_candidates(limit=10, max_retries=3)
        if retry_candidates:
            log.info("Retrying %d failed summary/summaries", len(retry_candidates))
            for rc in retry_candidates:
                ok = auto_summarize(
                    video_db_id=rc["id"],
                    youtube_video_id=rc["youtube_video_id"],
                    title=rc["title"],
                    description=rc.get("description") or "",
                )
                if ok:
                    retried_count += 1
                else:
                    increment_retry_count(rc["id"])

    # Exclude videos from channels with notifications disabled.
    notify_targets = [v for v in all_new if v.get("notify_enabled", True)]
    muted_count = len(all_new) - len(notify_targets)
    if muted_count:
        log.info("Excluded %d video(s) from notifications (channel notifications are off)", muted_count)

    # Apply notification mode, quiet hours, and digest policies.
    mode = app_module.get_setting("notify_mode", "instant")
    in_quiet = is_within_quiet_hours()
    digest_due = is_digest_time()

    should_dispatch = True
    dispatch_payload = notify_targets

    if in_quiet:
        log.info("Quiet hours are active; holding notifications and keeping notified=0")
        should_dispatch = False
    elif mode == "digest":
        if digest_due:
            # At digest time, send all accumulated unnotified videos.
            pending = get_pending_unnotified()
            log.info("Digest time; sending %d accumulated video(s)", len(pending))
            dispatch_payload = pending
        else:
            log.info("Digest mode; not send time yet, accumulating notifications")
            should_dispatch = False

    # Record the pending notification count for display in the UI.
    pending_count = 0
    try:
        if not should_dispatch:
            pending_count = len(get_pending_unnotified())
        app_module.set_setting("pending_notify_count", str(pending_count))
    except Exception:
        pass

    mail_sent = False
    telegram_sent = False
    if should_dispatch and dispatch_payload and send_mail:
        if is_email_configured():
            try:
                send_new_videos_email(dispatch_payload)
                mail_sent = True
                log.info("Email sent for %d video(s)", len(dispatch_payload))
            except NotifierError as e:
                log.error("Failed to send email: %s", e)
        else:
            log.info("SMTP is not configured; skipping email")

        if is_telegram_configured():
            try:
                send_new_videos_telegram(dispatch_payload)
                telegram_sent = True
                log.info("Telegram notification sent for %d video(s)", len(dispatch_payload))
            except TelegramNotifierError as e:
                log.error("Failed to send Telegram notification: %s", e)
        else:
            log.info("Telegram is not configured; skipping")

        if mail_sent or telegram_sent:
            mark_notified([v["youtube_video_id"] for v in dispatch_payload])
            try:
                app_module.set_setting("pending_notify_count", "0")
            except Exception:
                pass

    # Clean up old videos when retention is enabled.
    deleted_old = cleanup_old_videos()
    if deleted_old:
        log.info("Removed %d old video(s)", deleted_old)

    summary = {
        "channels": len(channels),
        "new": len(all_new),
        "summarized": summarized_count,
        "retried_recovered": retried_count,
        "deleted_old": deleted_old,
        "mail_sent": mail_sent,
        "telegram_sent": telegram_sent,
    }
    log.info("Run complete: %s", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description="YouTube Monitor new video worker")
    parser.add_argument("--max-results", type=int, default=30)
    parser.add_argument("--no-mail", action="store_true", help="Skip sending notifications")
    parser.add_argument("--include-shorts", action="store_true", help="Save and notify about Shorts (long-form only by default)")
    parser.add_argument("--no-summary", action="store_true", help="Skip automatic summaries")
    args = parser.parse_args()

    try:
        run_once(
            max_results=args.max_results,
            send_mail=not args.no_mail,
            longform_only=not args.include_shorts,
            auto_summarize_new=not args.no_summary,
        )
    except Exception as e:
        log.exception("Fatal error while running worker: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
