#!/usr/bin/env python3
"""
测试分块上传性能
运行: python test_chunked_upload.py
"""
import time
import asyncio
from pathlib import Path
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


async def test_chunked_upload(file_size_mb: int = 10):
    """Test chunked upload speed."""
    import aiohttp

    url_base = "http://127.0.0.1:8000/api/chunked-assets"

    # Generate test PNG
    print(f"Creating test PNG ({file_size_mb}MB)...", end="", flush=True)
    png_data = create_test_png(file_size_mb)
    print(f" Done ({len(png_data)} bytes)")

    total_size = len(png_data)
    chunk_size = 3 * 1024 * 1024  # 3MB

    print(f"\nStarting chunked upload ({file_size_mb}MB, 3MB chunks)...")
    start = time.time()

    try:
        token = generate_test_token()
        headers = {"Cookie": f"access_token={token}"}

        async with aiohttp.ClientSession() as session:
            # 1. 初始化上传
            print("  [1/3] Initializing upload...", end="", flush=True)
            init_data = {
                "total_size": total_size,
            }
            async with session.post(
                f"{url_base}/upload-start",
                json=init_data,
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    print(f" FAILED ({resp.status})")
                    return
                init_result = await resp.json()
                upload_id = init_result["upload_id"]
                chunk_count = init_result["chunk_count"]
                print(f" OK (upload_id={upload_id}, chunks={chunk_count})")

            # 2. 上传分块（4 并发）
            print(f"  [2/3] Uploading {chunk_count} chunks (4 concurrent)...")
            uploaded_chunks = []
            chunk_times = {}

            async def upload_chunk(index: int):
                start_byte = index * chunk_size
                end_byte = min(start_byte + chunk_size, total_size)
                chunk_data = png_data[start_byte:end_byte]
                chunk_size_bytes = len(chunk_data)

                form_data = aiohttp.FormData()
                form_data.add_field('file', chunk_data, filename=f'chunk_{index}')

                chunk_start = time.time()
                async with session.post(
                    f"{url_base}/upload-chunk/{upload_id}?chunk_index={index}",
                    data=form_data,
                    headers=headers,
                ) as resp:
                    chunk_elapsed = time.time() - chunk_start
                    chunk_times[index] = (chunk_elapsed, chunk_size_bytes)

                    if resp.status == 200:
                        speed = (chunk_size_bytes / 1024 / 1024) / chunk_elapsed if chunk_elapsed > 0 else 0
                        print(f"    Chunk {index}: {chunk_elapsed:.2f}s ({speed:.2f} MB/s)")
                        uploaded_chunks.append(index)
                        return True
                    return False

            # 并发上传
            chunk_indices = list(range(chunk_count))
            from asyncio import Semaphore
            semaphore = Semaphore(4)

            async def limited_upload(index):
                async with semaphore:
                    return await upload_chunk(index)

            results = await asyncio.gather(*[limited_upload(i) for i in chunk_indices])
            if not all(results):
                print(f"    FAILED")
                return
            print(f"    OK ({len(uploaded_chunks)}/{chunk_count})")

            # 显示预热效果
            if chunk_times:
                first_chunk_speed = (chunk_times[0][1] / 1024 / 1024) / chunk_times[0][0]
                last_chunk_speed = (chunk_times[max(chunk_times.keys())][1] / 1024 / 1024) / chunk_times[max(chunk_times.keys())][0]
                print(f"    Warmup effect: 1st chunk {first_chunk_speed:.2f} MB/s → last chunk {last_chunk_speed:.2f} MB/s ({last_chunk_speed/first_chunk_speed:.1f}x)")

            # 3. 完成上传
            print(f"  [3/3] Completing upload...", end="", flush=True)
            complete_data = {
                "filename": "test.png",
                "content_type": "image/png",
            }
            async with session.post(
                f"{url_base}/upload-complete/{upload_id}",
                json=complete_data,
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    print(f" FAILED ({resp.status})")
                    error = await resp.json()
                    print(f"  Error: {error.get('detail', 'Unknown error')}")
                    return
                result = await resp.json()
                print(f" OK (asset_id={result['id']})")

            elapsed = time.time() - start
            speed = (file_size_mb / elapsed)
            print(f"\n[SUCCESS] Upload complete!")
            print(f"  Time: {elapsed:.2f}s")
            print(f"  Speed: {speed:.2f} MB/s")
            return elapsed

    except Exception as e:
        elapsed = time.time() - start
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        return None


if __name__ == "__main__":
    print("=" * 60)
    print("Chunked Upload Performance Test")
    print("=" * 60)

    sizes = [10, 20]
    for size in sizes:
        elapsed = asyncio.run(test_chunked_upload(size))
        print()
        if elapsed:
            print(f"✓ {size}MB uploaded in {elapsed:.2f}s ({(size/elapsed):.2f} MB/s)")
        else:
            print(f"✗ {size}MB upload failed")
        print()
