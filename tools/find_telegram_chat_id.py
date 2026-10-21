"""Utility for discovering a Telegram chat ID.

Usage:
    1) Set TELEGRAM_BOT_TOKEN in .env.
    2) Find your bot in Telegram and send /start or another message.
    3) Run: python tools/find_telegram_chat_id.py
    4) Add the printed chat ID to .env as TELEGRAM_CHAT_ID.
    5) Verify with: python tools/find_telegram_chat_id.py --test
"""

import argparse
import sys
from pathlib import Path

# Force UTF-8 so output is readable in Windows consoles using legacy encodings.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telegram_notifier import (
    discover_chat_id,
    send_test_message,
    TelegramNotifierError,
    is_telegram_configured,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Send a test message using the configured token and chat ID")
    args = parser.parse_args()

    if args.test:
        if not is_telegram_configured():
            print("[X] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set in .env")
            sys.exit(1)
        try:
            send_test_message()
            print("[OK] Test message sent successfully. Check Telegram.")
        except TelegramNotifierError as e:
            print(f"[X] Failed: {e}")
            sys.exit(1)
        return

    try:
        chat_id = discover_chat_id()
        print(f"[OK] CHAT_ID = {chat_id}")
        print("Add or update this line in your .env file:")
        print(f"TELEGRAM_CHAT_ID={chat_id}")
    except TelegramNotifierError as e:
        print(f"[X] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
