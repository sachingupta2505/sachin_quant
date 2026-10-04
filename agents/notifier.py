"""
Telegram Notification Agent for SachinQuant Autonomous Trading Engine
Sends non-blocking market lifecycle and execution alerts via Telegram Bot API.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

# Ensure Windows UTF-8 console output protection without closing capture streams
for stream in (sys.stdout, sys.stderr):
    if stream and hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Ensure environment variables are loaded
load_dotenv()

logger = logging.getLogger("agent.notifier")


def resolve_chat_id_for_user(
    target_username: str = "shishilalapoopoo",
    bot_token: Optional[str] = None,
    env_file: Optional[Path | str] = None,
) -> Optional[str]:
    """
    Calls Telegram getUpdates API and searches the update list
    where message['from']['username'].lower() == target_username.lower().
    Extracts the numeric chat['id'], updates .env, and returns the chat_id.
    """
    token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "8983716841:AAFr0G5IYyeq7eJaYzUwh5NZ0197G7c6YLI")
    token = str(token).strip().strip('"').strip("'")
    target = target_username.lower().lstrip("@")
    env_path = Path(env_file or ".env")

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            logger.error(f"getUpdates returned error: {data}")
            return None

        updates = data.get("result", [])
        for update in reversed(updates):
            msg = update.get("message") or update.get("channel_post") or update.get("my_chat_member")
            if not msg:
                continue
            sender = msg.get("from", {})
            username = sender.get("username", "")
            if username and username.lower() == target:
                chat = msg.get("chat", {})
                cid = str(chat.get("id", "")).strip()
                if cid:
                    logger.info(f"Resolved numeric chat ID for @{target_username}: {cid}")
                    if env_path.exists():
                        content = env_path.read_text(encoding="utf-8")
                        if "TELEGRAM_CHAT_ID=" in content:
                            new_content = re.sub(
                                r'TELEGRAM_CHAT_ID=.*',
                                f'TELEGRAM_CHAT_ID="{cid}"',
                                content,
                            )
                        else:
                            new_content = content + f'\nTELEGRAM_CHAT_ID="{cid}"\n'
                        env_path.write_text(new_content, encoding="utf-8")
                    os.environ["TELEGRAM_CHAT_ID"] = cid
                    return cid

        # If not matched by username directly, check any private chat update
        for update in reversed(updates):
            msg = update.get("message")
            if msg and msg.get("chat", {}).get("type") == "private":
                cid = str(msg["chat"]["id"]).strip()
                if cid:
                    logger.info(f"Fallback: Auto-resolved chat ID: {cid}")
                    return cid

    except Exception as e:
        logger.error(f"Exception resolving chat ID for @{target_username}: {e}")
    return None


class TelegramNotifier:
    """
    Non-blocking Telegram Alert Dispatcher.
    Queues messages and dispatches HTTP POST requests asynchronously
    to prevent blocking execution loops or event buses.
    """

    DEFAULT_ENV_FILE = Path(".env")
    API_BASE = "https://api.telegram.org/bot{token}"

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        env_file: Optional[Path | str] = None,
    ):
        self.env_file = Path(env_file or self.DEFAULT_ENV_FILE)
        self.bot_token = self._clean_val(bot_token or os.getenv("TELEGRAM_BOT_TOKEN", ""))
        self.chat_id = self._clean_val(chat_id or os.getenv("TELEGRAM_CHAT_ID", ""))

        # If chat_id is missing or placeholder, attempt auto-resolution
        if self.is_placeholder_chat_id():
            resolved = resolve_chat_id_for_user(
                target_username="shishilalapoopoo",
                bot_token=self.bot_token,
                env_file=self.env_file,
            )
            if resolved:
                self.chat_id = resolved

        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._stop_event = threading.Event()
        self._worker_thread = threading.Thread(target=self._dispatch_loop, daemon=True, name="TelegramNotifierWorker")
        self._worker_thread.start()

    @staticmethod
    def _clean_val(val: Optional[str]) -> str:
        if not val:
            return ""
        val = str(val).strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1].strip()
        return val

    def is_placeholder_chat_id(self, cid: Optional[str] = None) -> bool:
        val = cid if cid is not None else self.chat_id
        if not val:
            return True
        return "<" in val or ">" in val or "YAHAN_APNA" in val or "your_chat_id" in val

    def send_message_sync(self, text: str, parse_mode: str = "HTML") -> dict[str, Any]:
        """Synchronously send message via Telegram Bot API."""
        if not self.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not configured.")

        if self.is_placeholder_chat_id():
            resolved = resolve_chat_id_for_user(
                target_username="shishilalapoopoo",
                bot_token=self.bot_token,
                env_file=self.env_file,
            )
            if resolved:
                self.chat_id = resolved
            else:
                raise ValueError(
                    f"Invalid TELEGRAM_CHAT_ID '{self.chat_id}'. "
                    "Please send /start to @sachin_quant_9821_bot on Telegram or set TELEGRAM_CHAT_ID in .env."
                )

        url = f"{self.API_BASE.format(token=self.bot_token)}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        if not resp.ok or not data.get("ok"):
            logger.error(f"Failed to send Telegram message: {data}")
            resp.raise_for_status()
        return data

    def send_message(self, text: str, parse_mode: str = "HTML") -> None:
        """Enqueue message for non-blocking asynchronous transmission."""
        self._queue.put((text, parse_mode))

    def _dispatch_loop(self) -> None:
        """Background worker thread draining the alert queue."""
        while not self._stop_event.is_set():
            try:
                text, parse_mode = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                self.send_message_sync(text, parse_mode=parse_mode)
            except Exception as e:
                logger.error(f"Error in Telegram dispatch worker: {e}")
            finally:
                self._queue.task_done()

    def stop(self) -> None:
        self._stop_event.set()
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)

    # -------------------------------------------------------------
    # Explicit Lifecycle Alert Handlers
    # -------------------------------------------------------------
    def notify_system_live(self) -> None:
        """09:14 IST: System Live & Broker Connected"""
        self.send_message("🚀 System Live & Broker Connected")

    def notify_initial_balance(self, ib_high: float, ib_low: float) -> None:
        """09:45 IST: Initial Balance Formed"""
        self.send_message(f"📊 Initial Balance Set: High {ib_high:.1f} | Low {ib_low:.1f}")

    def notify_order_executed(self, spread_type: str, max_risk: float) -> None:
        """Order Execution alert"""
        self.send_message(f"🎯 Order Executed: {spread_type} | Risk: INR {max_risk:.1f}")

    def notify_order_blocked(self, reason: str) -> None:
        """Auditor Rejection alert"""
        self.send_message(f"⚠️ Order Blocked: {reason}")

    def notify_positions_squared_off(self) -> None:
        """15:10 IST: Square-off alert"""
        self.send_message("🔒 All Positions Auto Squared-Off")

    def notify_eod_summary(self, count: int, pnl: float) -> None:
        """15:30 IST: EOD Summary alert"""
        self.send_message(f"🏁 EOD Summary: Total Trades: {count} | Daily PnL: INR {pnl:.1f}")

    # -------------------------------------------------------------
    # Blackboard Bus Integration
    # -------------------------------------------------------------
    def handle_bus_event(self, topic: str, payload: dict[str, Any]) -> None:
        """Routes a blackboard bus event to the corresponding Telegram notification."""
        if topic in ("SYSTEM_LIVE", "SYSTEM_START", "BROKER_CONNECTED"):
            self.notify_system_live()

        elif topic in ("INITIAL_BALANCE_LOCKED", "IB_LOCKED", "INITIAL_BALANCE_SET"):
            ib_high = float(payload.get("ib_high", 0.0))
            ib_low = float(payload.get("ib_low", 0.0))
            self.notify_initial_balance(ib_high=ib_high, ib_low=ib_low)

        elif topic in ("ORDER_EXECUTED", "ORDER_EXECUTED_ALERT"):
            spread_type = payload.get("spread_type", "SPREAD")
            max_risk = float(payload.get("max_risk_inr", payload.get("max_risk", 0.0)))
            self.notify_order_executed(spread_type=spread_type, max_risk=max_risk)

        elif topic in ("ORDER_BLOCKED", "ORDER_REJECTED", "AUDIT_REJECTED"):
            reason = payload.get("reason", payload.get("veto_reason", "Compliance rejection"))
            self.notify_order_blocked(reason=reason)

        elif topic in ("POSITIONS_SQUARED_OFF", "SQUARE_OFF_ALERT"):
            self.notify_positions_squared_off()

        elif topic in ("EOD_SUMMARY", "DAILY_RECONCILIATION"):
            count = int(payload.get("count", payload.get("total_trades", 0)))
            pnl = float(payload.get("pnl", payload.get("daily_pnl", 0.0)))
            self.notify_eod_summary(count=count, pnl=pnl)


def notifier_worker(
    bus: Any,
    stop_event: threading.Event,
    notifier: Optional[TelegramNotifier] = None,
) -> None:
    """
    Background worker thread monitoring bus.py for lifecycle events.
    Consumes events targeted to Notifier or queries topic alerts.
    """
    tg = notifier or TelegramNotifier()
    topics = [
        "SYSTEM_LIVE",
        "INITIAL_BALANCE_LOCKED",
        "ORDER_EXECUTED_ALERT",
        "ORDER_BLOCKED",
        "POSITIONS_SQUARED_OFF",
        "EOD_SUMMARY",
        "NOTIFIER_ALERT",
    ]

    while not stop_event.is_set():
        for topic in topics:
            events = bus.consume(topic=topic, target="Notifier")
            for ev in events:
                try:
                    tg.handle_bus_event(topic=ev.topic, payload=ev.payload)
                    bus.update_status(ev.id, ev.status if hasattr(ev, "status") else "COMPLETED")
                except Exception as e:
                    logger.error(f"Error handling event #{ev.id} in notifier_worker: {e}")
        time.sleep(0.1)


def main():
    parser = argparse.ArgumentParser(description="SachinQuant Telegram Notifier Service")
    parser.add_argument("--test", action="store_true", help="Send test alert to verify Telegram credentials")
    parser.add_argument("--resolve", action="store_true", help="Auto-resolve numeric chat ID for target user")
    parser.add_argument("--username", type=str, default="shishilalapoopoo", help="Target Telegram username")
    parser.add_argument("--chat-id", type=str, default=None, help="Explicit Telegram chat ID override")
    args = parser.parse_args()

    notifier = TelegramNotifier(chat_id=args.chat_id)

    if args.resolve or args.test:
        resolved_cid = resolve_chat_id_for_user(target_username=args.username)
        if resolved_cid:
            notifier.chat_id = resolved_cid
            print(f"[SUCCESS] Resolved Chat ID for @{args.username}: {resolved_cid}")

    if args.test:
        test_msg_connect = f"✅ Sachin Quant Alerts Connected Successfully for @{args.username}!"
        test_msg_active = "✅ Sachin Quant Telegram Alerts Active!"

        print(f"Sending test alerts to chat_id '{notifier.chat_id}'...")
        try:
            res1 = notifier.send_message_sync(test_msg_connect)
            msg_id1 = res1.get("result", {}).get("message_id")
            print(f"[SUCCESS] Delivered connection message! (Message ID: {msg_id1})")

            res2 = notifier.send_message_sync(test_msg_active)
            msg_id2 = res2.get("result", {}).get("message_id")
            print(f"[SUCCESS] Delivered active ping alert! (Message ID: {msg_id2})")
            print(f"[VERIFIED] Both alerts received by Chat ID {notifier.chat_id}")
        except Exception as e:
            print(f"[ERROR] Failed to send Telegram test alert: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
