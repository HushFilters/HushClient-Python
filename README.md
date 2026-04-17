# HushFilter Python Implementation

Python implementation for checking HushFilter bloom filters for credential membership.

## Supported Inputs

The CLI and API accept only:
- `username + password`
- precomputed SHA-256 hash (64-char hex digest of `username+password`)
- CLI: batch credentials from TSV
- API: batch credentials in the form of username + password, and SHA-256 hashes

## Quick Start

Instantiate environment:  
```
uv venv
uv sync
```

Acquire Hush filter file(s) and store them in the 'filters' folder.  
Copy manifest.json.EXAMPLE to manifest.json and update the manifest with the filters you have.  

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

# Start API Container in Test mode (comes with bundled test filters)
docker compose -f docker-compose.test.yml build
docker compose -f docker-compose.test.yml up -d

# Start API Container in Prod mode
docker compose build
docker compose up -d

# Credential check
curl "http://localhost:8000/check?username=test123&password=password123"

# Open credential check UI
# http://localhost:8000/ui-check

# Open filter sync UI
# http://localhost:8000/ui-sync

Postman collection is saved under test/HushClient.postman_collection.json

Swagger docs are available at http://localhost:8000/docs
```

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
Runs the full filter refresh sequence in order:
1. `POST /sync/filters`
2. `POST /sync/manifest`
3. `POST /sync/reload`

Each step waits for the previous step to finish. If any step fails, the sequence stops and returns the combined logs collected so far.

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
Closes existing filter mappings and loads the current `manifest.json` into memory without restarting the API process.

### `POST /check`
Request:
```json
{
  "username": "test",
  "password": "pass"
}
```

Response:
```json
{
  "found": true,
  "matching_filters": ["filters/000_filter1000000.hf"]
}
```

### `POST /check/batch`
Checks batch username/password inputs and returns only usernames that were found.

Request:
```json
{
  "credentials": [
    {"username": "user1", "password": "pass1"},
    {"username": "user2", "password": "pass2"}
  ]
}
```

Response:
```json
{
  "total": 2,
  "found_usernames": ["user1"]
}
```

### `POST /checkhash`
Request:
```json
{
  "hash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

Response:
```json
{
  "found": true,
  "matching_filters": ["filters/000_filter1000000.hf"]
}
```

### `POST /checkhash/batch`
Checks batch SHA-256 hash inputs and returns only hashes that were found.

Request:
```json
{
  "hashes": [
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  ]
}
```

Response:
```json
{
  "total": 2,
  "found_hashes": [
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  ]
}
```

## Library Usage

```python
from core.filter_core import FilterManager

with FilterManager(manifest_path="manifest.json") as manager:
    result = manager.check("username", "password")
    print(result.found)

    hash_result = manager.check_sha256_hash(
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )
    print(hash_result.found)
```

## Notes

- Bloom filters can produce false positives but not false negatives.
- Use context managers (or `close()`) to release memory-mapped files.

## Documentation

- `agents.md` for architecture and agent-specific notes
- `README_HASH.md` for SHA-256 workflow details
- `DEPLOYMENT.md` for deployment options
