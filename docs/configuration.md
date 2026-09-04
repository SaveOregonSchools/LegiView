# Configuration

LegiView loads configuration from process environment variables and an optional
`.env` file. Values already present in the process environment take precedence over
the same names in `.env`. Settings saved in the web UI are stored in SQLite and
overlay the matching runtime settings on the next start.

The database location and effective project root remain bootstrap settings: change
them in the environment or `.env`, not in the running web application. Settings that
alter the archive root or worker graph are deliberately applied only after a safe
restart.

## Portable defaults

The checked-in `.env.example` is:

```dotenv
# Copy to .env or set these variables in your shell.
# Optional base for relative paths. If omitted, LegiView uses the application root.
# LEGIVIEW_PROJECT_ROOT=.

LEGIVIEW_DATABASE_PATH=data/legiview.sqlite3
LEGIVIEW_ARCHIVE_ROOT=archive

LEGIVIEW_REQUEST_TIMEOUT=30
LEGIVIEW_ODATA_WORKERS=1
LEGIVIEW_DOWNLOAD_WORKERS=2
LEGIVIEW_HTML_CONCURRENCY=1
LEGIVIEW_MIN_FREE_SPACE_GB=5
LEGIVIEW_INTER_REQUEST_DELAY=0.25
LEGIVIEW_HOST=127.0.0.1
LEGIVIEW_PORT=5055
LEGIVIEW_DEBUG=0

LEGIVIEW_URL_PREFIX=/
LEGIVIEW_TRUST_PROXY=0
# LEGIVIEW_TRUSTED_HOSTS=legiview.example.internal
# LEGIVIEW_SECRET_KEY=replace-with-a-persistent-random-value
LEGIVIEW_SESSION_COOKIE_SECURE=0
```

With no path variables set, the effective locations are:

```text
<application root>/data/legiview.sqlite3
<application root>/archive
```

These defaults work on Windows, Linux, and macOS. Existing absolute Phase 1 paths
continue to work and do not require a data migration.

## Project-root and relative-path rules

`LEGIVIEW_PROJECT_ROOT` establishes the base for relative database and archive
paths.

1. When it is unset, LegiView uses the repository/application root determined from
   the installed code.
2. An absolute value is expanded and normalized directly.
3. A relative value is resolved against the directory containing the loaded `.env`
   file. If no `.env` file was loaded, it is resolved against the built-in
   application root.
4. `LEGIVIEW_DATABASE_PATH` and `LEGIVIEW_ARCHIVE_ROOT` are then resolved against
   that effective project root when they are relative.

The process working directory is never used as an implicit path base. Starting the
same installation from PowerShell, Command Prompt, bash, systemd, or another folder
therefore selects the same database and archive.

Examples:

```dotenv
# Portable layout below one chosen base.
LEGIVIEW_PROJECT_ROOT=D:/LegiViewData
LEGIVIEW_DATABASE_PATH=data/legiview.sqlite3
LEGIVIEW_ARCHIVE_ROOT=archive
```

```dotenv
# Linux absolute paths remain valid.
LEGIVIEW_DATABASE_PATH=/srv/legiview/data/legiview.sqlite3
LEGIVIEW_ARCHIVE_ROOT=/srv/legiview/archive
```

The archive-root field in Settings accepts an absolute or relative value. A relative
value such as `storage/archive` is saved in that portable form and displayed with
its resolved path; it is not silently converted into a machine-specific Windows
path. `LEGIVIEW_PROJECT_ROOT` itself is read-only in Settings.

Use a directory dedicated to LegiView as the archive root. Do not select a broad
personal/shared directory (for example, Documents), a filesystem root, a file, or a
linked/reparse-point alias. On locked startup LegiView creates a small
`.legiview-archive-root` ownership marker before archive-wide maintenance. An empty
directory is eligible for initialization. A marker-less Phase 1/2 archive is adopted
only when every entry matches the exact session/measure/document-kind/source-ID layout
and at least one regular payload provides positive legacy-archive evidence; an
arbitrary or merely archive-shaped nonempty directory is rejected without deleting
or changing anything.

Settings checks a proposed archive path without creating it or adding a marker. The
marker is initialized after restart under the normal single-writer lock. If you
change the archive root, move or copy the complete existing hierarchy (including the
marker when present) without changing any relative paths, then restart LegiView.

## Free-space floor

`LEGIVIEW_MIN_FREE_SPACE_GB` is the user-facing download floor and defaults to `5`.
LegiView defines one GB for this setting as `1024 ** 3` bytes. Fractional values such
as `5.5` are supported. Before and during transfers, the downloader converts the
configured value to bytes and refuses new work that would cross the floor. A run is
paused for low space; files and retained versions are not deleted automatically.

Compatibility precedence is:

1. `LEGIVIEW_MIN_FREE_SPACE_GB`, when set;
2. otherwise legacy `LEGIVIEW_MIN_FREE_SPACE_BYTES`, when set;
3. otherwise `5` GB.

The same conversion applies to an older `minimum_free_space_bytes` row in
`app_settings` when no `minimum_free_space_gb` row exists. LegiView logs a
deprecation warning. The Phase 2 Settings form writes the GB setting and need not
delete the old generic key.

## All variables

| Variable | Default | Meaning |
| --- | ---: | --- |
| `LEGIVIEW_PROJECT_ROOT` | application root | Stable base for relative database/archive paths. Bootstrap-only. |
| `LEGIVIEW_DATABASE_PATH` | `data/legiview.sqlite3` | SQLite database, relative to the effective project root unless absolute. Bootstrap-only. |
| `LEGIVIEW_ARCHIVE_ROOT` | `archive` | Payload archive, relative to the effective project root unless absolute. |
| `LEGIVIEW_REQUEST_TIMEOUT` | `30` | OData, OLIS HTML, probe, and download timeout in seconds. |
| `LEGIVIEW_ODATA_WORKERS` | `1` | Maximum OData workers available to the one active durable run; dependency-ordered ingestion may use fewer. Allowed range 1–4. |
| `LEGIVIEW_DOWNLOAD_WORKERS` | `2` | Concurrent payload workers within a download run; allowed range 1–8. |
| `LEGIVIEW_HTML_CONCURRENCY` | `1` | Concurrent narrowly scoped OLIS HTML requests; allowed range 1–2. |
| `LEGIVIEW_MIN_FREE_SPACE_GB` | `5` | Free space to preserve, in 1024³-byte GB. |
| `LEGIVIEW_MIN_FREE_SPACE_BYTES` | unset | Deprecated fallback used only when the GB setting is absent. |
| `LEGIVIEW_INTER_REQUEST_DELAY` | `0.25` | Minimum delay between requests in seconds. |
| `LEGIVIEW_HOST` | `127.0.0.1` | Loopback-only Flask/Gunicorn bind host (`127.0.0.1`, `localhost`, or `::1`). Non-loopback values are rejected. |
| `LEGIVIEW_PORT` | `5055` | Dedicated Flask/Gunicorn loopback port. |
| `LEGIVIEW_DEBUG` | `0` | Flask debug mode; keep disabled for normal use. |
| `LEGIVIEW_URL_PREFIX` | `/` | Public application root, such as `/legiview`; also scopes the unique `legiview_session` cookie. |
| `LEGIVIEW_TRUST_PROXY` | `0` | Trust exactly one proxy hop for forwarded client, protocol, host, and prefix headers. Enable only while the backend is loopback-only. |
| `LEGIVIEW_TRUSTED_HOSTS` | none beyond loopback/bind host | Comma-separated public Host allowlist; no wildcard is added implicitly. |
| `LEGIVIEW_SECRET_KEY` | generated per start in direct mode | Persistent Flask session secret. In trusted-proxy mode it must replace the example placeholder and contain at least 32 characters. |
| `LEGIVIEW_SESSION_COOKIE_SECURE` | `0` | Send the session cookie only over HTTPS when set to `1`. |
| `LEGIVIEW_WEB_THREADS` | `4` | Thread count used by the supplied one-process Gunicorn configuration; allowed range 1–16. |

The official service endpoints and download-host allowlist have safe application
defaults. Changing source endpoints is a development/testing capability, not a way
to evade an Oregon Legislature gate, restriction, or rate limit.

## Runtime provenance

Each durable run stores a configuration snapshot including the resolved project
root, database path, resolved archive root, configured GB floor and derived byte
floor, worker counts, timeouts, delay, and source endpoints. This records the
settings under which work actually ran even if Settings later changes.

The default network identity begins with `LegiView/<version>` and includes the Save
Oregon Schools site. Keep a descriptive User-Agent when making requests.

For a production Ubuntu service behind Nginx at a subpath, see
[Ubuntu and Nginx subpath deployment](linux_nginx.md). The supplied server setup
loads its project-local `/opt/legiview/.env` through systemd and uses exactly one
Gunicorn process because the application owns one exclusive database/archive
mutation lock and one in-process durable-run dispatcher.
