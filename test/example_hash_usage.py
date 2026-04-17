#!/usr/bin/env python3
"""
Examples for SHA-256 based checking flows.
Run with: uv run python test/example_hash_usage.py
"""

import json
from core.hash import credential_digest
from core.filter_core import FilterManager


def sha256_hex(username: str, password: str = "") -> str:
    return credential_digest(username, password).hex()


def demo_filter_manager_checks():
    print("\n" + "=" * 70)
    print("DEMO 1: FilterManager credential and SHA-256 checks")
    print("=" * 70)

    fm = FilterManager(manifest_path="manifest.json")

    username = "testuser"
    password = "testpass"
    digest = sha256_hex(username, password)

    result_credential = fm.check(username, password)
    result_hash = fm.check_sha256_hash(digest)

    print(f"Credential found: {result_credential.found}")
    print(f"SHA-256 found: {result_hash.found}")
    print(f"Result parity: {result_credential.found == result_hash.found}")


def demo_api_payloads():
    print("\n" + "=" * 70)
    print("DEMO 2: API payload examples")
    print("=" * 70)

    credentials_payload = {
        "credentials": [
            {"username": "user1", "password": "pass1"},
            {"username": "user2", "password": "pass2"},
        ]
    }

    hash_payload = {
        "hashes": [
            sha256_hex("user1", "pass1"),
            sha256_hex("user2", "pass2"),
        ]
    }

    print("/check/batch payload:")
    print(json.dumps(credentials_payload, indent=2))
    print("\n/checkhash/batch payload:")
    print(json.dumps(hash_payload, indent=2))


def main():
    print("\n" + "#" * 70)
    print("# HushFilter SHA-256 Checking Examples")
    print("#" * 70)

    demo_filter_manager_checks()
    demo_api_payloads()


if __name__ == "__main__":
    main()
