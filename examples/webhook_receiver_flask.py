#!/usr/bin/env python3
"""Minimal Flask receiver for NapcatKeeper webhook notifications."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

HOST = os.environ.get("NAPCAT_WEBHOOK_HOST", "0.0.0.0")
PORT = int(os.environ.get("NAPCAT_WEBHOOK_PORT", "8787"))
LOG_FILE = Path(
    os.environ.get("NAPCAT_WEBHOOK_LOG_FILE", "napcat_webhook_receiver.log")
)

app = Flask(__name__)


def _append_log(payload: dict[str, Any]) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.post("/napcat-webhook")
def napcat_webhook():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "invalid json"}), 400

    record = {
        "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": data.get("event"),
        "time": data.get("time"),
        "account": data.get("account"),
        "status": data.get("status"),
        "message": data.get("message"),
        "raw": data,
    }
    _append_log(record)

    print(
        f"[NapcatWebhook] event={record['event']} "
        f"time={record['time']} account={record['account']}"
    )
    return jsonify({"ok": True})


if __name__ == "__main__":
    print(
        "NapcatKeeper webhook receiver is listening on "
        f"http://{HOST}:{PORT}/napcat-webhook"
    )
    print(f"Logs will be appended to: {LOG_FILE}")
    app.run(host=HOST, port=PORT)
