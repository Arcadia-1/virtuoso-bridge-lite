"""Descriptor-anchored private Linux directories (Python 2.7 and 3).

System ancestors may be root-owned and traversable. A root-owned sticky
directory such as /tmp is safe above a private, account-owned child. Never
chmod existing paths: reject unsafe roots instead of legitimizing planted code.
"""
import errno
import os
import stat


def check_private(info, path):
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("Private directory requires current owner and mode 0700: " + path)


def _open_child(parent_fd, name, create):
    path = "/proc/self/fd/%d/%s" % (parent_fd, name)
    if create:
        try:
            os.mkdir(path, 0o700)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def private_child(parent_fd, name, create=False):
    if not name or name in (".", "..") or "/" in name:
        raise ValueError("Invalid private child name")
    fd = _open_child(parent_fd, name, create)
    try:
        check_private(os.fstat(fd), name)
        return fd
    except BaseException:
        os.close(fd)
        raise


def private_directory(path, create=False):
    """Walk from / without following symlinks, retaining the verified leaf FD."""
    if not os.path.isabs(path):
        raise ValueError("Private directory path must be absolute")
    parts = [part for part in path.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        raise ValueError("Invalid private directory path: " + path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(parts):
            ancestor = os.fstat(fd)
            sticky_root = ancestor.st_uid == 0 and ancestor.st_mode & stat.S_ISVTX
            if (ancestor.st_uid not in (0, os.geteuid())
                    or (ancestor.st_mode & 0o022 and not sticky_root)):
                raise ValueError("Unsafe directory ancestor: " + path)
            child = _open_child(fd, part, create)
            os.close(fd)
            fd = child
            if index == len(parts) - 1:
                check_private(os.fstat(fd), path)
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)
