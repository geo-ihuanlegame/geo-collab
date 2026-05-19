#!/usr/bin/env python3
"""
Upload performance verification test
Tests various file sizes to ensure all optimizations work correctly
"""
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from PIL import Image
import io

# Config
API_BASE_URL = "http://127.0.0.1:8000/api"
TEST_FILES_DIR = Path("./test_uploads")
TEST_FILES_DIR.mkdir(exist_ok=True)

# Test file sizes to verify
TEST_CASES = [
    ("tiny", 100 * 1024),           # 100KB
    ("small", 1 * 1024 * 1024),     # 1MB
    ("medium", 10 * 1024 * 1024),   # 10MB
    ("large", 50 * 1024 * 1024),    # 50MB
]


def create_test_image(size_bytes: int, name: str) -> tuple[Path, str]:
    """Create a test PNG image of specified size"""
    file_path = TEST_FILES_DIR / f"test_{name}_{size_bytes // (1024*1024)}mb.png"

    if file_path.exists():
        # Reuse existing file
        with open(file_path, "rb") as f:
            sha256 = hashlib.sha256(f.read()).hexdigest()
        return file_path, sha256

    # Create image large enough to reach desired file size
    # Start with a base image and save with varying quality
    width = 256
    height = 256

    while True:
        img = Image.new('RGB', (width, height), color='red')

        # Add some pattern to avoid compression
        pixels = img.load()
        for i in range(width):
            for j in range(height):
                pixels[i, j] = (i % 256, j % 256, (i + j) % 256)

        # Save to bytes
        buffer = io.BytesIO()
        img.save(buffer, format='PNG', optimize=False)

        if buffer.tell() >= size_bytes:
            break

        # Increase dimensions
        width = int(width * 1.5)
        height = int(height * 1.5)

    # Write to file
    buffer.seek(0)
    with open(file_path, "wb") as f:
        f.write(buffer.getvalue())

    # Calculate SHA256
    with open(file_path, "rb") as f:
        sha256 = hashlib.sha256(f.read()).hexdigest()

    actual_size = file_path.stat().st_size
    print(f"  Created {file_path.name}: {actual_size / (1024*1024):.1f}MB")

    return file_path, sha256


def get_auth_token() -> str:
    """Get authentication token"""
    response = requests.post(
        f"{API_BASE_URL}/auth/login",
        json={"username": "admin", "password": "admin"}
    )
    if response.status_code != 200:
        raise Exception(f"Login failed: {response.text}")

    # Extract token from cookie
    cookies = response.cookies.get_dict()
    if "access_token" not in cookies:
        raise Exception("No access_token in response")

    return cookies["access_token"]


def upload_file(file_path: Path, auth_token: str, expected_sha256: str) -> dict:
    """Upload a file using chunked upload API"""
    file_size = file_path.stat().st_size
    print(f"\n📤 Uploading {file_path.name} ({file_size / (1024*1024):.1f}MB)...")

    session = requests.Session()
    session.cookies.set("access_token", auth_token)

    start_time = time.time()

    try:
        # Step 1: Start upload
        print("  Step 1: Initializing upload session...")
        init_response = session.post(
            f"{API_BASE_URL}/chunked-assets/upload-start",
            json={"total_size": file_size}
        )
        if init_response.status_code != 200:
            raise Exception(f"Upload init failed: {init_response.text}")

        upload_id = init_response.json()["upload_id"]
        chunk_size = init_response.json()["chunk_size"]
        chunk_count = init_response.json()["chunk_count"]
        print(f"  ✓ Upload ID: {upload_id}, Chunks: {chunk_count} × {chunk_size / (1024*1024):.1f}MB")

        # Step 2: Upload chunks
        print(f"  Step 2: Uploading {chunk_count} chunks...")
        with open(file_path, "rb") as f:
            for i in range(chunk_count):
                chunk_data = f.read(chunk_size)
                if not chunk_data:
                    break

                files = {"file": chunk_data}
                chunk_response = session.post(
                    f"{API_BASE_URL}/chunked-assets/upload-chunk/{upload_id}?chunk_index={i}",
                    files=files
                )

                if chunk_response.status_code != 200:
                    raise Exception(f"Chunk {i} upload failed: {chunk_response.text}")

                progress = (i + 1) / chunk_count * 100
                print(f"    [{i+1}/{chunk_count}] {progress:.0f}%", end="\r")

        print(f"    ✓ All {chunk_count} chunks uploaded")

        # Step 3: Complete upload
        print("  Step 3: Completing upload (merging chunks)...")
        complete_response = session.post(
            f"{API_BASE_URL}/chunked-assets/upload-complete/{upload_id}",
            json={
                "filename": file_path.name,
                "content_type": "image/png"
            }
        )

        if complete_response.status_code != 200:
            raise Exception(f"Upload completion failed: {complete_response.text}")

        result = complete_response.json()
        elapsed = time.time() - start_time

        # Verify
        server_sha256 = result.get("sha256", "")
        if server_sha256 != expected_sha256:
            raise Exception(
                f"SHA256 mismatch! Expected {expected_sha256}, got {server_sha256}"
            )

        throughput = file_size / elapsed / (1024 * 1024)  # MB/s

        print(f"  ✓ Upload complete!")
        print(f"    Time: {elapsed:.2f}s")
        print(f"    Throughput: {throughput:.1f} MB/s")
        print(f"    Asset ID: {result['id']}")
        print(f"    SHA256: {server_sha256[:16]}...")

        return {
            "status": "success",
            "file": file_path.name,
            "size": file_size,
            "time": elapsed,
            "throughput": throughput,
            "asset_id": result["id"],
            "sha256_match": True
        }

    except Exception as e:
        elapsed = time.time() - start_time
        print(f"  ✗ Upload failed: {e}")
        return {
            "status": "failed",
            "file": file_path.name,
            "size": file_size,
            "time": elapsed,
            "error": str(e)
        }


def main():
    """Run upload tests"""
    print("=" * 60)
    print("Upload Performance Verification Test")
    print("=" * 60)

    # Check API is running
    try:
        response = requests.get(f"{API_BASE_URL}/bootstrap")
        print(f"✓ API is running at {API_BASE_URL}\n")
    except Exception as e:
        print(f"✗ Cannot reach API at {API_BASE_URL}")
        print(f"  Error: {e}")
        print("\nMake sure backend is running:")
        print("  conda activate geo_xzpt")
        print("  uvicorn server.app.main:app --reload --host 127.0.0.1 --port 8000")
        sys.exit(1)

    # Get auth token
    print("Authenticating...")
    try:
        auth_token = get_auth_token()
        print(f"✓ Authenticated\n")
    except Exception as e:
        print(f"✗ Authentication failed: {e}")
        sys.exit(1)

    # Create test files
    print("Creating test files...")
    test_files = []
    for name, size in TEST_CASES:
        try:
            file_path, sha256 = create_test_image(size, name)
            test_files.append((file_path, sha256))
        except Exception as e:
            print(f"  ✗ Failed to create {name}: {e}")

    if not test_files:
        print("✗ No test files created")
        sys.exit(1)

    # Run upload tests
    print("\n" + "=" * 60)
    print("Running Upload Tests")
    print("=" * 60)

    results = []
    for file_path, sha256 in test_files:
        result = upload_file(file_path, auth_token, sha256)
        results.append(result)

    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)

    successful = [r for r in results if r["status"] == "success"]
    failed = [r for r in results if r["status"] == "failed"]

    print(f"\n✓ Successful: {len(successful)}/{len(results)}")
    for r in successful:
        print(f"  • {r['file']}: {r['size']/(1024*1024):.1f}MB in {r['time']:.2f}s ({r['throughput']:.1f} MB/s)")

    if failed:
        print(f"\n✗ Failed: {len(failed)}/{len(results)}")
        for r in failed:
            print(f"  • {r['file']}: {r['error']}")

    # Overall statistics
    if successful:
        avg_time = sum(r["time"] for r in successful) / len(successful)
        avg_throughput = sum(r["throughput"] for r in successful) / len(successful)
        total_size = sum(r["size"] for r in successful) / (1024 * 1024)

        print(f"\n📊 Statistics:")
        print(f"  Total uploaded: {total_size:.1f}MB")
        print(f"  Avg time: {avg_time:.2f}s")
        print(f"  Avg throughput: {avg_throughput:.1f} MB/s")

    # Cleanup
    print("\n🧹 Cleaning up test files...")
    for file_path, _ in test_files:
        file_path.unlink()

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
