from __future__ import annotations

import json
import os
import time
from pathlib import Path


class StateFileLock:
    def __init__(
        self,
        lock_path: Path,
        timeout_seconds: float = 10,
        stale_seconds: float = 120,
    ) -> None:
        self.lock_path = lock_path
        self.timeout_seconds = timeout_seconds
        self.stale_seconds = stale_seconds
        self._fd: int | None = None

    def __enter__(self) -> "StateFileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        while True:
            try:
                self._fd = os.open(str(self.lock_path), flags)
                owner = {
                    "pid": os.getpid(),
                    "created_at": time.time(),
                }
                os.write(self._fd, json.dumps(owner).encode("utf-8"))
                return
            except FileExistsError:
                self._remove_stale_lock()
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"state lock timeout: {self.lock_path}")
                time.sleep(0.05)

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass

    def _remove_stale_lock(self) -> None:
        try:
            age = time.time() - self.lock_path.stat().st_mtime
        except FileNotFoundError:
            return
        if age < self.stale_seconds:
            return
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass

