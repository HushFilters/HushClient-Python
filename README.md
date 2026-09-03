# HushFilter Python Implementation

Python implementation for checking HushFilter bloom filters for credential membership.

## Supported Inputs

The CLI and API accept only:
- `username + password`
- precomputed SHA-256 hash (64-char hex digest of `username+nWebbed+password`)
- CLI: batch credentials from TSV
- API: batch credentials in the form of username + password, and SHA-256 hashes

## Quick Start
  
```
git clone {this repo}

cp .env.EXAMPLE .env
[paste in your nWebbed API key into the newly created .env file]

When direct R2 credentials are not set, the client fetches them from `NWEBBED_API_URL`
using `GET` with header `Authorization: HFKey <NWEBBED_API_KEY>`.

cp manifest.json.EXAMPLE manifest.json

docker compose build

docker compose up

Navigate to https://localhost/ui-sync/

Click on "sync, update manifest, and reload filters"

Wait... You will have large files to download at first. (50+ GB)

When finished, navigate to https://localhost/ui-check/

The prepopulated value should return TRUE when submitted

You can check raw username + password combinations like so:

GET https://localhost/check?username=testusername1@nwebbed.com&password=testpassword1

POST https://localhost/check

{
  "username": "testusername1@nwebbed.com",
  "password": "testpassword1"
}

Internally, raw usernames and passwords are hashed: SHA256(username+nWebbed+password)

Instead of sending raw credentials, you can hash beforehand and check the hash directly:

POST https://localhost/checkhash

{
    "hash": "29f33573df6d1c7aac289e5c75e0bce5e4939e69c0499fb7e2540b7f371c59d9"
}


To run the application in TEST MODE with bundled test filters, set HUSHFILTER_TEST_MODE=1 in your .env file.

To enable the built-in daily auto updater, set AUTO_UPDATE_FILTERS=1 and choose the container-local 24-hour run hour with AUTO_UPDATE_TIME.
Examples:
- `AUTO_UPDATE_TIME=23` runs the full sync/apply workflow at 11pm each day
- `AUTO_UPDATE_TIME=2` runs the full sync/apply workflow at 2am each day
- `AUTO_UPDATE_FILTERS=0` disables scheduled auto updates

The Filter Sync UI at `/ui-sync/` can also enable or disable automatic updates and change the run hour without restarting the service. The UI shows the container timezone, current container time, next scheduled update, live progress, and the latest 20 automatic update attempts with their downloaded/refreshed files and logs.

UI changes are persisted to `filters/.auto_update_state.json`, which takes precedence over the environment defaults on later starts. The existing `filters:/app/filters` Docker volume preserves this state when the API container is recreated. Delete that state file to return to the `AUTO_UPDATE_FILTERS` and `AUTO_UPDATE_TIME` defaults.

These username+password combinations should always return TRUE in both test and production modes:

testusername1@nwebbed.com testpassword1
testusername2@nwebbed.com testpassword2
testusername3@nwebbed.com testpassword3
testusername4@nwebbed.com testpassword4
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

`docker compose up` starts three services:

- `tls-cert-init` creates default self-signed certificates under `./tls` when no certificates are present.
- `hushfilter-api` runs Uvicorn on private Docker port `8443` with TLS enabled and requires a client certificate.
- `nginx` publishes ports `80` and `443`, redirects HTTP to HTTPS, and connects to `hushfilter-api` with an internal client certificate.

The generated/default certificate layout is:

```text
tls/
  public/
    fullchain.pem          # public/customer-facing nginx certificate
    privkey.pem            # public/customer-facing nginx private key
  internal/
    ca.crt                 # internal CA trusted by nginx and Uvicorn
    ca.key                 # internal CA private key for generated defaults
    hushfilter-api.crt     # Uvicorn server certificate
    hushfilter-api.key     # Uvicorn server private key
    nginx-client.crt       # nginx client certificate for mTLS
    nginx-client.key       # nginx client private key for mTLS
```

To use a customer-facing certificate, replace:

```text
tls/public/fullchain.pem
tls/public/privkey.pem
```

To use your own internal mTLS material, replace the complete `tls/internal/` set. The API server certificate must be valid for DNS name `hushfilter-api`, because nginx verifies that name when proxying to the app. The nginx client certificate should include client authentication usage and be signed by the CA in `tls/internal/ca.crt`.

The bootstrap script does not overwrite existing certs. If you want to regenerate defaults, stop the stack and remove `./tls`, then run `docker compose up` again. The generated certs are for bootstrapping and local deployments; compliance-sensitive deployments should replace them with certificates issued and rotated by your internal PKI.

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

- `GET /`
- `GET /docs`
- `GET /ui-check`
- `GET /ui-sync`
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
