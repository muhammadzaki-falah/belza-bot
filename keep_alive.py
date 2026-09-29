"""Small Flask server used to keep the bot process reachable on port 8080."""

import os
from threading import Thread

from flask import Flask

app = Flask(__name__)


@app.get("/")
def health_check():
    return "Belza Digital bot is running.", 200


@app.get("/health")
def health():
    return {"status": "ok", "service": "belza-digital-bot"}, 200


def _run_server():
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def keep_alive():
    """Start Flask in a daemon thread so Telegram polling can run beside it."""
    thread = Thread(target=_run_server, name="keep-alive", daemon=True)
    thread.start()
    return thread