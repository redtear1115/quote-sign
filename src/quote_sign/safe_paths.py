"""Descriptor-relative directory traversal and private atomic writes."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
from uuid import uuid4

from .errors import InputNotFound, InputRejected


def expand_system_alias(path: str, lstat=os.lstat, realpath=os.path.realpath) -> str:
    if not path.startswith("/"):
        return path
    name = path[1:].split("/", 1)[0]
    if not name:
        return path
    first = "/" + name
    try:
        info = lstat(first)
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
            return path
        target = realpath(first)
        if not target.startswith("/private/"):
            return path
        child = target[len("/private/"):]
        if not child or "/" in child:
            return path
        return target + path[len(first):]
    except Exception:
        return path


def _open_component(name, parent_fd, create, mode):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        try:
            return os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(name, mode, dir_fd=parent_fd)
            except FileExistsError:
                pass
            return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except (OSError, ValueError, TypeError, NotImplementedError):
            info = None
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise InputRejected("路徑含符號連結") from None
        if info is not None and not stat.S_ISDIR(info.st_mode):
            raise InputRejected("不是目錄") from None
        if exc.errno == errno.ENOENT and not create:
            raise InputNotFound() from None
        raise InputRejected("無法安全開啟") from None


def _walk_dir(path, create=False, mode=0o777, preflight=False):
    fd = None
    try:
        path = Path(path)
        if ".." in path.parts:
            raise InputRejected("路徑含 ..")
        path = Path(expand_system_alias(str(path)))
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        fd = os.open("/" if path.is_absolute() else ".", flags)
        for part in path.parts:
            if part in (path.anchor, ".", ""):
                continue
            try:
                child = _open_component(part, fd, create, mode)
            except InputNotFound:
                if preflight:
                    return None
                raise
            parent, fd = fd, child
            os.close(parent)
        if preflight:
            return None
        result, fd = fd, None
        return result
    except OSError:
        raise InputRejected("無法安全開啟") from None
    except (ValueError, TypeError, NotImplementedError, AttributeError):
        raise InputRejected("無法安全開啟") from None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                raise InputRejected("無法安全開啟") from None


def open_dir(path, create=False, mode=0o777) -> int:
    return _walk_dir(path, create=create, mode=mode)


def check_out_dir(path) -> None:
    _walk_dir(path, preflight=True)


def write_all(fd, data):
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError(errno.EIO, "write failed")
        remaining = remaining[written:]


def atomic_write_at(directory_fd, name, data):
    """Replace a bare filename without following its previous target."""
    temp = ".quote-sign-" + uuid4().hex + ".tmp"
    fd = None
    created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(temp, flags, 0o600, dir_fd=directory_fd)
        created = True
        write_all(fd, data)
        os.fsync(fd)
        closing, fd = fd, None
        os.close(closing)
        os.replace(temp, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        created = False
    finally:
        try:
            if fd is not None:
                os.close(fd)
        finally:
            if created:
                os.unlink(temp, dir_fd=directory_fd)
