"""Public smoke-test credentials, selected by exact manifest build location."""
from __future__ import annotations

import re

from core.hash import credential_digest

LEGACY_ID = "legacy-canaries-v1"
SHARD_ID = "shard-canaries-v1"
LEGACY_CREDENTIALS = tuple((f"testusername{i}@nwebbed.com", f"testpassword{i}") for i in range(1, 5))


def manifest_test_sets(source):
    """Return location -> (set ID, credentials). Only absent metadata is legacy.

    Rebuilt historic ranges can select the full suite without imposing it on
    older builds. Incomplete or malformed declarations must never skip checks.
    """
    if "test_sets" not in source and "current_filter_tests" not in source:
        return None
    sets = source.get("test_sets")
    references = source.get("current_filter_tests")
    locations = source.get("current_filter_locations")
    if not isinstance(sets, dict) or not isinstance(references, dict) or not isinstance(locations, list):
        raise ValueError("Invalid manifest test-set metadata")
    if not locations or any(not isinstance(loc, str) for loc in locations):
        raise ValueError("Test sets require current filter locations")
    if len(set(locations)) != len(locations) or set(references) != set(locations):
        raise ValueError("current_filter_tests must cover every current filter location exactly once")
    parsed = {}
    for set_id, definition in sets.items():
        if set_id not in (LEGACY_ID, SHARD_ID):
            raise ValueError(f"Unknown test set {set_id}")
        if not isinstance(definition, dict) or definition.get("id") != set_id:
            raise ValueError(f"Invalid definition for test set {set_id}")
        entries = definition.get("credentials")
        count = 256 if set_id == SHARD_ID else 4
        if not isinstance(entries, list) or len(entries) != count:
            raise ValueError(f"Test set {set_id} requires {count} credentials")
        credentials, shards = [], set()
        for entry in entries:
            if not isinstance(entry, dict) or any(not isinstance(entry.get(k), str)
                    for k in ("username", "password", "sha256", "shard")):
                raise ValueError(f"Malformed credential in test set {set_id}")
            username, password = entry["username"], entry["password"]
            match = re.fullmatch(r"testusername([1-9][0-9]*)@nwebbed\.com", username)
            if not match or password != f"testpassword{match[1]}":
                raise ValueError(f"Invalid synthetic test credential {username}")
            digest = credential_digest(username, password).hex()
            if entry["sha256"] != digest or entry["shard"] != digest[:2]:
                raise ValueError(f"Incorrect digest or shard for test credential {username}")
            if entry["shard"] in shards:
                raise ValueError(f"Duplicate test shard {entry['shard']}")
            shards.add(entry["shard"])
            credentials.append((username, password))
        if not set(LEGACY_CREDENTIALS).issubset(credentials):
            raise ValueError(f"Test set {set_id} omits legacy credentials")
        parsed[set_id] = tuple(credentials)
    result = {}
    for location, set_id in references.items():
        if not re.fullmatch(r"[0-9]{6}/[0-9]{8}_[0-9]{8}", location):
            raise ValueError(f"Invalid test-set build location {location}")
        if not isinstance(set_id, str) or set_id not in parsed:
            raise ValueError(f"Unknown test set for {location}")
        result[location] = (set_id, parsed[set_id])
    return result
