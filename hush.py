#!/usr/bin/env python3
"""
HushFilter CLI for manifest-backed bloom filter operations.
"""
import sys
import argparse
from core.hash import normalize_sha256_hex
from core.filter_core import FilterManager


def run_manifest_checks(args):
    """Check credentials or hashes against filters loaded from a manifest."""
    # Initialize filter manager
    try:
        manager = FilterManager(manifest_path=args.manifest)
        print(f"Loading {len(manager.filters)} filters from manifest...", file=sys.stderr)
        print(f"Successfully loaded {len(manager.filters)} filters", file=sys.stderr)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    
    try:
        # Single credential check
        if args.username:
            result = manager.check(args.username, args.password)
            
            if args.verbose:
                print(f"\nChecking: {result.username}" + 
                      (f" / {result.password}" if result.password else ""))
                print(f"Found in {result.match_count} filter(s)")
                if result.matching_filters:
                    print("Matching filters:")
                    for match in result.matching_filters:
                        print(f"  - {match}")
                else:
                    print("  (Not found in any filter)")
            else:
                # Compact output format
                print(f"{result.username}\t{result.password}\t{result.found}\t{result.match_count}")
        
        # Single precomputed SHA-256 hash check
        elif args.checkhash:
            normalized = normalize_sha256_hex(args.checkhash)
            result = manager.check_sha256_hash(normalized)
            if args.verbose:
                print(f"\nChecking hash: {normalized}")
                print(f"Found in {result.match_count} filter(s)")
                if result.matching_filters:
                    print("Matching filters:")
                    for match in result.matching_filters:
                        print(f"  - {match}")
                else:
                    print("  (Not found in any filter)")
            else:
                print(f"{normalized}\t\t{result.found}\t{result.match_count}")

        # TSV file batch check
        elif args.tsv:
            credentials = []
            with open(args.tsv, 'r') as f:
                for line in f:
                    parts = line.strip().split('\t')
                    if len(parts) >= 1:
                        username = parts[0]
                        password = parts[1] if len(parts) >= 2 else ''
                        credentials.append((username, password))
            
            print(f"Checking {len(credentials)} credentials...", file=sys.stderr)
            results = manager.check_batch(credentials)
            
            # Output results
            found_count = sum(1 for r in results if r.found)
            print(f"\nResults: {found_count}/{len(credentials)} found", file=sys.stderr)
            
            for result in results:
                if args.verbose:
                    print(f"{result.username}\t{result.password}\t{result.found}\t{result.match_count}")
                    if result.matching_filters:
                        for match in result.matching_filters:
                            print(f"  {match}")
                else:
                    print(f"{result.username}\t{result.password}\t{result.found}\t{result.match_count}")
        
        else:
            print("Error: Provide --username, --checkhash, or --tsv", file=sys.stderr)
            return 1
    
    finally:
        manager.close()
    
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='HushFilter - Bloom filter credential checker',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
            Examples:
            hush.py -m manifest.json -u USERNAME
            hush.py -m manifest.json -u USERNAME -v
            hush.py -m manifest.json -t credentials.tsv

            # Single precomputed SHA-256 hash check
            hush.py -m manifest.json --checkhash <sha256_hex_digest>

            # Test mode (uses root test_manifest.json)
            hush.py --test -u USERNAME -p PASSWORD
                """
    )
    
    parser.add_argument(
        '-m',
        '--manifest',
        help='Path to manifest.json file (default: manifest.json)',
    )
    parser.add_argument(
        '--test',
        action='store_true',
        help='Use test_manifest.json at the project root',
    )
    
    # Common arguments
    parser.add_argument('-u', '--username', help='Username to check')
    parser.add_argument('-p', '--password', default='', help='Password to check (optional)')
    parser.add_argument('--checkhash', help='Single SHA-256 hash (64-char hex) to check for membership')
    parser.add_argument('-t', '--tsv', help='TSV file with username/password pairs to check')
    
    parser.add_argument('-v', '--verbose', action='store_true',
                       help='Show detailed output')
    
    args = parser.parse_args()
    
    # Test mode uses the project-root test manifest
    if args.test:
        args.manifest = "test_manifest.json"
    elif not args.manifest:
        args.manifest = "manifest.json"

    if args.tsv and args.checkhash:
        parser.error("--checkhash cannot be used with --tsv")
    if args.username and args.checkhash:
        parser.error("Use either --username or --checkhash, not both")
    if args.checkhash and args.password:
        parser.error("--password cannot be used with --checkhash")

    # Validate that either username, checkhash, or tsv is provided
    if not args.username and not args.checkhash and not args.tsv:
        parser.error("Provide --username, --checkhash, or --tsv")
    
    return run_manifest_checks(args)


if __name__ == "__main__":
    sys.exit(main())
