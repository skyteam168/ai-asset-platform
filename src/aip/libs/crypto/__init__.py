"""数据保护：公司作用域信封加密（architecture.md 12.6）。"""

from aip.libs.crypto.envelope import decrypt_json, encrypt_json, is_envelope

__all__ = ["decrypt_json", "encrypt_json", "is_envelope"]
