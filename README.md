# HushFilter Python Implementation

Python implementation for checking HushFilter bloom filters for credential membership.

## Quick Start

**You'll need:** Docker with Compose and an [nWebbed API key](#acquiring-your-nwebbed-api-key). The initial filter download can exceed **50 GB**, so allow enough disk space and time for the first sync.

### 1. Prepare your configuration

Clone this repository and open a terminal in its root directory. Create your local configuration files:

```bash
cp .env.EXAMPLE .env
cp manifest.json.EXAMPLE manifest.json
```

Open `.env` and set your API key:

```dotenv
NWEBBED_API_KEY=your_api_key_here
```

The client obtains its R2 download credentials automatically from the configured nWebbed API.

### 2. Build and start the client

Run these commands in order, continuing only after each succeeds. The certificate initializer must finish before the application containers start.

```bash
docker compose build
docker compose run --rm tls-cert-init
docker compose up -d
```

For certificate setup, renewal, or startup recovery, see [Docker TLS and Internal mTLS](#docker-tls-and-internal-mtls).

### 3. Download and load your filters

Open [Filter Sync](https://localhost/ui-sync/) and click **sync, update manifest, and reload filters**. Follow the **Progress** panel and wait for the operation to finish.

### 4. Check a credential

Open [Credential Check](https://localhost/ui-check/) and submit the prepopulated test credentials. After the filters are loaded, the result should be **TRUE**.

| Where to go next | Link |
| --- | --- |
| Client home | [https://localhost/](https://localhost/) |
| Schedule daily updates | [Filter Sync](https://localhost/ui-sync/) |
| Configure email notifications | [Alerts](https://localhost/ui-alerts/) |
| Explore the API | [Swagger Docs](https://localhost/docs) |

## Acquiring your nWebbed API key

1. Make sure you have a Hush Filters license for your **organization**.
2. Sign in to the **Owner** account, or a **Member** account with delegated permissions for Hush management.
3. Go to **Product Configs** → **Hushfilters**, using the link for your region:

   - [Global product config page](https://www.nwebbed.com/dashboard/product-configs/hushfilters)
   - [EU product config page](https://eu.nwebbed.com/dashboard/product-configs/hushfilters)

4. Scroll to **Inventory** and click **Issue Key**. The API key will be issued and displayed at the top of the current screen; scroll up to see it.
5. Choose a **Slot Name**, enter the IP address you will be running Hush on, then click **Save Slot**.
6. If you don't know your IP address, attempt to use Hush with the API key you just generated, then refresh the Hushfilters config page. Click **Show Usage** to see the IP address that attempted to connect, and whitelist that IP address in your slot.

Add the issued key to `NWEBBED_API_KEY` in your `.env` file as shown in [Quick Start](#quick-start).

## Supported Inputs

The CLI and API accept only:

- `username + password`
- precomputed SHA-256 hash (64-char hex digest of `username+nWebbed+password`)
- CLI: batch credentials from TSV
- API: batch credentials in the form of username + password, and SHA-256 hashes

## Outbound network allowlist

Allow outbound connections from the client/container to:

- `nwebbed.com` and the nWebbed subdomain configured in `NWEBBED_API_URL` (for example `tarsus.nwebbed.com`), on HTTPS TCP port 443. If your firewall supports wildcard rules, allow `*.nwebbed.com` as well.
- `*.r2.cloudflarestorage.com` on HTTPS TCP port 443 for filter manifest and archive downloads.
- **Your configured SMTP server hostname and TCP port** for email alerts: typically 587 for STARTTLS, 465 for TLS from connection, or your relay's configured port (often 25). There is no separate “Python sendmail” domain to allowlist; Python connects directly to the SMTP host you enter on the Alerts page.

Ensure the container can resolve these hostnames through your DNS service.

## Email alerts

Open **Alerts** at `/ui-alerts/` to enter the SMTP hostname, port, TLS mode, optional username/password, sender address, and recipient addresses. Save the settings and use **Send test email** to verify delivery. A test uses saved settings and can be sent while automatic alerts are disabled. Authenticated SMTP requires TLS; use the no-TLS option only for a trusted unauthenticated relay. Sending uses Python's standard-library [`smtplib.sendmail`](https://docs.python.org/3/library/smtplib.html#smtplib.SMTP.sendmail), so no system sendmail installation is needed.

Enable any combination of:

- **Sync failures:** manual and scheduled remote-manifest, download, extraction, and checksum failures.
- **Filter loading and manifest failures:** startup loading, partial filter loading, local manifest generation, and reload failures.
- **No filters loaded:** detected at startup, reload, or when saving enabled alert settings.
- **Critical service errors:** unhandled request/background errors and other HTTP 5xx responses. Sync and SMTP failures are handled separately to avoid duplicate notifications and email loops.
- **Certificate expiry and validity:** expiring, expired, not-yet-valid, missing, or unreadable customer-facing/internal TLS certificates, including supplied chains. Choose the warning window on Alerts (default 30 days). Existing saved alert selections are retained; explicitly select this new category to enable its emails.

Alerts are disabled by default. The first qualifying event queues an email; repeat attempts in the same category are suppressed for the configured cooldown (default 15 minutes), including scheduled sync retries. Cooldowns and the latest 20 delivery attempts are kept in memory and reset on restart. Saving settings resets cooldowns. SMTP failures appear on the Alerts page and in diagnostic logs without interrupting sync or credential checks. Queued alerts use the current saved configuration; disabling a category cancels its queued sends. Email content includes the client hostname, UTC time, and issue category, without request bodies, credentials, or raw exception text.

Settings persist in `filters/.alerts.json` (override with `HUSHCLIENT_ALERT_SETTINGS_PATH`), covered by the existing Docker filters volume. The SMTP password is stored locally in this file with owner-only permissions on POSIX and is never returned by the settings API. Protect the file and its backups; the password is not encrypted at rest. Leave the password field blank to retain it, or select **Clear saved password** to remove it. Keep the management UI/API accessible only to trusted administrators.

Alerts cover issues detected while this Python process is running. A stopped container, host/network outage, or abrupt process termination requires external monitoring. Successful SMTP acceptance does not guarantee delivery to the recipient's inbox.

## UI navigation and progress

The root URL (`/`) displays the Hushfilters home page, with an overview, first-sync guidance, and links to each tool. The previous JSON API/endpoint listing is now at `/endpoints`; integrations that read that listing should use the new path.

Credential Check, Filter Sync, Logs, Alerts, and Swagger **Docs** (`/docs`) use the same navigation header on every page. The buttons occupy their own row and retain the same order and layout at each screen size. Click the nWebbed brand in the header to return home. Every page includes links to both Global and EU nWebbed Hushfilters dashboards.

The Filter Sync **Progress** panel shows remote-manifest fetching, checking/downloading archives, extracting filters, checksum verification, local manifest generation, and reload. Download progress uses bytes when the server supplies a size; extraction and verification use file counts within each archive. Unknown totals show an indeterminate bar. Already-current filters skip extraction and verification of downloaded archives. Manual steps show only their relevant phases, and failed operations retain the failing phase.

Operational log panels can be collapsed. When expanded, **Auto-scroll to latest entries** is enabled by default; turn it off to read earlier entries. The Logs panel refreshes every two seconds while expanded and visible, and sync status refreshes every second while visible.

## Configuration and examples

### Test mode and sample credentials

To use the bundled test filters, set `HUSHFILTER_TEST_MODE=1` in `.env`.

These sample combinations should return **TRUE** in both test and production modes once the corresponding filters are loaded:

| Username | Password |
| --- | --- |
| `testusername1@nwebbed.com` | `testpassword1` |
| `testusername2@nwebbed.com` | `testpassword2` |
| `testusername3@nwebbed.com` | `testpassword3` |
| `testusername4@nwebbed.com` | `testpassword4` |

### Automatic updates

Enable the daily updater with `AUTO_UPDATE_FILTERS=1` and choose the container-local 24-hour start time using `AUTO_UPDATE_TIME`.

| Setting | Behavior |
| --- | --- |
| `AUTO_UPDATE_TIME=23` | Run the full sync/apply workflow at 11pm each day. |
| `AUTO_UPDATE_TIME=2` | Run the full sync/apply workflow at 2am each day. |
| `AUTO_UPDATE_FILTERS=0` | Disable scheduled automatic updates. |

The [Filter Sync UI](https://localhost/ui-sync/) can enable or disable automatic updates and change the start hour without restarting the service. It shows the container timezone, current container time, next scheduled update, live progress, and the latest 20 manual or automatic sync outcomes.

UI changes are saved in `filters/.auto_update_state.json` and take precedence over environment defaults on later starts. The Docker filters volume preserves this state when the API container is recreated. Delete that state file to return to the `AUTO_UPDATE_FILTERS` and `AUTO_UPDATE_TIME` defaults.

### API credential checks

The client fetches R2 credentials from `NWEBBED_API_URL` using `GET` with the header `Authorization: HFKey <NWEBBED_API_KEY>`.

Check a username and password using either request:

```http
GET /check?username=testusername1@nwebbed.com&password=testpassword1
Host: localhost
```

```http
POST /check
Host: localhost
Content-Type: application/json

{
  "username": "testusername1@nwebbed.com",
  "password": "testpassword1"
}
```

Use HTTPS for these requests. The client hashes raw inputs as `SHA256(username+nWebbed+password)`. To hash locally before sending, submit the digest instead:

```http
POST /checkhash
Host: localhost
Content-Type: application/json

{
  "hash": "29f33573df6d1c7aac289e5c75e0bce5e4939e69c0499fb7e2540b7f371c59d9"
}
```

### CLI
```bash
# Manifest-backed credential check
uv run hush.py -m manifest.json -u USERNAME -p PASSWORD

# Manifest-backed batch credentials from TSV
uv run hush.py -m manifest.json -t credentials.tsv

# Manifest-backed single precomputed SHA-256 check
uv run hush.py -m manifest.json --checkhash <sha256_hex_digest>

# Test mode (project-root test_manifest.json)
uv run hush.py --test -u USERNAME -p PASSWORD

# Test mode with hashes (project-root test_manifest.json)
uv run hush.py --test --checkhash <sha256_hex_digest>
```

### API
```bash
# Start API
uv run uvicorn api:app --reload

# Start API Container with nginx public TLS and internal nginx-to-API mTLS
docker compose build
docker compose run --rm tls-cert-init
docker compose up -d

# Credential check
curl -k "https://localhost/check?username=test123&password=password123"

# Open credential check UI
# https://localhost/ui-check

# Open filter sync UI
# https://localhost/ui-sync

Postman collection is saved under test/HushClient.postman_collection.json

Swagger docs are available at https://localhost/docs
```

### Docker TLS and Internal mTLS

The services use customer-facing HTTPS at nginx and internal mTLS between nginx and Uvicorn:

- `tls-cert-init` is a one-shot provisioning/checking service. It alone mounts the host `./tls` tree read/write, including a local CA signing key when one exists. Its inherited application health check is disabled.
- `hushfilter-api` runs on private Docker port `8443` with a server certificate and requires trusted client certificates. Its local health check has a separate client identity.
- `nginx` publishes ports `80`/`443`, redirects HTTP to HTTPS, and authenticates to the API with its own mTLS client certificate.

Generate the files **before the first `docker compose up`**, and before upgrading an existing installation to these mounts:

```bash
docker compose build
docker compose run --rm tls-cert-init
docker compose up -d
```

Individual file mounts deliberately fail when files are missing instead of creating directories at certificate paths. On upgrade, the initializer adds `healthcheck.crt`/`healthcheck.key` using the existing local CA, leaving valid existing certificates unchanged. If your CA is managed elsewhere and its signing key is absent, supply this additional client certificate/key from your PKI before starting the services. Set its extended key usage to client authentication.

If startup reports `Is a directory: '/app/tls/internal/healthcheck.crt'` (or another certificate/key path), an earlier file bind mount may have created a directory before the file existed. Docker documents this behavior for [automatically created bind-mount sources](https://docs.docker.com/engine/storage/bind-mounts/). The initializer removes **empty directories only** at expected certificate/key paths before generating missing pairs. It leaves existing certificate files unchanged, refuses populated directories and directory symlinks, and never replaces an existing CA to repair a missing leaf. `--check-only` reports the problem without removing anything.

To recover, run these commands individually from the project directory (also valid in PowerShell), proceeding only when each succeeds:

```powershell
docker compose down
docker compose build
docker compose run --rm --no-deps tls-cert-init
docker compose up -d
```

This removes the failed containers before repairing their file mounts and rebuilds the initializer with the recovery code. It retains the host's certificates, filters, and settings. If a certificate path contains data, inspect it and restore the correct file before retrying; do not delete the `tls` tree.

The host certificate layout is:

```text
tls/
  public/
    fullchain.pem          # customer-facing leaf followed by intermediate certificates
    privkey.pem            # customer-facing private key
  internal/
    ca.crt                 # trusted internal CA certificate(s); no private material
    ca.key                 # optional LOCAL signing key: provisioning service/host only
    hushfilter-api.crt     # API server leaf, followed by intermediates if required
    hushfilter-api.key     # API server private key
    nginx-client.crt       # nginx mTLS client leaf/chain
    nginx-client.key       # nginx mTLS client private key
    healthcheck.crt        # dedicated API health-check client leaf/chain
    healthcheck.key        # dedicated API health-check client private key
```

Only these keys enter the long-running containers:

| Container | Private keys mounted read-only |
| --- | --- |
| `hushfilter-api` | `internal/hushfilter-api.key`, `internal/healthcheck.key` |
| `nginx` | `public/privkey.pem`, `internal/nginx-client.key` |

Both services receive `internal/ca.crt` and the certificates required for their own TLS connections. The API also receives **only the public certificate files** `public/fullchain.pem` and `internal/nginx-client.crt` for expiry monitoring. Neither running service receives `internal/ca.key`, nor the other service's private keys. An external PKI's CA signing key can remain entirely outside this host: a complete externally issued certificate set does not require `ca.key` locally. Back up any locally managed signing key outside the repository with restricted access; generated private files use mode `0600` on POSIX.

#### Expiry checks and alerts

The initializer validates certificate/key matches, internal issuer signatures/chains, internal server SANs, client/server usage, and the validity dates of every certificate in the supplied PEM files. Default initialization reuses valid files, reports approaching expiry, and exits nonzero on expired, not-yet-valid, unreadable, mismatched, or incomplete material. It never silently rotates an existing CA.

Run a read-only check at any time, or schedule it from your host monitoring:

```bash
docker compose run --rm tls-cert-init python scripts/ensure_tls_certs.py \
  --tls-dir /app/tls --check-only --renew-before-days 30
```

`--check-only` exits **1** when any certificate is invalid/missing or expires within the warning window; otherwise it exits **0**. It does not create or renew files. This host-side check reads keys to validate their matches; the application expiry monitor reads certificates only.

Compose enables application monitoring with `HUSHCLIENT_TLS_MONITOR=1` and `HUSHCLIENT_TLS_DIR=/app/tls`. It runs at startup, hourly, and when alert settings are saved. On **Alerts**, select **Certificate expiry and validity**, choose **Certificate expiry warning (days)** (1–365, default 30), configure SMTP, and enable automatic alerts. The category uses the normal alert cooldown. The **TLS certificate status** panel and `GET /alerts/certificates` provide an on-demand read-only expiry check, even when email alerts are disabled. For a plain-HTTP development process, monitoring is off unless explicitly enabled; custom TLS deployments must set these environment variables themselves.

The application's check reports certificate validity dates and readability, not a live handshake or full PKI/revocation validation. Missing or malformed TLS material can prevent Uvicorn from starting before application alerts run. Use the read-only preflight and external availability/TLS monitoring for those failures and for host/container outages. Expiry alerts never renew certificates or reload services automatically.

#### Renewing bootstrap certificates with a stable CA

Generated leaves default to 825 days; a **new** internal CA defaults to 3650 days. `--valid-days` and `--ca-valid-days` change issuance lifetimes; they do not extend an existing CA. Internal leaves are capped at that CA's expiry. Trust the internal CA certificate, rather than pinning individual server certificates, so renewing a leaf under the same CA does not require distributing a new trust anchor.

Take a protected backup first, then renew leaves approaching expiry:

```bash
docker compose run --rm tls-cert-init python scripts/ensure_tls_certs.py \
  --tls-dir /app/tls --renew --renew-before-days 30

docker compose run --rm tls-cert-init python scripts/ensure_tls_certs.py \
  --tls-dir /app/tls --check-only --renew-before-days 30
```

`--renew` replaces only expiring self-signed public certificates and internal leaves for which the local CA key is available. It reuses the existing leaf keys and **retains the internal CA certificate and key**. Internal renewal refuses a CA that is expired, not yet valid, or itself inside the renewal window; arrange CA rotation instead. Old installations whose CA and leaves share the same expiry may need this explicit CA rotation. An externally issued public certificate is left unchanged and must be renewed through its issuing CA/ACME provider. Renewing a self-signed public certificate changes that certificate's identity; clients that explicitly trusted it must update that trust. For routine customer-facing renewal, use certificates issued by an established CA.

Run renewal from a host timer or your certificate-management system before expiry, and pair successful renewal with the activation procedure below. Repeated initialization/checks alone do not perform renewal.

#### Installing customer-facing or externally issued certificates

Use a maintenance window and keep a protected backup of the previous matching files.

1. Obtain the replacement from your ACME/PKI provider. Install the customer-facing leaf and intermediate chain in `tls/public/fullchain.pem` and its matching unencrypted private key in `tls/public/privkey.pem`. Ensure its SANs cover the hostname customers use. The generator's self-signed default is only a bootstrap option.
2. For internal leaf renewal under the **same CA**, replace the affected `.crt`/`.key` pair without changing `tls/internal/ca.crt`. The API certificate needs server-authentication usage and SANs for `hushfilter-api`, `localhost` (used by the health check), and any additional names in `HUSHFILTER_INTERNAL_SERVER_NAMES`. Both nginx and health-check certificates need client-authentication usage. PEM chains should put the leaf first; provide required intermediates and the appropriate CA trust bundle.
3. Install regular files with restricted private-key permissions. If your ACME client uses symlinks, copy the dereferenced certificate/key into this layout as part of its deploy hook. Avoid exposing an entire ACME account/key directory to the containers.
4. Run the read-only check above. If valid certificates are intentionally within the warning window, the check still returns 1; renew them or explicitly resolve that warning before considering the refresh complete.
5. Activate the replacement using the following procedure.

#### Activating replacements and reloads

The supplied Compose configuration uses **individual bind-mounted files**. The generator writes certificates atomically, and external tools commonly replace files or symlinks in the same way. Existing mounts can continue referencing the old files, so a process reload or container restart alone is not a reliable refresh. **Force-recreate the affected containers to remount the files.** Uvicorn must restart to load its TLS context; recreating nginx also refreshes its upstream API address after API recreation.

This sequence refreshes all TLS files and the API's monitoring copies, including after a public-only certificate replacement. Expect a brief interruption on this single-instance deployment:

```bash
# Validate the files on the host through the provisioning service first.
docker compose run --rm tls-cert-init python scripts/ensure_tls_certs.py \
  --tls-dir /app/tls --check-only

# Stop here if validation failed. Load the new API/health-check certificates first.
docker compose up -d --no-deps --force-recreate --wait --wait-timeout 120 hushfilter-api

# Then refresh nginx's customer-facing/client certificates and upstream address.
docker compose up -d --no-deps --force-recreate nginx
docker compose exec nginx nginx -t
docker compose ps
```

Afterward, check the TLS certificate dates in Alerts, verify `https://<your-host>/health` using a client that trusts the public certificate's issuer, and confirm the certificate presented externally is the replacement. If validation or startup fails, restore the previous matching files (including the matching CA for a CA rotation), then repeat the recreation sequence.

For deployments using a different mount layout where new certificate contents are visible in nginx, `nginx -t` followed by `nginx -s reload` reloads configuration/certificates gracefully. That does **not** refresh Uvicorn's TLS context or replace stale file bind mounts in this Compose setup. See [nginx reload behavior](https://nginx.org/en/docs/control.html) and [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/).

#### Explicit CA rotation

Routine server/client renewal should keep the CA stable. When the CA itself is expiring or must be replaced, coordinate a trust change: stop nginx and the API, obtain the new CA trust bundle and **all three** matching internal server/client pairs, replace the internal files as a set, validate, and run the recreation sequence above. Update any other clients that trust this CA. Archive/remove any obsolete local signing key; do not leave an old key paired with a new CA certificate.

For a locally generated bootstrap CA, generate a fresh set in a separate empty staging directory, for example `uv run python scripts/ensure_tls_certs.py --tls-dir /secure/staged-tls`, then install only that staged `internal/` set during the maintenance window. Keep the customer-facing pair unchanged. Do not delete the live `tls` tree to renew a server certificate. The tool deliberately does not automate CA rotation or a zero-downtime trust overlap.

### Optional explicit DNS

Docker normally supplies working DNS without configuration. If name resolution fails inside `hushfilter-api`, uncomment the `dns` block under that service in `docker-compose.yml` and use resolvers reachable from the container:

```yaml
    dns:
      - 1.1.1.1
      - 1.0.0.1
```

The example uses public resolvers; use your organization's DNS servers if SMTP or other required names are private. Recreate the service after changing Compose settings. Explicit DNS should not normally be necessary, but a host-local resolver stub, VPN/split-DNS configuration, or host firewall/DNS policy can make the host's DNS path unavailable to containers. A loopback resolver such as `127.0.0.1` inside a container refers to that container, not the host. This setting addresses resolution; it does not bypass outbound firewall restrictions. See [Docker DNS behavior](https://docs.docker.com/engine/network/#dns-services).

## Diagnostics and scheduled retries

The Filter Sync page at `/ui-sync/` displays the machine ID sent to nWebbed. This ID is derived from the runtime's MAC address, so it can change when a container is recreated. If no hardware MAC is detected, the page shows that the ID is unavailable. Recent sync history appears below the operational log and shows only the newest entry until expanded.

Open **Logs** at `/ui-logs/` to view local diagnostics, see their total retained size, download all retained logs, clear them, and save logging settings. Checkbox selections are combined: choose **Critical errors** alone for critical-only logging, **Sync logs** alone for sync-only logging, or combine categories. **Everything** includes debug messages; **Off** clears all selections. Turning logging off keeps existing files until you clear them.

By default, errors, warnings, and sync activity are recorded. Logs include application startup/load failures, request failures, background exceptions, and sync/download failures. Request summaries omit request bodies and query strings. Successful GET/HEAD requests (including status polling, health checks, and page loads) are not recorded, even with Everything enabled, so routine reads cannot rotate away useful diagnostics. Failed requests and other application errors remain eligible under the selected categories. Logging covers the Python application; Docker/nginx logs and failures before the application starts remain available through `docker compose logs`.

Logs are stored in `logs/hushclient.log`, with three rotated backups (`hushclient.log.1` through `.3`). Each file holds up to 256 MiB, for approximately **1 GiB (1,024 MiB) of total retention**. These are consecutive parts of the same log: the active file receives new entries, `.1` is the newest backup, and `.3` is the oldest. Rotation discards the oldest backup as new history arrives, keeping disk usage bounded without growing a single file indefinitely. Existing logs remain available when upgrading to the larger limit.

The UI previews the latest 256 KiB; the download combines all retained files from oldest to newest. Settings are saved in `logs/settings.json`. Docker Compose mounts `./logs` so diagnostics and settings survive container recreation. Set `HUSHCLIENT_LOG_DIR` to change this directory outside Docker, or mount the matching directory when overriding it in Docker.

Scheduled updates retry failed filter sync/download stages every five minutes, for up to one hour after the original scheduled start. Each attempt is recorded in history and the log file. Existing partial downloads are reused by the downloader. The sync page shows the next retry and the window deadline. No new retry begins after the deadline; an attempt already running may finish later. Manifest-generation and reload failures do not trigger download retries. Changing or disabling the schedule cancels pending retries; a running attempt is allowed to finish. Restarting the app resets a pending retry window, and the next daily schedule applies.

## CLI Usage

### Filter Source
- `-m, --manifest`: use the specified manifest file
- `--test`: use `test_manifest.json`

### Inputs
- `-u, --username`
- `-p, --password` (optional; defaults to empty)
- `-t, --tsv` (batch credentials, tab-separated `username\tpassword`)
- `--checkhash` (single SHA-256 digest)

### Output
```text
username\tpassword\tTrue/False\tmatch_count
```

## API Endpoints

- `GET /` — home page (HTML)
- `GET /endpoints` — API information and endpoint listing (JSON)
- `GET /docs`
- `GET /ui-check`
- `GET /ui-sync`
- `GET /ui-logs`
- `GET /ui-alerts`
- `GET /alerts/settings`
- `PUT /alerts/settings`
- `POST /alerts/test`
- `GET /alerts/certificates`
- `GET /health`
- `GET /stats`
- `GET /check`
- `POST /check`
- `POST /check/batch`
- `POST /checkhash`
- `POST /checkhash/batch`
- `POST /sync/apply`
- `POST /sync/filters`
- `POST /sync/manifest`
- `POST /sync/reload`

## Web UI

Frontend is served directly by the API at:
- `GET /ui-check`
- `GET /ui-sync`

UI assets are stored in:
- `webui/ui-check/index.html`
- `webui/ui-check/styles.css`
- `webui/ui-check/app.js`
- `webui/ui-sync/index.html`
- `webui/ui-sync/styles.css`
- `webui/ui-sync/app.js`

Behavior:
- `/ui-check` accepts `username` and `password`
- `/ui-check` computes a SHA-256 digest client-side in JavaScript, displays it, and sends `POST /checkhash`
- `/ui-sync` provides a top-level one-click action to sync filters, update `manifest.json`, and reload the in-memory filters in sequence
- `/ui-sync` also provides manual `sync filters from nWebbed`, `update manifest`, and `reload with new filters` actions
- `/ui-sync` calls `POST /sync/apply`, `POST /sync/filters`, `POST /sync/manifest`, and `POST /sync/reload` and renders the outputs on screen

### `POST /sync/apply`
Starts the full filter refresh sequence in a background worker and immediately returns `202 Accepted`.

Sequence:
1. `POST /sync/filters`
2. `POST /sync/manifest`
3. `POST /sync/reload`

Each step waits for the previous step to finish. If any step fails, the sequence stops.

Poll `GET /sync/status` for live logs and the final result payload. Once `active` becomes `false`, the operation is complete and the status payload contains the final `success`, `detail`, logs, and any apply result fields such as `downloaded`, `manifest_path`, `output_file`, and `filter_count`.

### `POST /sync/filters`
Triggers the filter sync workflow, including manifest download, zip verification, extraction, and filter MD5 verification.

Response:
```json
{
  "success": true,
  "manifest_path": "filters/manifest_current.json",
  "downloaded": ["filters/202604/20260401_20260408/20260401_20260408.zip"],
  "redownloaded": [],
  "verified_existing": [],
  "logs": [
    "INFO starting filter md5 verification",
    "INFO finished filter md5 verification"
  ],
  "detail": null
}
```

### `POST /sync/manifest`
Regenerates `manifest.json` from the local `filters/` tree using `helpers/generate_manifest.py`.

### `POST /sync/reload`
Closes existing filter mappings and loads files from the current `manifest.json` into memory without restarting the API process.

### `POST /check`
Request:
```json
{
  "username": "testusername1@nwebbed.com",
  "password": "testpassword1"
}
```

Response:
```json
{
    "test_mode": false,
    "found": true,
    "matching_filters": [
        "filters/202604/20010101_20260401/29_20010101_20260401.hf"
    ]
}
```

### `POST /check/batch`
Checks batch username/password inputs and returns only usernames that were found.

Request:
```json
{
  "credentials": [
    {
      "username": "testusername1@nwebbed.com",
      "password": "testpassword1"
    },
    {
      "username": "user038_alpha",
      "password": "DyQE4efLerNH"
    }
  ]
}
```

Response:
```json
{
    "test_mode": false,
    "total": 2,
    "found_usernames": [
        "testusername1@nwebbed.com"
    ]
}
```

### `POST /checkhash`
Request:
```json
{
  "hash": "29f33573df6d1c7aac289e5c75e0bce5e4939e69c0499fb7e2540b7f371c59d9"
}
```

Response:
```json
{
    "test_mode": false,
    "found": true,
    "matching_filters": [
        "filters/202604/20010101_20260401/29_20010101_20260401.hf"
    ]
}
```

### `POST /checkhash/batch`
Checks batch SHA-256 hash inputs and returns only hashes that were found.

Request:
```json
{
    "hashes": [
        "1e8ce99fda5de7cb95dc4d32261ffbb6e495fcaffde224a0751efa45d4867c2d", // Random
        "dfce80097de4de11f760b9ff85902c55e9ae66826d802c0941f215b5cd41304e", // Random
        "29f33573df6d1c7aac289e5c75e0bce5e4939e69c0499fb7e2540b7f371c59d9"  // Positive Test Value: testusername1@nwebbed.com testpassword1
    ]
}
```

Response:
```json
{
  "total": 3,
  "found_hashes": [
    "29f33573df6d1c7aac289e5c75e0bce5e4939e69c0499fb7e2540b7f371c59d9"
  ]
}
```

## Notes

- Bloom filters can produce false positives but not false negatives.
