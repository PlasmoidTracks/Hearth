"""Workspace encryption at rest — opt-in, transparent, best-effort.

Boot config (see config.py): HEARTH_WORKSPACE_ENCRYPTION=true plus either a raw
32-byte hex key (HEARTH_WORKSPACE_ENC_KEY) or a passphrase (HEARTH_WORKSPACE_ENC_PASS,
key = scrypt(pass, salt=secret_key)). When enabled, workspaces.py encrypts every file
written into the jail and decrypts on read — plaintext never touches disk.

Format:  b"HEENC1" || nonce(24) || ciphertext+tag
Cipher:  XChaCha20-Poly1305 via the `cryptography` package (add it to requirements
when you turn this on). If the library is missing we RAISE at first use rather than
silently storing plaintext — fail closed, never leak.

Honest threat model: this defends against disk theft, lost devices, and leaked
backups. It does NOT hide data from someone with access to the running server and
its RAM (the key lives in process memory by necessity), and the exec sandbox sees
ciphertext files. Per-user keys are a planned follow-up (crypto-shredding).
"""
from __future__ import annotations
import hashlib
import os

MAGIC = b"HEENC1"
NONCE_LEN = 24


def encryption_enabled() -> bool:
    from .config import get_boot_settings
    return bool(get_boot_settings().workspace_encryption)


_key_cache: bytes | None = None


def _load_key() -> bytes:
    global _key_cache
    if _key_cache is not None:
        return _key_cache
    from .config import get_boot_settings
    bs = get_boot_settings()
    if not bs.workspace_encryption:
        raise RuntimeError("workspace encryption disabled")
    hexkey = (bs.workspace_enc_key or "").strip()
    if hexkey:
        try:
            key = bytes.fromhex(hexkey)
        except ValueError as e:
            raise RuntimeError("HEARTH_WORKSPACE_ENC_KEY must be 64 hex chars") from e
        if len(key) != 32:
            raise RuntimeError("HEARTH_WORKSPACE_ENC_KEY must decode to exactly 32 bytes")
    elif bs.workspace_enc_pass:
        dk = hashlib.scrypt(bs.workspace_enc_pass.encode(), salt=bs.secret_key.encode()[:16],
                            n=1 << 14, r=8, p=1, dklen=32, maxmem=(1 << 25))
        key = dk
    else:
        raise RuntimeError("workspace encryption enabled but no key/passphrase configured")
    _key_cache = key
    return key


def _cipher():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "workspace encryption requires the 'cryptography' package (pip install cryptography)"
        ) from e
    return ChaCha20Poly1305(_load_key())


def encrypt_bytes(data: bytes) -> bytes:
    if not encryption_enabled():
        return data
    nonce = os.urandom(NONCE_LEN)
    return MAGIC + nonce + _cipher().encrypt(nonce, data, None)


def decrypt_bytes(blob: bytes) -> bytes:
    """Decrypt if our framing is present; pass through unframed data unchanged.

    Pass-through for unframed files means turning encryption ON mid-life never
    corrupts reads of pre-existing plaintext (they get encrypted on next write).
    """
    if not blob.startswith(MAGIC):
        return blob
    nonce = blob[len(MAGIC):len(MAGIC) + NONCE_LEN]
    ct = blob[len(MAGIC) + NONCE_LEN:]
    return _cipher().decrypt(nonce, ct, None)


def is_encrypted_blob(blob: bytes) -> bool:
    return blob.startswith(MAGIC)
