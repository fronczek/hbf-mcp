"""Synchronous JSON audit sink; failed writes prevent operation execution."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import threading

class AuditWriter:
    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        if self.path is not None:
            if not self.path.is_absolute():
                raise ValueError("Audit path must be absolute")
            self._secure_parent()

    def _secure_parent(self):
        assert self.path is not None
        for parent in self.path.parents:
            info = parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid())
                    or info.st_mode & 0o022):
                raise ValueError("Audit parents must be owned, real and not group/world writable")

    def __call__(self, event: dict) -> None:
        payload = (json.dumps(event, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
        with self._lock:
            if self.path is None:
                # The service journal receives stderr. Do not use Python logging,
                # whose handlers may suppress sink failures or filter INFO events.
                if os.write(2, payload) != len(payload):
                    raise OSError("Incomplete audit write")
                return
            self._secure_parent()
            descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT |
                                 getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_mode & 0o077 or info.st_nlink != 1):
                    raise ValueError("Audit file must be a private owned regular file")
                if os.write(descriptor, payload) != len(payload):
                    raise OSError("Incomplete audit write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
