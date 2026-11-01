"""Cross-process ownership guard for serve/admin using the same local data."""

import sys
from pathlib import Path
from typing import BinaryIO


class ProcessLock:
    def __init__(self, database: Path):
        self.path = database.with_suffix('.runtime.lock')
        self.stream: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True,exist_ok=True)
        stream = self.path.open('a+b')
        stream.write(b'0')
        stream.flush()
        stream.seek(0)
        try:
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise ValueError('Another serve/admin process owns this database; use its admin page.') from None
        self.stream = stream

    def release(self) -> None:
        if self.stream:
            self.stream.close()  # OS releases the advisory lock, including after a crash.
            self.stream = None
