"""Private local files with advisory process locks and symlink refusal."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import tempfile
import threading


def refuse_symlinks(path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise OSError("symlink refused")


class PrivateStore:
    def __init__(self, path, lock_name, error):
        self.path = Path(path)
        self.lock_path = self.path.with_name(lock_name)
        self.error = error
        self._mutex = threading.RLock()
        self._depth = 0

    def guard(self):
        try:
            refuse_symlinks(self.path)
            refuse_symlinks(self.lock_path)
        except OSError:
            raise self.error("拒絕使用符號連結紀錄路徑") from None

    @contextmanager
    def lock(self):
        with self._mutex:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            fd = None
            try:
                self.guard()
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                self.guard()
                self._depth = 1
                yield
            except OSError:
                raise self.error("無法存取本機紀錄") from None
            finally:
                self._depth = 0
                if fd is not None:
                    os.close(fd)


def atomic_write(path, content):
    path = Path(path)
    refuse_symlinks(path)
    temp = None
    try:
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".quote-sign-")
        with os.fdopen(fd, "wb") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        refuse_symlinks(path)
        os.replace(temp, path)
    finally:
        if temp is not None:
            Path(temp).unlink(missing_ok=True)
