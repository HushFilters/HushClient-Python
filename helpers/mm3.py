def _normalize_key(key):
    """Ensure the key is bytes for hashing."""
    if isinstance(key, bytes):
        return key
    if isinstance(key, str):
        return key.encode("utf-8")
    raise TypeError(f"Unsupported key type: {type(key)}")


def _to_signed_64(value: int) -> int:
    """Convert an unsigned 64-bit int to signed representation."""
    if value & (1 << 63):
        return value - (1 << 64)
    return value

def rotl64(x, r):
    """Rotate left a 64-bit integer by r bits."""
    return ((x << r) & 0xFFFFFFFFFFFFFFFF) | (x >> (64 - r))

def getblock64(data, index):
    """Get a 64-bit block from the byte array."""
    start = index * 8
    return int.from_bytes(data[start:start + 8], byteorder='little')

def fmix64(k):
    """Final mixing of hash values."""
    k ^= k >> 33
    k *= 0xff51afd7ed558ccd
    k &= 0xFFFFFFFFFFFFFFFF  # Ensure 64-bit
    k ^= k >> 33
    k *= 0xc4ceb9fe1a85ec53
    k &= 0xFFFFFFFFFFFFFFFF  # Ensure 64-bit
    k ^= k >> 33
    return k

def mm3_x64_128(key, seed):
    """MurmurHash3 128-bit hash."""
    data = _normalize_key(key)
    length = len(data)
    n_blocks = length // 16

    h1 = h2 = seed

    c1 = 0x87c37b91114253d5
    c2 = 0x4cf5ad432745937f

    # Process 128-bit blocks
    for i in range(n_blocks):
        k1 = getblock64(data, i * 2)
        k2 = getblock64(data, i * 2 + 1)

        # Mix k1
        k1 *= c1
        k1 &= 0xFFFFFFFFFFFFFFFF
        k1 = rotl64(k1, 31)
        k1 *= c2
        k1 &= 0xFFFFFFFFFFFFFFFF
        h1 ^= k1

        h1 = rotl64(h1, 27)
        h1 = (h1 + h2) & 0xFFFFFFFFFFFFFFFF
        h1 = (h1 * 5 + 0x52dce729) & 0xFFFFFFFFFFFFFFFF

        # Mix k2
        k2 *= c2
        k2 &= 0xFFFFFFFFFFFFFFFF
        k2 = rotl64(k2, 33)
        k2 *= c1
        k2 &= 0xFFFFFFFFFFFFFFFF
        h2 ^= k2

        h2 = rotl64(h2, 31)
        h2 = (h2 + h1) & 0xFFFFFFFFFFFFFFFF
        h2 = (h2 * 5 + 0x38495ab5) & 0xFFFFFFFFFFFFFFFF

    # Process remaining bytes (tail)
    tail = data[n_blocks * 16:]
    k1 = k2 = 0
    tail_len = len(tail)

    # Process tail bytes into k1 and k2
    for i in range(tail_len):
        byte = tail[i]
        if i < 8:
            k1 |= (byte << (8 * i))
        else:
            k2 |= (byte << (8 * (i - 8)))
    
    # Mix k1 if we had any bytes in it
    if k1 != 0:
        k1 &= 0xFFFFFFFFFFFFFFFF
        k1 *= c1
        k1 &= 0xFFFFFFFFFFFFFFFF
        k1 = rotl64(k1, 31)
        k1 *= c2
        k1 &= 0xFFFFFFFFFFFFFFFF
        h1 ^= k1
    
    # Mix k2 if we had any bytes in it
    if k2 != 0:
        k2 &= 0xFFFFFFFFFFFFFFFF
        k2 *= c2
        k2 &= 0xFFFFFFFFFFFFFFFF
        k2 = rotl64(k2, 33)
        k2 *= c1
        k2 &= 0xFFFFFFFFFFFFFFFF
        h2 ^= k2

    # Final avalanche
    h1 ^= length
    h2 ^= length

    h1 = (h1 + h2) & 0xFFFFFFFFFFFFFFFF
    h2 = (h2 + h1) & 0xFFFFFFFFFFFFFFFF

    h1 = fmix64(h1)
    h2 = fmix64(h2)

    h1 = (h1 + h2) & 0xFFFFFFFFFFFFFFFF
    h2 = (h2 + h1) & 0xFFFFFFFFFFFFFFFF

    return h1, h2


def hash64(key, seed: int = 0, signed: bool = False):
    """
    mmh3.hash64-compatible helper that returns the low/high 64-bit halves.

    Args:
        key: bytes or str to hash
        seed: seed value (default: 0)
        signed: whether to return signed 64-bit ints (default: False)
    """
    h1, h2 = mm3_x64_128(key, seed)
    if signed:
        return _to_signed_64(h1), _to_signed_64(h2)
    return h1, h2


def test_murmur3_hash():
    data = b"Alice in Wonderland"
    seed = 0

    h1, h2 = mm3_x64_128(data, seed)
    computed = (h2 << 64) | h1
    # Expected value from corrected mmh3-compatible implementation
    expected = 152558322607905516292314101629334561983
    assert computed == expected, f"Test failed! My hash: {computed}, Expected hash: {expected}"
    print("Test passed!")

if __name__ == "__main__":
    test_murmur3_hash()
