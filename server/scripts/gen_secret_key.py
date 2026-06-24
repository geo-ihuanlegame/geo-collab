"""打印一个新的 Fernet 密钥，填入 GEO_SECRET_KEY。

用法：python -m server.scripts.gen_secret_key
"""

from __future__ import annotations

from cryptography.fernet import Fernet


def main() -> None:
    print(Fernet.generate_key().decode("ascii"))


if __name__ == "__main__":
    main()
