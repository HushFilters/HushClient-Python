# Credential compatibility

- This repository is open source. Document only public components and interfaces; do not name or link to private projects, include their file paths, or describe their implementation/build processes. HushClient and HushClient-SDKs may reference each other.
- Hash `UTF8(lowercase(account) + "nWebbed" + password)` with SHA-256. Normalize in `core.hash.credential_digest` before both shard selection and Bloom checking; raw API/CLI/TSV paths must share it.
- Browser credential checks hash locally and must use `toLowerCase()` before SHA-256. Python uses `str.lower()`. Preserve passwords, whitespace and Unicode normalization form; do not case-fold or use locale-specific casing.
- Precomputed hash endpoints and CLI input cannot normalize the original account. Clearly instruct callers to lowercase the account before hashing. Retain submitted username spelling in results.
- Keep `test/data/hashes.json` aligned with HushClient-SDKs `testdata/hashes.json` to verify the public credential-hashing contract.
- Validate with `python -m pytest test/test_account_normalization.py test/test_api_ui_endpoints.py test/test_filter_manager_empty_manifest.py` and `node --test test/account_normalization_ui.test.cjs`. Use local test fixtures; do not sync or reload a live service for unit tests.
- Hashing changes require compatible filter data and coordinated HushClient/UI and SDK upgrades. Document the required data compatibility, sync and reload steps; a source update alone does not migrate stored filters.
