"""Publish a generated account bundle under a bounded Linux lock.

Executed by the client, not by a CIW. Keep compatible with Python 2.7.
Old bundles are retained as private backups on explicit replacement.
"""
from __future__ import print_function

import errno
import fcntl
import json
import os
import stat
import sys
import tempfile
import time
import uuid

try:
    from .ramic_paths import private_directory, check_private
except (ImportError, ValueError):
    from ramic_paths import private_directory, check_private


def _private_directory(path):
    info = os.lstat(path)
    check_private(info, path)


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022 or info.st_size > 16 * 1024 * 1024):
            raise ValueError("Unsafe deployment file: " + path)
        with os.fdopen(fd, "rb") as stream:
            fd = None
            return stream.read()
    finally:
        if fd is not None:
            os.close(fd)


def publish(destination, files, replace=False, timeout=10):
    root, leaf = os.path.split(destination.rstrip("/"))
    if not leaf or leaf in (".", ".."):
        raise ValueError("Invalid deployment destination")
    root_fd = private_directory(root, create=True)
    anchor = "/proc/self/fd/%d" % root_fd
    try:
        result = _publish_at(anchor + "/" + leaf, files, replace, timeout, root_fd)
        for key in ("path", "backup"):
            if result.get(key):
                result[key] = root + result[key][len(anchor):]
        result["warnings"] = [warning.replace(anchor, root) for warning in result.get("warnings", [])]
        return result
    except Exception as exc:
        # Never report a closed /proc descriptor as an operator recovery path.
        exc.args = tuple(arg.replace(anchor, root) if isinstance(arg, str) else arg for arg in exc.args)
        if isinstance(exc, OSError):
            for name in ("filename", "filename2"):
                value = getattr(exc, name, None)
                if isinstance(value, str):
                    setattr(exc, name, value.replace(anchor, root))
        raise
    finally:
        os.close(root_fd)


def _publish_at(destination, files, replace, timeout, root_fd):
    """Compare before mutation; stage all files, then publish under one lock.

    Directory rename is atomic per step, not a multi-directory transaction.
    A durable recovery name preserves the old complete directory across the
    exchange gap. The next invocation restores it if publication was interrupted.
    """
    root, leaf = os.path.split(destination.rstrip("/"))
    if not leaf or leaf in (".", ".."):
        raise ValueError("Invalid deployment destination")
    for name in files:
        if not name or os.path.basename(name) != name or name in (".", ".."):
            raise ValueError("Invalid bundle member")
    lock = os.path.join(root, "." + leaf + "-deploy.lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    stage = None
    directory_fd = None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError("Unsafe deployment lock: " + lock)
        clock = getattr(time, "monotonic", lambda: os.times()[4])
        deadline = clock() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except IOError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES) or clock() >= deadline:
                    raise RuntimeError("Timed out waiting for deployment lock: " + lock)
                time.sleep(0.05)
        directory_fd = os.dup(root_fd)
        recovery = os.path.join(root, "." + leaf + "-recover")
        if os.path.lexists(recovery):
            _private_directory(recovery)
            if not os.path.lexists(destination):
                os.rename(recovery, destination)
            else:
                os.rename(recovery, recovery + "-" + uuid.uuid4().hex)
            os.fsync(directory_fd)
        differences = []
        existed = os.path.lexists(destination)
        if existed:
            _private_directory(destination)
            differences.extend(sorted(set(os.listdir(destination)) - set(files)))
            for name, content in sorted(files.items()):
                expected = content.encode("utf-8")
                try:
                    actual = _read(os.path.join(destination, name))
                except OSError as exc:
                    if exc.errno != errno.ENOENT:
                        raise
                    actual = None
                if actual != expected:
                    differences.append(name)
            if not differences:
                return {"reused": True, "path": destination}
            if not replace:
                raise RuntimeError("Generated account bundle differs at %s (%s). "
                                   "Inspect the configuration; use start --replace-setup explicitly, "
                                   "or install a new versioned shared bundle. No bundle file was changed."
                                   % (repr(destination), ", ".join(repr(name) for name in differences)))
        stage = tempfile.mkdtemp(prefix="." + leaf + "-stage-", dir=root)
        for name, content in sorted(files.items()):
            path = os.path.join(stage, name)
            out = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(out, "wb") as stream:
                stream.write(content.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
        stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        if existed:
            os.rename(destination, recovery)
            os.fsync(directory_fd)
        committed = False
        try:
            os.rename(stage, destination)
            stage = None
            committed = True
            os.fsync(directory_fd)
        except BaseException:
            if committed:
                raise RuntimeError("Bundle published at %r but durability could not be confirmed; "
                                   "inspect active and recovery paths before retrying" % destination)
            if existed and not os.path.lexists(destination):
                os.rename(recovery, destination)
                os.fsync(directory_fd)
            raise
        backup = None
        warnings = []
        if existed:
            backup = recovery
            try:
                retained = recovery + "-" + uuid.uuid4().hex
                os.rename(recovery, retained)
                backup = retained
                os.fsync(directory_fd)
            except OSError as exc:
                warnings.append("Bundle published; inspect retained backup %r: %s" % (backup, exc))
        return {"reused": False, "path": destination, "backup": backup,
                "changed": differences, "warnings": warnings}
    finally:
        try:
            if stage is not None:
                # Preserve the primary error and leave failed cleanup inspectable.
                try:
                    for name in files:
                        path = os.path.join(stage, name)
                        if os.path.lexists(path):
                            os.unlink(path)
                    os.rmdir(stage)
                except OSError as exc:
                    sys.stderr.write("Unpublished stage retained at %r: %s\n" % (stage, exc))
        finally:
            for descriptor in (directory_fd, fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError as exc:
                        sys.stderr.write("Deployment descriptor cleanup failed: %s\n" % exc)


if __name__ == "__main__":
    try:
        payload = json.loads(_read(sys.argv[1]).decode("utf-8")) if len(sys.argv) > 1 else json.load(sys.stdin)
        print(json.dumps(publish(payload["destination"], payload["files"], payload.get("replace", False))))
    except Exception as exc:
        sys.stderr.write(str(exc) + "\n")
        sys.exit(1)
