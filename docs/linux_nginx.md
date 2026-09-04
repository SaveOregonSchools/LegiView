# Fresh Ubuntu 24.04 installation behind Nginx

This guide installs LegiView as a dedicated local service at `/opt/legiview` and
publishes it through an existing Nginx server at `/legiview/`. Application code,
the virtual environment, application configuration, SQLite data, and downloaded
files all remain beneath `/opt/legiview`.

The only system-wide registration is a systemd link for the checked-in service
unit. This repository does not install, replace, or edit an Nginx configuration;
the required location block is shown for an administrator to add to the existing
server block.

LegiView listens only on `127.0.0.1`. Nginx removes `/legiview/` before proxying
and supplies `X-Forwarded-Prefix: /legiview`. Werkzeug restores that prefix as
WSGI `SCRIPT_NAME`, so Flask generates prefixed links with `url_for()`.

## Assumptions

- You have a sudo-capable administrator account.
- Nginx already serves the intended hostname.
- `/opt/legiview` does not yet exist. If it does, use the update procedure below
  rather than cloning over it.
- TCP port `5055` is available on loopback. Another unused port can be selected
  consistently in `.env` and the Nginx `proxy_pass` directive.

The commands below use `brad` as the deployment-specific browser hostname only
where a concrete health-check value is required. The application and supplied
service do not hard-code that name.

## 1. Install operating-system prerequisites

```bash
sudo apt update
sudo apt install --yes ca-certificates curl git iproute2 python3 python3-venv
```

Nginx is not included in that command because this deployment targets the existing
frontend. Before choosing the default backend port, check for another listener:

```bash
sudo ss -ltnp | grep ':5055 '
```

No output means the default port is currently unused.

## 2. Create the service account and clone LegiView

First check that the dedicated account does not already exist:

```bash
getent passwd legiview
```

On a fresh host, that command prints nothing. Create a system account with no login
shell, then clone the repository. The code remains root-owned; only the runtime
directories created later are writable by the service.

```bash
sudo useradd --system --user-group --no-create-home --home-dir /opt/legiview --shell /usr/sbin/nologin legiview
sudo git clone --branch main --single-branch https://github.com/SaveOregonSchools/LegiView.git /opt/legiview
cd /opt/legiview
```

If the GitHub repository requires authentication, clone it using the server's
approved authenticated Git transport, then apply the same ownership and mode.

## 3. Create the virtual environment and run the offline suite

Do not copy a Windows virtual environment to Linux. Build a new one on the server:

```bash
cd /opt/legiview
sudo python3 -m venv .venv
sudo .venv/bin/python -m pip install --upgrade pip
sudo .venv/bin/python -m pip install -e '.[server,test]'
sudo .venv/bin/python -m pip check
sudo chown -R root:legiview /opt/legiview
sudo chmod -R a+rX,go-w /opt/legiview
sudo chmod 0755 /opt/legiview
```

The `test` extra is installed because this fresh-install procedure verifies the
checkout before starting it. Run the network-independent suite before creating the
production `.env`, so production proxy settings cannot influence test requests:

```bash
sudo -u legiview env PYTHONDONTWRITEBYTECODE=1 \
  /opt/legiview/.venv/bin/python -m pytest -p no:cacheprovider
```

The ordinary automated suite does not contact Oregon Legislature services.

## 4. Create writable storage and project-local configuration

```bash
cd /opt/legiview
sudo install -d -o legiview -g legiview -m 0750 data archive
sudo install -o root -g legiview -m 0640 deploy/legiview.env.example .env
python3 -c 'import secrets; print(secrets.token_hex(32))'
sudoedit /opt/legiview/.env
```

Paste the generated 64-character value into `LEGIVIEW_SECRET_KEY`. For the current
HTTP deployment at `http://brad/legiview/`, the important values are:

```dotenv
LEGIVIEW_PROJECT_ROOT=/opt/legiview
LEGIVIEW_DATABASE_PATH=data/legiview.sqlite3
LEGIVIEW_ARCHIVE_ROOT=archive
LEGIVIEW_HOST=127.0.0.1
LEGIVIEW_PORT=5055
LEGIVIEW_URL_PREFIX=/legiview
LEGIVIEW_TRUST_PROXY=1
LEGIVIEW_TRUSTED_HOSTS=brad
LEGIVIEW_SECRET_KEY=replace-this-with-the-generated-value
LEGIVIEW_SESSION_COOKIE_SECURE=0
```

Use a comma-separated list when more than one exact browser hostname is valid.
Do not enter a URL, wildcard, path, or scheme in `LEGIVIEW_TRUSTED_HOSTS`. Change
`LEGIVIEW_SESSION_COOKIE_SECURE` to `1` when the public endpoint uses HTTPS.

Keep the database and archive on local storage. SQLite, advisory locking, and
atomic file publication should not use NFS or another network share. The supplied
unit's `ProtectHome=true` also means runtime paths should not be placed under
`/home` without deliberately revising the sandbox policy.

## 5. Register and start the systemd service

The unit remains in the project at `deploy/legiview.service`. `systemctl link`
registers that exact file instead of maintaining a second copied unit:

```bash
sudo systemctl link /opt/legiview/deploy/legiview.service
sudo systemd-analyze verify /opt/legiview/deploy/legiview.service
sudo systemctl daemon-reload
sudo systemctl enable --now legiview.service
sudo systemctl status legiview.service --no-pager
```

Confirm that Gunicorn is listening only on loopback:

```bash
sudo ss -ltnp | grep ':5055 '
```

The listener must be `127.0.0.1:5055`, not `0.0.0.0:5055`. If startup fails, read
the service log:

```bash
sudo journalctl -u legiview.service -n 100 --no-pager
```

Before changing Nginx, exercise the backend with the same trusted headers Nginx
will provide:

```bash
curl --fail --show-error --silent \
  --header 'X-Forwarded-For: 127.0.0.1' \
  --header 'X-Forwarded-Host: brad' \
  --header 'X-Forwarded-Proto: http' \
  --header 'X-Forwarded-Prefix: /legiview' \
  http://127.0.0.1:5055/health
```

The response should be JSON with `"status":"ok"` and the current schema version.
Do not add a firewall rule for port `5055`; it is deliberately a loopback-only
backend and Nginx is its public entry point.

## 6. Add the location to the existing Nginx server

Add these locations inside the existing `server { ... }` block that handles the
public hostname. The trailing slash on `proxy_pass` is significant: it removes the
matched `/legiview/` prefix before forwarding the request to Flask.

```nginx
location = /legiview {
    return 308 /legiview/;
}

location ^~ /legiview/ {
    proxy_pass http://127.0.0.1:5055/;
    proxy_http_version 1.1;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix /legiview;
}
```

Validate the complete existing Nginx configuration before reloading it:

```bash
sudo nginx -t
sudo systemctl reload nginx
curl --fail --show-error http://brad/legiview/health
curl --fail --silent http://brad/legiview/ | grep '/legiview/static/'
curl --fail --show-error --silent --output /dev/null \
  http://brad/legiview/static/app.css
```

Then open `http://brad/legiview/` in a browser. Generated navigation, forms,
redirects, static assets, exports, APIs, and downloads should retain the prefix.

## Service operation

```bash
sudo systemctl status legiview.service --no-pager
sudo systemctl restart legiview.service
sudo systemctl stop legiview.service
sudo systemctl start legiview.service
sudo journalctl -u legiview.service -f
```

LegiView intentionally uses one Gunicorn worker process. It owns one exclusive
SQLite/archive mutation lock and one in-process durable-job dispatcher. Gunicorn
threads and LegiView's bounded source workers provide concurrency inside that
process; multiple Gunicorn workers, `--preload`, and overlapping service/CLI
writers are unsupported.

## Updating an existing `/opt/legiview` installation

Stop the only writer first:

```bash
sudo systemctl stop legiview.service
```

With the service stopped, back up the SQLite database and irreplaceable archive
data according to the host's normal backup procedure. Do not continue until that
backup has completed. Then update with a fast-forward pull, refresh installed
dependencies, reload the linked unit in case it changed, and restart:

```bash
sudo sh -c 'umask 0022 && git -C /opt/legiview pull --ff-only origin main'
cd /opt/legiview
sudo sh -c 'umask 0022 && cd /opt/legiview && .venv/bin/python -m pip install -e ".[server,test]"'
sudo systemctl daemon-reload
sudo systemctl start legiview.service
sudo systemctl status legiview.service --no-pager
curl --fail --show-error http://brad/legiview/health
```

Do not replace `/opt/legiview/.env` during an update. Database migrations run at
application startup.

## CLI smoke tests and locking

The mutating CLI and web service deliberately share one exclusive ownership lock.
Stop the service and run mutating commands as the service account so new files keep
the correct ownership:

```bash
sudo systemctl stop legiview.service
sudo -u legiview /opt/legiview/.venv/bin/python -m olis_archive collect-measure 2014R1 HB4111
sudo -u legiview /opt/legiview/.venv/bin/python -m olis_archive archive-preflight --session 2014R1
sudo systemctl start legiview.service
```

Live collection must follow the Oregon Legislature acceptable-use agreement. An
all-history inventory is a full refresh and should be run only within the published
full-refresh window and frequency limit.

## Proxy and prefix security notes

- Do not enable `LEGIVIEW_TRUST_PROXY` if clients can reach the backend port.
- LegiView trusts exactly one forwarded proxy hop and requires the literal configured
  `X-Forwarded-Prefix` value.
- Proxy mode refuses to start without a non-placeholder persistent secret and an
  explicit trusted-host allowlist.
- The `legiview_session` cookie is scoped to `/legiview`, preventing collisions with
  Flask applications mounted at other paths on the same hostname.
- With proxy mode disabled and `LEGIVIEW_URL_PREFIX=/`, direct development continues
  to work at `http://127.0.0.1:5055/`.
