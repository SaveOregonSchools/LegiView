# Ubuntu and Nginx subpath deployment

LegiView can run behind a single trusted Nginx hop at a public path such as
`/legiview/`. Nginx removes that prefix before proxying and supplies
`X-Forwarded-Prefix: /legiview`; Werkzeug restores the prefix as WSGI
`SCRIPT_NAME`, so Flask generates prefixed links with `url_for()`.

The repository does not install or modify Nginx configuration. The existing
frontend must forward `X-Forwarded-For`, `X-Forwarded-Proto`,
`X-Forwarded-Host`, and `X-Forwarded-Prefix`. LegiView trusts exactly one value
from each of those header chains only when `LEGIVIEW_TRUST_PROXY=1`.

## Installation

These commands assume the checkout is `/opt/legiview` and the dedicated
unprivileged account is `legiview`:

```bash
cd /opt/legiview
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[server]'
sudo install -d -o root -g legiview -m 0750 /etc/legiview
sudo install -o root -g legiview -m 0640 deploy/legiview.env.example /etc/legiview/legiview.env
sudo install -o root -g root -m 0644 deploy/legiview.service /etc/systemd/system/legiview.service
```

Edit `/etc/legiview/legiview.env` before starting. At minimum:

- Replace `LEGIVIEW_TRUSTED_HOSTS` with the public hostname accepted by Nginx.
- Replace `LEGIVIEW_SECRET_KEY` with a persistent random value of at least 32
  characters (the documented generator produces 64 hexadecimal characters).
- Keep `LEGIVIEW_HOST=127.0.0.1`; the supplied Gunicorn configuration rejects
  non-loopback binds.
- Keep `LEGIVIEW_URL_PREFIX=/legiview` and ensure Nginx sends the same value in
  `X-Forwarded-Prefix`.
- Use `LEGIVIEW_SESSION_COOKIE_SECURE=1` only after the public endpoint uses
  HTTPS.
- Point the database and archive at local storage owned by the service account.
  SQLite, advisory locking, and atomic publication should not use NFS or a
  network share.

Then initialize and start the service:

```bash
sudo install -d -o legiview -g legiview /opt/legiview/data /opt/legiview/archive
sudo systemctl daemon-reload
sudo systemctl enable --now legiview
sudo systemctl status legiview
```

The production server is intentionally fixed at one Gunicorn worker. LegiView
owns a single exclusive mutation lock and an in-process durable-job dispatcher;
multiple process workers, `--preload`, overlapping instances, and rolling
multi-instance restarts are unsupported. Gunicorn threads and LegiView's
bounded OData/download workers provide concurrency inside that one process.

## Proxy and prefix guardrails

Do not enable `LEGIVIEW_TRUST_PROXY` when clients can connect directly to the
Flask/Gunicorn port. The service must remain bound to loopback so only the
trusted local Nginx process can supply forwarded headers.

Proxy mode refuses to start without `LEGIVIEW_SECRET_KEY`. A unique
`legiview_session` cookie is scoped to `/legiview` (or the configured prefix),
preventing it from colliding with other Flask applications on the same host.
The configured trusted-host list contains no implicit wildcard; unexpected
forwarded hosts receive HTTP 400.

Direct development remains unchanged apart from the dedicated default port:

```bash
python -m olis_archive serve
# http://127.0.0.1:5055/
```

With proxy mode disabled and the URL prefix left as `/`, forwarded headers are
ignored and all generated routes remain rooted at `/`.

## Verification on the target server

Before production use, run the automated suite and a small collection/download
smoke test on the server's actual local filesystem:

```bash
.venv/bin/pytest
.venv/bin/python -m olis_archive collect-measure 2025R1 HJR11
.venv/bin/python -m olis_archive archive-preflight --session 2026R1
```

The command-line collector cannot run concurrently with the web service because
both are mutating owners. Stop the service for a CLI mutation smoke test, or use
the web UI while the service is running.
