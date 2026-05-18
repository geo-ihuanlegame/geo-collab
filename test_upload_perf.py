#!/usr/bin/env python3
"""
Test image upload performance
Run: python test_upload_perf.py
"""
import time
import sys
from pathlib import Path
import io
import os

# Generate a valid JWT token for testing
def generate_test_token():
    """Generate a JWT token for testing."""
    from datetime import datetime, timedelta, timezone
    from jose import jwt

    user_id = 1
    role = "admin"
    secret = os.environ.get("GEO_JWT_SECRET", "test-secret-key-for-testing-only")

    expire = datetime.now(timezone.utc) + timedelta(hours=24)
    payload = {"sub": str(user_id), "role": role, "exp": expire}

    token = jwt.encode(payload, secret, algorithm="HS256")
    return token

# Create test PNG image (minimal valid PNG header)
def create_test_png(size_mb: int = 2) -> bytes:
    """Create a minimal valid PNG for testing."""
    import struct
    import zlib

    # PNG header
    png = b'\x89PNG\r\n\x1a\n'

    # IHDR chunk (image header)
    width = 1024
    height = 1024
    ihdr_data = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    ihdr_crc = zlib.crc32(b'IHDR' + ihdr_data) & 0xffffffff
    png += struct.pack('>I', len(ihdr_data)) + b'IHDR' + ihdr_data + struct.pack('>I', ihdr_crc)

    # IDAT chunk (image data) - pad to reach desired size
    raw_data = b'\x00' * (width * height * 3)  # white pixels
    compressed = zlib.compress(raw_data, 9)

    # Pad to reach target size
    target_size = size_mb * 1024 * 1024
    if len(compressed) < target_size - 1000:
        compressed += b'\x00' * (target_size - len(png) - len(compressed) - 1000)

    idat_crc = zlib.crc32(b'IDAT' + compressed) & 0xffffffff
    png += struct.pack('>I', len(compressed)) + b'IDAT' + compressed + struct.pack('>I', idat_crc)

    # IEND chunk
    iend_crc = zlib.crc32(b'IEND') & 0xffffffff
    png += struct.pack('>I', 0) + b'IEND' + struct.pack('>I', iend_crc)

    return png

def test_upload(file_size_mb: int = 2):
    """Test upload speed to local development server."""
    import urllib.request
    import urllib.error
    import json

    url = "http://127.0.0.1:8000/api/assets"

    # Generate test PNG
    print(f"Creating test PNG ({file_size_mb}MB)...", end="", flush=True)
    png_data = create_test_png(file_size_mb)
    print(f" Done ({len(png_data)} bytes)")

    print(f"\nUploading to {url}...")
    start = time.time()

    try:
        # Generate a valid JWT token
        token = generate_test_token()

        # Create multipart form data
        boundary = '----WebKitFormBoundary' + ''.join([f"{i:x}" for i in range(16)])
        body = (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="file"; filename="test.png"\r\n'
            f'Content-Type: image/png\r\n\r\n'
        ).encode() + png_data + f'\r\n--{boundary}--\r\n'.encode()

        req = urllib.request.Request(url, data=body)
        req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
        req.add_header('Cookie', f'access_token={token}')

        with urllib.request.urlopen(req, timeout=60) as response:
            elapsed = time.time() - start
            status = response.status
            response_data = response.read().decode()

            print(f"Status: {status}")
            print(f"Time: {elapsed:.2f}s")
            print(f"Speed: {(len(png_data) / elapsed / 1024 / 1024):.2f} MB/s")

            if status == 200:
                data = json.loads(response_data)
                print(f"Asset ID: {data.get('id', 'N/A')}")
                print("\n[OK] Upload successful!")
                return elapsed
            else:
                print(f"Error: {response_data}")
                return None

    except urllib.error.HTTPError as e:
        elapsed = time.time() - start
        print(f"HTTP Error {e.code}")
        print(f"Time: {elapsed:.2f}s")
        print(f"Speed: {(len(png_data) / elapsed / 1024 / 1024):.2f} MB/s")
        print(f"Note: {e.read().decode()}")
        return elapsed if elapsed > 0 else None

    except urllib.error.URLError as e:
        print(f"[ERROR] Cannot connect to server at {url}")
        print(f"  Reason: {e.reason}")
        print("  Make sure to start the backend with:")
        print("  python -m uvicorn server.app.main:app --host 127.0.0.1 --port 8000")
        return None
    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback
        traceback.print_exc()
        return None

if __name__ == "__main__":
    # Test different file sizes
    sizes = [1, 2, 5]
    print("=" * 60)
    print("Image Upload Performance Test")
    print("=" * 60)

    results = []
    for size in sizes:
        elapsed = test_upload(size)
        if elapsed:
            results.append((size, elapsed))
        print()

    if results:
        print("=" * 60)
        print("Summary:")
        print("=" * 60)
        for size, elapsed in results:
            speed = (size * 1024 / elapsed)
            print(f"{size}MB: {elapsed:.2f}s ({speed:.1f} KB/s)")
