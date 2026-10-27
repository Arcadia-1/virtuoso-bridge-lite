"""Private per-UID credential provisioning; Python 2.7/3, no secret logging."""
import binascii
import errno
import os
import stat
import tempfile


def default_path():
    import pwd
    return os.path.join(pwd.getpwuid(os.geteuid()).pw_dir,
                        ".virtuoso-bridge", "bridge_token")


def read_existing(path=None):
    """Validate and read without creating files or changing permissions."""
    path = path or default_path()
    parent = os.path.dirname(path)
    info = os.lstat(parent)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise OSError("credential directory must be private and owned by the current UID")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise OSError("credential must be a private regular file owned by the current UID")
        data = os.read(fd, 66).decode("ascii").strip()
        if len(data) != 64 or any(c not in "0123456789abcdefABCDEF" for c in data):
            raise OSError("invalid credential file")
        return data.lower()
    finally:
        os.close(fd)


def read_or_create(path=None):
    path = path or default_path()
    parent = os.path.dirname(path)
    try:
        os.mkdir(parent, 0o700)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
    info = os.lstat(parent)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise OSError("credential directory must be owned by the current UID and not a symlink")
    os.chmod(parent, 0o700)

    def read():
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise OSError("credential must be a private regular file owned by the current UID")
            os.fchmod(fd, 0o600)
            data = os.read(fd, 66).decode("ascii").strip()
            if len(data) != 64 or any(c not in "0123456789abcdefABCDEF" for c in data):
                raise OSError("invalid credential file")
            return data.lower()
        finally:
            os.close(fd)

    try:
        return read()
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise
    fd, temporary = tempfile.mkstemp(prefix=".bridge-token-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(binascii.hexlify(os.urandom(32)) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
    finally:
        os.unlink(temporary)
    return read()


if __name__ == "__main__":
    # Used ONLY by the authenticated SSH command, never by the CIW launcher.
    import sys
    print(read_existing() if "--read-only" in sys.argv else read_or_create())
