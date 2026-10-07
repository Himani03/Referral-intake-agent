"""Append-only, tamper-evident audit log with no PHI in it.

Each entry carries the SHA-256 of the previous entry, so editing or deleting any line breaks
the chain and `verify()` reports exactly where. Patient identifiers are replaced by a keyed
HMAC, so the log can show *that* the agent touched a record (and which one, if you hold the key)
without the log itself becoming a PHI store.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _key() -> bytes:
    k = os.environ.get("AUDIT_HMAC_KEY")
    if not k:
        raise RuntimeError("Set AUDIT_HMAC_KEY (e.g. from your secret manager). It is never written to the log.")
    return k.encode()


def pseudonym(value: str) -> str:
    """Stable, non-reversible reference to a patient or other PHI value."""
    return "pt_" + hmac.new(_key(), value.strip().lower().encode(), hashlib.sha256).hexdigest()[:16]


class AuditLog:
    def __init__(self, path: Path, actor: str):
        self.path = Path(path)
        self.actor = actor
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _last_hash(self) -> str:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return GENESIS
        with self.path.open("rb") as f:
            last = f.read().splitlines()[-1]
        return json.loads(last)["hash"]

    def record(self, action: str, outcome: str = "ok", subject: str | None = None, **detail) -> dict:
        """action: e.g. emr.login, emr.patient.create, emr.referral.submit. detail must not contain PHI."""
        with self._lock:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "actor": self.actor,
                "action": action,
                "outcome": outcome,
                "subject": subject,
                "detail": detail,
                "prev": self._last_hash(),
            }
            body = json.dumps(entry, sort_keys=True, separators=(",", ":"))
            entry["hash"] = hashlib.sha256(body.encode()).hexdigest()
            with self.path.open("a") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return entry


def verify(path: Path) -> tuple[bool, str]:
    prev, n = GENESIS, 0
    path = Path(path)
    if not path.exists():
        return True, "empty log"
    for n, line in enumerate(path.read_text().splitlines(), 1):
        entry = json.loads(line)
        claimed = entry.pop("hash")
        if entry["prev"] != prev:
            return False, f"line {n}: chain broken (entry removed or reordered)"
        body = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(body.encode()).hexdigest() != claimed:
            return False, f"line {n}: contents modified"
        prev = claimed
    return True, f"{n} entries verified"
