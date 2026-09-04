"""Single-process Gunicorn configuration for LegiView.

LegiView intentionally owns one exclusive SQLite/archive mutation lock and one
in-process durable-run dispatcher.  More than one worker process is therefore
invalid; concurrency belongs in threads and LegiView's bounded source workers.
"""

from __future__ import annotations

import os


def _web_threads() -> int:
    try:
        value = int(os.environ.get("LEGIVIEW_WEB_THREADS", "4"))
    except ValueError as exc:
        raise RuntimeError("LEGIVIEW_WEB_THREADS must be an integer") from exc
    if not 1 <= value <= 16:
        raise RuntimeError("LEGIVIEW_WEB_THREADS must be between 1 and 16")
    return value


bind_host = os.environ.get("LEGIVIEW_HOST", "127.0.0.1").strip()
if bind_host not in {"127.0.0.1", "::1", "localhost"}:
    raise RuntimeError(
        "The supplied production configuration only permits a loopback "
        "LEGIVIEW_HOST; expose LegiView through the trusted reverse proxy"
    )

try:
    bind_port = int(os.environ.get("LEGIVIEW_PORT", "5055"))
except ValueError as exc:
    raise RuntimeError("LEGIVIEW_PORT must be an integer") from exc
if not 1 <= bind_port <= 65535:
    raise RuntimeError("LEGIVIEW_PORT must be between 1 and 65535")

bind_address = f"[{bind_host}]" if ":" in bind_host else bind_host
bind = f"{bind_address}:{bind_port}"
workers = 1
threads = _web_threads()
worker_class = "gthread"
preload_app = False
accesslog = "-"
errorlog = "-"
capture_output = True
timeout = 120
graceful_timeout = 30
