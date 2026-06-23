"""storage_state.json 的加密读写助手。

Playwright 的 storage_state 既接受文件路径也接受 dict——加密版读出内存 dict 喂给
new_context，不明文落盘。无密钥时 crypto 层透传，等价明文读写。
"""

from __future__ import annotations

import json
from pathlib import Path

from server.app.core.crypto import decrypt_bytes, encrypt_bytes


def read_state(path: Path) -> dict:
    raw = path.read_bytes()
    return json.loads(decrypt_bytes(raw).decode("utf-8"))


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    plain = json.dumps(state, ensure_ascii=False).encode("utf-8")
    path.write_bytes(encrypt_bytes(plain))
