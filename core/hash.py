"""
Standalone hash generation utilities for HushFilter bloom filters.

This module provides functions to generate MurmurHash3-compatible hashes for credentials
that can be used to check membership in bloom filters. It has no dependencies
on other project files.

Functions:
    hash_credential: Generate hash array for a username or username+password
    compute_hash_positions: Compute bit positions from hash array
    compute_credential_hashes: Generate array of mmh3-compatible hashes for a credential
"""
from typing import List
import hashlib
import binascii
from helpers.mm3 import hash64

_DOUBLE_HASH_FALLBACK = 0x9E3779B97F4A7C15


def credential_digest(username: str, password: str = "") -> bytes:
    """Compute the Builder-compatible SHA-256 digest bytes for a credential."""
    return hashlib.sha256((username + 'nWebbed' + password).encode("utf-8")).digest()


def normalize_sha256_hex(sha256_hex: str) -> str:
    """Validate and normalize a SHA-256 hex digest string."""
    normalized = (sha256_hex or "").lower()
    try:
        digest = binascii.unhexlify(normalized)
    except (binascii.Error, ValueError):
        raise ValueError("sha256_hex must be a 64-character hexadecimal string")
    if len(digest) != 32:
        raise ValueError("sha256_hex must be a 64-character hexadecimal string")
    return normalized


def parse_sha256_hex(sha256_hex: str) -> bytes:
    """Parse a normalized SHA-256 hex digest string into raw bytes."""
    normalized = normalize_sha256_hex(sha256_hex)
    return binascii.unhexlify(normalized)


def bloom_probe_hashes(digest: bytes, nk: int = 10) -> List[int]:
    """
    Generate Builder-compatible Bloom probe hashes using Murmur3 double hashing.

    The Builder uses two Murmur3 64-bit base hashes with seeds 0 and 1, then
    applies Kirsch-Mitzenmacher double hashing: h(i) = h1 + i*h2.
    """
    if nk <= 0:
        raise ValueError("nk must be a positive integer")
    if len(digest) != 32:
        raise ValueError("digest must be exactly 32 bytes (SHA-256)")

    h1, _ = hash64(digest, 0, signed=False)
    h2, _ = hash64(digest, 1, signed=False)
    if h2 == 0:
        h2 = _DOUBLE_HASH_FALLBACK

    mask = (1 << 64) - 1
    return [(h1 + i * h2) & mask for i in range(nk)]


def hash_digest(digest: bytes, nk: int = 10) -> List[int]:
    """
    Generate MurmurHash3-compatible hashes from precomputed SHA-256 digest bytes.

    Args:
        digest: 32-byte SHA-256 digest
        nk: Number of hash functions to use (default: 10)
    """
    return bloom_probe_hashes(digest, nk=nk)


def hash_sha256_hex(sha256_hex: str, nk: int = 10) -> List[int]:
    """
    Generate MurmurHash3-compatible hashes from a SHA-256 hex digest string.

    Args:
        sha256_hex: 64-character hexadecimal SHA-256 digest string
        nk: Number of hash functions to use (default: 10)
    """
    return hash_digest(parse_sha256_hex(sha256_hex), nk=nk)


def hash_credential(username: str, password: str = "", nk: int = 10) -> List[int]:
    """
    Generate an array of MurmurHash3-compatible hashes for a credential.
    
    This function produces the same hash values that are used by the HushFilter
    bloom filter implementation. The hashes can be sent to the API to check
    membership without sending the actual credential.
    
    Args:
        username: The username/account/email to hash
        password: The password to hash (default: empty string)
        nk: Number of hash functions to use (default: 10)
            Must match the nk value of the target filter(s)
    
    Returns:
        List of nk unsigned 64-bit integers representing the hashes
        
    Example:
        >>> hashes = hash_credential("user@example.com", "password123", nk=10)
        >>> len(hashes)
        10
        >>> isinstance(hashes[0], int)
        True
    """
    if nk <= 0:
        raise ValueError("nk must be a positive integer")
    
    # Match the filter input digest, then derive Bloom probes via double hashing.
    digest = credential_digest(username, password)
    return hash_digest(digest, nk=nk)


def compute_hash_positions(hashes: List[int], nbits: int) -> List[int]:
    """
    Compute bit positions from hash values for a bloom filter.
    
    This converts raw hash values to actual bit positions in a bloom filter
    of a given size.
    
    Args:
        hashes: List of hash values (from hash_credential)
        nbits: Size of the bloom filter in bits
    
    Returns:
        List of bit positions (0 to nbits-1)
        
    Example:
        >>> hashes = [12345678901234567, 98765432109876543]
        >>> positions = compute_hash_positions(hashes, 1000000)
        >>> all(0 <= p < 1000000 for p in positions)
        True
    """
    if nbits <= 0:
        raise ValueError("nbits must be a positive integer")
    
    return [h % nbits for h in hashes]


def compute_credential_hashes(username: str, password: str = "", nk: int = 10) -> List[int]:
    """
    Convenience function that combines hash_credential functionality.
    
    This is an alias for hash_credential for backward compatibility and clarity.
    
    Args:
        username: The username/account/email to hash
        password: The password to hash (default: empty string)
        nk: Number of hash functions to use (default: 10)
    
    Returns:
        List of nk unsigned 64-bit integers representing the hashes
    """
    return hash_credential(username, password, nk)


if __name__ == "__main__":
    # Example usage
    print("HushFilter Hash Generator")
    print("=" * 50)
    
    # Example 1: Username only
    username = "user@example.com"
    hashes = hash_credential(username, "", nk=10)
    print(f"\nUsername: {username}")
    print(f"Password: (empty)")
    print(f"Number of hashes (nk): 10")
    print(f"Generated hashes:")
    for i, h in enumerate(hashes):
        print(f"  Hash {i}: {h}")
    
    # Example 2: Username + password
    username = "admin"
    password = "secretpass"
    hashes = hash_credential(username, password, nk=5)
    print(f"\nUsername: {username}")
    print(f"Password: {password}")
    print(f"Number of hashes (nk): 5")
    print(f"Generated hashes:")
    for i, h in enumerate(hashes):
        print(f"  Hash {i}: {h}")
    
    # Example 3: Computing positions for a filter
    nbits = 1000000  # 1 million bits
    positions = compute_hash_positions(hashes, nbits)
    print(f"\nBit positions for filter with {nbits} bits:")
    for i, pos in enumerate(positions):
        print(f"  Position {i}: {pos}")
