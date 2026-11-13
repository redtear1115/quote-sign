from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import stat

from .errors import InputNotFound, InputRejected
from .safe_paths import expand_system_alias

EXIT_INPUT_REJECTED = 3


@dataclass(frozen=True, repr=False)
class InputDoc:
    name: str
    data: bytes

    def __post_init__(self):
        # export_pdf joins this name onto a private temp folder: it must be a bare file name.
        if (not isinstance(self.name, str) or self.name in ("", ".", "..")
                or os.path.basename(self.name) != self.name or "\x00" in self.name):
            raise InputRejected("無法安全開啟")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def __repr__(self) -> str:
        return f"InputDoc(name={self.name!r})"


def _open_component(name: str, flags: int, parent_fd: int) -> int:
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        try:
            mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
        except (OSError, ValueError, TypeError, NotImplementedError):
            mode = None
        if mode is not None and stat.S_ISLNK(mode):
            raise InputRejected("路徑含符號連結") from None
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            raise InputNotFound() from None
        if mode is not None and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise InputRejected("不是一般檔案") from None
        raise InputRejected("無法安全開啟") from None


def load_markdown(path: Path) -> InputDoc:
    name = path.name
    if len(name) <= 3 or not name.endswith((".md", ".MD")):
        raise InputRejected("副檔名不是 .md")
    if ".." in path.parts:
        raise InputRejected("路徑含 ..")
    path = Path(expand_system_alias(str(path)))

    fds = []
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        fds.append(os.open("/" if path.is_absolute() else ".", directory_flags))
        components = [part for part in path.parts if part not in (path.anchor, ".", "")]
        for component in components[:-1]:
            fds.append(_open_component(component, directory_flags | os.O_NOFOLLOW, fds[-1]))
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
        fds.append(_open_component(name, flags, fds[-1]))
        info = os.fstat(fds[-1])
        if not stat.S_ISREG(info.st_mode):
            raise InputRejected("不是一般檔案")
        if info.st_nlink != 1:
            raise InputRejected("有多個硬連結")
        chunks = []
        while True:
            chunk = os.read(fds[-1], 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return InputDoc(name, b"".join(chunks))
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            raise InputNotFound() from None
        raise InputRejected("無法安全開啟") from None
    except (ValueError, TypeError, NotImplementedError, AttributeError):
        # Unsupported descriptor-relative opens must fail closed.
        raise InputRejected("無法安全開啟") from None
    finally:
        close_failed = False
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                close_failed = True
        if close_failed:
            raise InputRejected("無法安全開啟") from None
