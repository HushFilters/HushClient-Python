"""
Core library for HushFilter bloom filter operations.
Provides shared manifest-backed filter loading for both CLI and API interfaces.
"""
import json
import os
from typing import List, Tuple, Dict, Optional
from dataclasses import dataclass
from core.hash import credential_digest, normalize_sha256_hex
from core.main import HushFilter


@dataclass
class CheckResult:
    """Result of a membership check."""
    username: str
    password: str
    found: bool
    match_count: int
    matching_filters: List[str]


class FilterManager:
    """
    Manages bloom filter operations for filters loaded from a manifest.
    Thread-safe and suitable for use in web services.
    """
    
    def __init__(self, manifest_path: str):
        """
        Initialize the filter manager.
        
        Args:
            manifest_path: Path to manifest.json
        """
        self.filters: List[Tuple[str, HushFilter]] = []
        self.filters_by_prefix: Dict[str, List[Tuple[str, HushFilter]]] = {}
        self.max_nk = 0
        self._requested_filter_count = 0
        self.load_from_manifest(manifest_path)

        if self._requested_filter_count > 0 and not self.filters:
            raise RuntimeError("No filters were successfully loaded")

        self._build_prefix_index()
        self.max_nk = max((hf.nk for _, hf in self.filters), default=0)
    
    def load_from_manifest(self, manifest_path: str):
        """Load multiple filters from a manifest file."""
        with open(manifest_path, 'r') as f:
            manifest = json.load(f)
        
        filter_paths = manifest.get('filters', [])
        self._requested_filter_count = len(filter_paths)
        
        for filter_path in filter_paths:
            try:
                hf = HushFilter(filter_path)
                self.filters.append((filter_path, hf))
            except Exception as e:
                print(f"Warning: Failed to load {filter_path}: {e}")

    @staticmethod
    def _extract_filter_prefix(filter_path: str) -> Optional[str]:
        """
        Extract 2-character hexadecimal prefix from filenames like:
        {first_two_hash_characters}_{int}_{int}.hf
        """
        basename = os.path.basename(filter_path).lower()
        prefix = basename.split("_", 1)[0]
        if len(prefix) != 2:
            return None
        if any(ch not in "0123456789abcdef" for ch in prefix):
            return None
        return prefix

    def _build_prefix_index(self):
        """Build prefix -> filters map for targeted lookups."""
        index: Dict[str, List[Tuple[str, HushFilter]]] = {}
        for filter_path, hf in self.filters:
            prefix = self._extract_filter_prefix(filter_path)
            if prefix is None:
                continue
            index.setdefault(prefix, []).append((filter_path, hf))
        self.filters_by_prefix = index

    def _get_candidate_filters_for_sha256(self, sha256_hash: str) -> List[Tuple[str, HushFilter]]:
        """
        Return filters that match the hash prefix.
        Falls back to all filters when no prefix-matched filters exist.
        """
        prefix = normalize_sha256_hex(sha256_hash)[:2]
        candidates = self.filters_by_prefix.get(prefix, [])
        if candidates:
            return candidates
        return self.filters
    
    def check(self, username: str, password: str = "") -> CheckResult:
        """
        Check if credentials exist in the loaded filter(s).
        Short-circuits after the first positive match.
        
        Args:
            username: The username to check
            password: The password to check (default: empty string)
        
        Returns:
            CheckResult with match details
        """
        sha256_hex = credential_digest(username, password).hex()
        candidates = self._get_candidate_filters_for_sha256(sha256_hex)

        matching_filters = []
        for filter_path, hf in candidates:
            if hf.check(username, password):
                matching_filters.append(filter_path)
                break  # short-circuit on first positive match

        return CheckResult(
            username=username,
            password=password,
            found=len(matching_filters) > 0,
            match_count=len(matching_filters),
            matching_filters=matching_filters
        )
    
    def check_batch(self, credentials: List[Tuple[str, str]]) -> List[CheckResult]:
        """
        Check multiple credentials.
        
        Args:
            credentials: List of (username, password) tuples
        
        Returns:
            List of CheckResult objects
        """
        return [self.check(username, password) for username, password in credentials]
    
    def check_sha256_hash(self, sha256_hash: str) -> CheckResult:
        """
        Check membership using a single precomputed SHA-256 hex hash string.

        Args:
            sha256_hash: 64-character hexadecimal SHA-256 digest string

        Returns:
            CheckResult with match details

        Raises:
            ValueError: If the hash is not a 64-character hex string
        """
        normalized = normalize_sha256_hex(sha256_hash)

        candidates = self._get_candidate_filters_for_sha256(normalized)

        matching_filters = []
        for filter_path, hf in candidates:
            if hf.check_sha256_hash(normalized):
                matching_filters.append(filter_path)
                break  # short-circuit on first positive match

        return CheckResult(
            username="",
            password="",
            found=len(matching_filters) > 0,
            match_count=len(matching_filters),
            matching_filters=matching_filters
        )

    def check_sha256_batch(self, sha256_hashes: List[str]) -> List[CheckResult]:
        """
        Check multiple precomputed SHA-256 hex hashes.

        Args:
            sha256_hashes: List of 64-character hexadecimal SHA-256 digest strings

        Returns:
            List of CheckResult objects
        """
        return [self.check_sha256_hash(value) for value in sha256_hashes]
    
    def get_stats(self) -> Dict:
        """Get statistics about loaded filters."""
        return {
            "filter_count": len(self.filters),
            "filters": [path for path, _ in self.filters],
            "max_nk": self.max_nk
        }
    
    def close(self):
        """Close all open filter files."""
        for _, hf in self.filters:
            try:
                hf.close()
            except Exception:
                pass
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
