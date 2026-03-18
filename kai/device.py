"""
Machine certificate — a 30-byte random key generated once per Kai installation.
Stored in the memory directory as a hex file.

Security model:
  - Generated on first launch, never changes, never leaves the machine.
  - When a user registers, SHA-256(machine_key) is stored alongside their account.
  - Login requires: correct name + correct PIN + machine key must match registration.
  - If someone copies kai.db to another machine and tries name+PIN, it fails —
    the machine key on that machine will produce a different hash.
  - The plain key is never sent to the browser or stored in the database.
    Only its hash lives in the DB.
"""
import hashlib
import secrets
from kai.config import MEMORY_DIR

_KEY_FILE = MEMORY_DIR / "device.key"
_device_key: bytes | None = None


def get_key() -> bytes:
    """Load or generate the machine key. Cached in memory after first call."""
    global _device_key
    if _device_key is not None:
        return _device_key

    if _KEY_FILE.exists():
        raw = _KEY_FILE.read_text().strip()
        _device_key = bytes.fromhex(raw)
    else:
        # 30 random bytes = 60-char hex string = 240 bits of entropy
        _device_key = secrets.token_bytes(30)
        _KEY_FILE.write_text(_device_key.hex())
        try:
            import stat
            _KEY_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)   # owner read/write only
        except Exception:
            pass   # Windows may not support chmod — acceptable
        print(f"[+] Machine certificate created: {_KEY_FILE}")

    return _device_key


def key_hash() -> str:
    """
    SHA-256 of the machine key.
    This is what gets stored in the DB — never the key itself.
    """
    return hashlib.sha256(get_key()).hexdigest()
