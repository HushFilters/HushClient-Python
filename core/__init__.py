"""
Core module for HushFilter bloom filter operations.

This module contains the core functionality for working with HushFilter
bloom filters, including:
- main.py: Low-level HushFilter class for file I/O and bloom filter operations
- filter_core.py: FilterManager for managing manifest-backed filters
- hash.py: Standalone hash generation utilities
"""

from core.main import HushFilter
from core.filter_core import FilterManager, CheckResult
from core.hash import hash_credential, compute_hash_positions, compute_credential_hashes

__all__ = [
    'HushFilter',
    'FilterManager',
    'CheckResult',
    'hash_credential',
    'compute_hash_positions',
    'compute_credential_hashes',
]
