"""Webhook forwarding Flask application.

This module provides a Flask app that records incoming webhook payloads
into per-webhook JSONL log files and relays the payloads to a Telegram
chat via the Pyrogram client.

The previous version of this module eagerly started the Pyrogram client at
import time.  That approach works when the module is executed directly,
but it breaks when the module is imported by a WSGI server or when Flask's
reloader spawns a secondary process: each import attempt tried to start a
second Pyrogram client against the same session.  Pyrogram then raised
`RuntimeError` ("Event loop is closed" or "Client has not been started")
as the loop was not yet running in the new interpreter.

The client is now started lazily in a thread-safe manner the first time it
is required.  The atexit hook guarantees that the session is closed cleanly
when the process exits.  These changes ensure that both the Flask app and
Pyrogram operate reliably in long-running production environments.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List

from flask import Flask, jsonify, request
from pyrogram import Client

app = Flask(__name__)

api_id = 22722906
api_hash = "90e4ccdeff67faa84af9ffe678bc9da3"
phone = "+919894094567"
group_chat_id = -5020419953

WEBHOOKS: List[str] = [
    "webhook1",
    "webhook2",
]

DOWNLOAD_SECRET = "my_download_secret_9876"

# --- Pyrogram client management ------------------------------------------------

telegram_client = Client("my_account", api_id=api_id, api_hash=api_hash, phone_number=phone)

_client_lock = threading.Lock()
_client_started = False
_loop_run_lock = threading.Lock()
_TG_LOOP: asyncio.AbstractEventLoop | None = None

IST = timezone(timedelta(hours=5, minutes=30))


def _ensure_client_started() -> None:
    """Start the global Pyrogram client once in a threadsafe manner."""
    global _client_started, _TG_LOOP

    if _client_started:
        return

    with _client_lock:
        if _client_started:
            return
        telegram_client.start()
        _TG_LOOP = telegram_client.loop
        _client_started = True


@atexit.register
def _stop_client() -> None:
    """Ensure the Pyrogram client is closed on interpreter shutdown."""
    if not _client_started:
        return
    try:
        telegram_client.stop()
    except RuntimeError:
        # Ignore cases where the loop has already been shut down.
        pass


# --- Utility helpers ----------------------------------------------------------


def purge_old_entries(logfile: Path) -> None:
    """Keep only log entries from the last seven days in *logfile*."""
    if not logfile.exists():
        return

    now = datetime.now(timezone.utc)
    new_logs: List[Dict[str, Any]] = []

    with logfile.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts_str = entry.get("timestamp")
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except ValueError:
                    try:
                        ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                    except ValueError:
                        ts = now
            else:
                ts = now

            if (now - ts).days <= 7:
                new_logs.append(entry)

    with logfile.open("w", encoding="utf-8") as f:
        for entry in new_logs:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _format_timestamp_ist(value: Any) -> str:
    """Return *value* converted to IST in "hh:mm:ss dd/mm/yyyy" format."""

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(value, fmt)
                    break
                except ValueError:
                    continue
            else:
                return value
    else:
        return str(value)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(IST).strftime("%H:%M:%S %d/%m/%Y")


def _build_curated_message(data: Dict[str, Any]) -> str:
    """Create the formatted Telegram message from *data*."""

    excluded_keys = {"webhook_url", "alert_name", "scan_name", "scan_url"}
    lines = ["PriceAction Alert (Buy or Sell detected)", ""]

    for key, value in data.items():
        if key in excluded_keys:
            continue

        if value is None:
            continue

        label = key.replace("_", " ").title()
        if key == "timestamp":
            label = "Timestamp (IST)"
            display_value = _format_timestamp_ist(value)
        elif isinstance(value, (dict, list)):
            display_value = json.dumps(value, ensure_ascii=False)
        else:
            display_value = str(value)

        if display_value:
            lines.append(f"{label}: {display_value}")

    return "\n".join(lines)


def send_telegram_message(data: Dict[str, Any]) -> None:
    """Schedule send_message on Pyrogram's loop to avoid loop mismatch."""
    _ensure_client_started()
    assert _TG_LOOP is not None, "Telegram event loop not initialised"

    message = _build_curated_message(data)

    async def _send() -> None:
        await telegram_client.send_message(chat_id=group_chat_id, text=message)

    try:
        if _TG_LOOP.is_running():
            asyncio.run_coroutine_threadsafe(_send(), _TG_LOOP).result()
        else:
            with _loop_run_lock:
                _TG_LOOP.run_until_complete(_send())
    except Exception as exc:  # pragma: no cover - logging only
        print("Telegram message send failed:", repr(exc))


# --- Flask route factories ----------------------------------------------------


def make_post_handler(logfile_path: Path) -> Callable[[], tuple[str, int]]:
    def handler() -> tuple[str, int]:
        data = request.get_json(force=True, silent=False) or {}
        if "timestamp" not in data:
            data["timestamp"] = datetime.now(timezone.utc).isoformat()

        purge_old_entries(logfile_path)
        with logfile_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

        send_telegram_message(data)
        return "", 200

    return handler


def make_get_handler(logfile_path: Path) -> Callable[[], tuple[Any, int]]:
    def handler():
        token = request.args.get("token")
        if token != DOWNLOAD_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

        purge_old_entries(logfile_path)
        if not logfile_path.exists():
            return jsonify([]), 200

        logs: List[Dict[str, Any]] = []
        with logfile_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    logs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return jsonify(logs), 200

    return handler


def make_clear_handler(logfile_path: Path) -> Callable[[], tuple[Any, int]]:
    def handler():
        token = request.args.get("token")
        if token != DOWNLOAD_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

        logfile_path.write_text("", encoding="utf-8")
        return jsonify({"status": f"{logfile_path.name} cleared"}), 200

    return handler


for webhook_name in WEBHOOKS:
    logfile = Path(f"{webhook_name}_logs.json")
    app.add_url_rule(
        f"/{webhook_name}",
        f"post_{webhook_name}",
        make_post_handler(logfile),
        methods=["POST"],
    )
    app.add_url_rule(
        f"/{webhook_name}/logs",
        f"getlogs_{webhook_name}",
        make_get_handler(logfile),
        methods=["GET"],
    )
    app.add_url_rule(
        f"/{webhook_name}/clearlogs",
        f"clearlogs_{webhook_name}",
        make_clear_handler(logfile),
        methods=["POST"],
    )


@app.route("/", methods=["GET"])
def home() -> tuple[str, int]:
    return "Flask app is running", 200


def main() -> None:
    """Entry point used when running the module directly."""
    _ensure_client_started()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
