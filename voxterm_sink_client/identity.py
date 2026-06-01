from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


def config_dir() -> Path:
    override = os.environ.get("VOXTERM_SINK_CLIENT_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "voxterm-sink-client"


def author_key_path() -> Path:
    return config_dir() / "author_ed25519.key"


class AuthorIdentity:
    def __init__(self, private_key: Ed25519PrivateKey):
        self.private_key = private_key

    @property
    def public_hex(self) -> str:
        return self.private_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        ).hex()

    def sign(self, message: bytes) -> str:
        return "ed25519:" + self.private_key.sign(message).hex()


def load_or_create_author(path: Path | None = None) -> AuthorIdentity:
    path = path or author_key_path()
    if path.exists():
        raw_hex = path.read_text(encoding="utf-8").strip()
        if len(raw_hex) != 64 or raw_hex.lower() != raw_hex:
            raise ValueError(f"invalid author key file: {path}")
        raw = bytes.fromhex(raw_hex)
        return AuthorIdentity(Ed25519PrivateKey.from_private_bytes(raw))

    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw.hex() + "\n")
    except Exception:
        try:
            path.unlink()
        finally:
            raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return AuthorIdentity(key)
