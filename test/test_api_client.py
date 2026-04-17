#!/usr/bin/env python3
"""
Example API client for testing the HushFilter API
"""
import requests
import json

# API base URL
BASE_URL = "http://localhost:8000"


def test_health():
    """Test the health endpoint."""
    print("Testing /health endpoint...")
    response = requests.get(f"{BASE_URL}/health")
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    print()


def test_stats():
    """Test the stats endpoint."""
    print("Testing /stats endpoint...")
    response = requests.get(f"{BASE_URL}/stats")
    print(f"Status: {response.status_code}")
    data = response.json()
    print(f"Filters loaded: {data['filter_count']}")
    print()


def test_single_check_get(username, password=""):
    """Test single credential check via GET."""
    print(f"Testing GET /check for {username}...")
    params = {"username": username}
    if password:
        params["password"] = password
    
    response = requests.get(f"{BASE_URL}/check", params=params)
    print(f"Status: {response.status_code}")
    result = response.json()
    print(f"Found: {result['found']}")
    if result['matching_filters']:
        print(f"Matching filters: {', '.join(result['matching_filters'][:3])}" + 
              (f" (+{len(result['matching_filters']) - 3} more)" if len(result['matching_filters']) > 3 else ""))
    print()


def test_single_check_post(username, password=""):
    """Test single credential check via POST."""
    print(f"Testing POST /check for {username}...")
    data = {"username": username, "password": password}
    
    response = requests.post(f"{BASE_URL}/check", json=data)
    print(f"Status: {response.status_code}")
    result = response.json()
    print(f"Found: {result['found']}")
    print()


def test_batch_check(credentials):
    """Test batch credential check."""
    print(f"Testing POST /check/batch with {len(credentials)} credentials...")
    data = {
        "credentials": [
            {"username": u, "password": p} for u, p in credentials
        ]
    }
    
    response = requests.post(f"{BASE_URL}/check/batch", json=data)
    print(f"Status: {response.status_code}")
    result = response.json()
    print(f"Total checked: {result['total']}")
    print(f"Found usernames: {result['found_usernames']}")
    print()


def test_batch_checkhash(hashes):
    """Test batch precomputed SHA-256 hash check."""
    print(f"Testing POST /checkhash/batch with {len(hashes)} hashes...")
    data = {"hashes": hashes}

    response = requests.post(f"{BASE_URL}/checkhash/batch", json=data)
    print(f"Status: {response.status_code}")
    result = response.json()
    print(f"Total checked: {result['total']}")
    print(f"Found hashes: {result['found_hashes']}")
    print()


if __name__ == "__main__":
    print("=" * 60)
    print("HushFilter API Test Client")
    print("=" * 60)
    print()
    
    try:
        # Test basic endpoints
        test_health()
        test_stats()
        
        # Test single checks
        test_single_check_get("TESTUSER123")
        test_single_check_post("admin", "password123")
        
        # Test batch check
        test_batch_check([
            ("user1", ""),
            ("user2", "pass2"),
            ("user3", "test"),
        ])

        test_batch_checkhash([
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "1e8ce99fda5de7cb95dc4d32261ffbb6e495fcaffde224a0751efa45d4867c2d",
        ])
        
        print("=" * 60)
        print("All tests completed!")
        print("=" * 60)
        
    except requests.exceptions.ConnectionError:
        print("ERROR: Could not connect to API server.")
        print("Make sure the server is running with:")
        print("  uv run uvicorn api:app --reload")
    except Exception as e:
        print(f"ERROR: {e}")
