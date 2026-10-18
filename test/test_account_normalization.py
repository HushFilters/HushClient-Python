"""Account normalization must agree across HushClient, the UI and both SDKs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.filter_core import FilterManager
from core.hash import bloom_probe_hashes, credential_digest
from core.main import HushFilter


VECTORS = json.loads((Path(__file__).parent / "data/hashes.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["username"])
def test_credential_digest_vectors(vector):
    assert credential_digest(vector["username"], vector["password"]).hex() == vector["hash"]


@pytest.fixture
def manager():
    # An in-memory Bloom populated from a fixed shared digest, not from the
    # function under test. Exercise real shard selection and membership checks.
    digest = bytes.fromhex(next(v["hash"] for v in VECTORS if v["username"] == "Username1"))
    bloom = HushFilter.__new__(HushFilter)
    bloom.nbits = 1 << 20
    bloom.nk = 10
    bloom.filter_start = 0
    bloom.mm = bytearray(bloom.nbits // 8)
    bloom.f = None
    for probe in bloom_probe_hashes(digest, nk=bloom.nk):
        pos = probe % bloom.nbits
        bloom.mm[pos // 8] |= 1 << (pos % 8)
    manager = FilterManager.__new__(FilterManager)
    path = f"{digest.hex()[:2]}_20010101_20260101.hf"
    manager.filters = [(path, bloom)]
    # A wrong shard fails loudly instead of falling back to the populated shard.
    class WrongShard:
        def check(self, *args):
            pytest.fail("Account was not lowercased before shard selection")
    manager.filters_by_prefix = {
        digest.hex()[:2]: manager.filters,
        credential_digest("other-account", "PaSsword1").hex()[:2]: [("wrong.hf", WrongShard())],
    }
    import hashlib
    for account in ("UserName1", "USERNAME1", "Username1"):
        raw_prefix = hashlib.sha256((account + "nWebbedPaSsword1").encode()).hexdigest()[:2]
        if raw_prefix != digest.hex()[:2]:
            manager.filters_by_prefix[raw_prefix] = [("wrong.hf", WrongShard())]
    return manager


def test_raw_checks_batch_and_precomputed_hash_agree(manager):
    accounts = ["Username1", "username1", "USERNAME1"]
    for result, account in zip(manager.check_batch([(a, "PaSsword1") for a in accounts]), accounts):
        assert result.found
        assert result.username == account
    digest = next(v["hash"] for v in VECTORS if v["username"] == "Username1")
    assert manager.check_sha256_hash(digest).found
    assert not manager.check("username1", "password1").found
    assert not manager.check(" username1", "PaSsword1").found


def test_api_single_get_batch_and_hash_checks(manager, monkeypatch):
    import api
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api, "filter_manager", manager)
    client = TestClient(api.app)
    for username in ("Username1", "username1", "USERNAME1"):
        payload = {"username": username, "password": "PaSsword1"}
        assert client.post("/check", json=payload).json()["found"]
        assert client.get("/check", params=payload).json()["found"]
    response = client.post("/check/batch", json={"credentials": [
        {"username": "Username1", "password": "PaSsword1"},
        {"username": "USERNAME1", "password": "PaSsword1"},
        {"username": "username1", "password": "password1"},
    ]})
    assert response.json()["found_usernames"] == ["Username1", "USERNAME1"]
    digest = next(v["hash"] for v in VECTORS if v["username"] == "Username1")
    assert client.post("/checkhash", json={"hash": digest}).json()["found"]
    assert client.post("/check", json={"username": "Username1", "password": "password1"}).json()["found"] is False
