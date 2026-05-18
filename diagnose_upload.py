#!/usr/bin/env python3
"""Diagnostic script to check upload endpoint and GEO_DATA_DIR setup."""
import os
import sys
import tempfile
from pathlib import Path

def check_env():
    print("=== Environment Variables ===")
    data_dir = os.getenv("GEO_DATA_DIR")
    jwt_secret = os.getenv("GEO_JWT_SECRET")
    print(f"GEO_DATA_DIR: {data_dir or '[NOT SET]'}")
    print(f"GEO_JWT_SECRET: {'[SET]' if jwt_secret else '[NOT SET]'}")

    if not data_dir:
        print("\n[WARNING] GEO_DATA_DIR is not set!")
        print("   Set it before running the backend:")
        print("   Windows: set GEO_DATA_DIR=C:\\tmp\\geo_data")
        print("   Linux:   export GEO_DATA_DIR=/tmp/geo_data")
        return False

    return True

def check_data_dir(data_dir_path):
    print(f"\n=== Data Directory: {data_dir_path} ===")
    data_dir = Path(data_dir_path)

    # Check if exists
    if not data_dir.exists():
        print(f"[ERROR] Directory does not exist")
        print(f"   Creating: mkdir -p {data_dir}")
        data_dir.mkdir(parents=True, exist_ok=True)
        print(f"[OK] Created")
    else:
        print(f"[OK] Exists")

    # Check permissions
    if not os.access(data_dir, os.R_OK | os.W_OK | os.X_OK):
        print(f"[ERROR] No read/write/execute permission")
        return False
    print(f"[OK] Readable and writable")

    # Check required subdirs
    for subdir in ["assets", "browser_states", "logs", "exports"]:
        subdir_path = data_dir / subdir
        if not subdir_path.exists():
            print(f"   Creating subdirectory: {subdir}")
            subdir_path.mkdir(parents=True, exist_ok=True)
    print(f"[OK] Subdirectories ready")

    # Test file I/O
    print(f"\n=== Testing File I/O ===")
    test_file = data_dir / ".test_write"
    try:
        test_file.write_text("test")
        test_file.unlink()
        print(f"[OK] Can write and delete files")
    except Exception as e:
        print(f"[ERROR] Cannot write: {e}")
        return False

    return True

def check_temp_file_location():
    print(f"\n=== Checking Temp File Location ===")
    with tempfile.NamedTemporaryFile(delete=False) as f:
        temp_loc = f.name
    Path(temp_loc).unlink()

    print(f"System temp directory: {tempfile.gettempdir()}")
    print(f"Note: In old code, temp files were created here (cross-disk copy issue)")

    # Check if temp and data_dir are on same filesystem
    data_dir = os.getenv("GEO_DATA_DIR")
    if data_dir:
        import stat
        data_stat = os.stat(data_dir)
        temp_stat = os.stat(tempfile.gettempdir())
        same_fs = data_stat.st_dev == temp_stat.st_dev
        print(f"Same filesystem: {'[Yes]' if same_fs else '[No - would cause slow uploads]'}")

def main():
    print("Geo Upload Diagnostic\n")

    if not check_env():
        return 1

    data_dir = os.getenv("GEO_DATA_DIR")
    if not check_data_dir(data_dir):
        return 1

    check_temp_file_location()

    print(f"\nSetup looks good!")
    print(f"\nNext steps:")
    print(f"1. Start backend: uvicorn server.app.main:app --reload --host 127.0.0.1 --port 8000")
    print(f"2. Start frontend: pnpm --filter @geo/web dev")
    print(f"3. Try uploading an image")
    print(f"\nIf still stuck:")
    print(f"- Check browser DevTools → Network tab (look for POST /api/assets)")
    print(f"- Check browser Console for errors")
    print(f"- Check backend logs for errors")

    return 0

if __name__ == "__main__":
    sys.exit(main())
