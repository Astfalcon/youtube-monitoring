"""Telegram Bot notification module."""

import html
import logging
import os
import time
from typing import List, Dict

import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_BASE = "https://api.telegram.org"

MAX_MESSAGE_LEN = 4000  # Telegram limit is 4096; leave some margin.
INTER_MESSAGE_DELAY_SEC = 0.5  # Stay below Telegram's recommended per-chat rate.

log = logging.getLogger(__name__)


class TelegramNotifierError(Exception):
    pass


def is_telegram_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def send_new_videos_telegram(new_videos: List[Dict]) -> None:
    if not new_videos:
        return

    if not is_telegram_configured():
        raise TelegramNotifierError(
            "Telegram is not fully configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env."
        )

    # Send one message per video because summaries can be long.
    # Pause briefly between messages to avoid HTTP 429 rate limits.
    for i, v in enumerate(new_videos):
        text = _format_video_message(v)
        _send_message(text)
        if i < len(new_videos) - 1:
            time.sleep(INTER_MESSAGE_DELAY_SEC)


def _format_video_message(v: Dict) -> str:
    """Format an HTML-mode message and escape all body text."""
    title = v.get("title", "")
    channel = v.get("channel_title", "?")
    url = v.get("url", "")
    published = (v.get("published_at") or "")[:16].replace("T", " ")
    summary = (v.get("summary_text") or "").strip()

    parts = [
        f"🎥 <b>{_escape_html(channel)}</b>",
        f"<i>{_escape_html(published)}</i>",
        "",
        f"<b>{_escape_html(title)}</b>",
        _escape_html(url),
    ]
    if summary:
        summary_short = summary[:MAX_MESSAGE_LEN - 500]
        parts.extend(["", "📝 Summary:", _escape_html(summary_short)])

    return "\n".join(parts)


def _escape_html(text: str) -> str:
    """Escape <, >, and & for Telegram HTML mode; accept None safely."""
    return html.escape(text or "", quote=False)


def _send_message(text: str) -> None:
    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    # Send the message once.
    try:
        resp = requests.post(url, json=payload, timeout=15)
    except Exception as e:
        raise TelegramNotifierError(f"Telegram request failed: {e}")

    # On rate limit (429), wait for Telegram's retry_after value and try again.
    if resp.status_code == 429:
        try:
            retry_after = int(resp.json().get("parameters", {}).get("retry_after", 3))
        except Exception:
            retry_after = 3
        log.warning("Telegram rate-limited, sleeping %ds", retry_after + 1)
        time.sleep(retry_after + 1)
        try:
            resp = requests.post(url, json=payload, timeout=15)
        except Exception as e:
            raise TelegramNotifierError(f"Telegram retry after HTTP 429 failed: {e}")

    # For other failures, retry once without parse_mode (plain text).
    if resp.status_code != 200:
        payload.pop("parse_mode", None)
        try:
            resp2 = requests.post(url, json=payload, timeout=15)
        except Exception as e:
            raise TelegramNotifierError(f"Telegram retry failed: {e}")
        if resp2.status_code != 200:
            raise TelegramNotifierError(
                f"Telegram send failed: {resp.status_code} {resp.text[:300]}"
            )


def send_test_message() -> str:
    """Send a test message to verify the bot and chat ID."""
    if not is_telegram_configured():
        raise TelegramNotifierError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set.")
    _send_message("✅ YouTube Monitor connection test succeeded.")
    return "ok"


def discover_chat_id() -> str:
    """Find and return the chat ID after the user sends a message to their bot."""
    if not TELEGRAM_BOT_TOKEN:
        raise TelegramNotifierError("TELEGRAM_BOT_TOKEN is not set.")
    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    resp = requests.get(url, timeout=15)
    if resp.status_code != 200:
        raise TelegramNotifierError(f"getUpdates failed: {resp.status_code} {resp.text}")
    data = resp.json()
    for upd in reversed(data.get("result", [])):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid:
            return str(cid)
    raise TelegramNotifierError(
        "No recent messages found. Find your bot in Telegram, send /start or another message, and try again."
    )
