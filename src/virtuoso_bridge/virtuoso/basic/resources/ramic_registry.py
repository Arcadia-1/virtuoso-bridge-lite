"""Private Linux instance discovery; OS sockets alone allocate ports.

Python 2.7/3. Record locks mark liveness only; no directory/allocation lock.
Locked records are atomically published so sweepers never see an unlocked
record under construction. All child paths are anchored to an open directory.
"""
from __future__ import print_function

import atexit
import errno
import hashlib
import json
import os
import re
import stat
import time
import uuid


def _flock(fd, operation):
    import fcntl
    fcntl.flock(fd, operation | fcntl.LOCK_NB)


def _same_file(fd, path):
    a, b = os.fstat(fd), os.lstat(path)
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _private_directory(path, create):
    if create:
        try:
            os.mkdir(path, 0o700)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(fd)
        raise RuntimeError("Instance scratch directory must be owned by this UID with mode 0700: %s. "
                           "Use a private child directory, not /tmp itself." % path)
    return fd


def scratch_root():
    import pwd
    root = os.environ.get("RB_SCRATCH_ROOT") or os.environ.get("VB_REMOTE_SCRATCH_ROOT") or "/tmp"
    if root.rstrip("/") == "/tmp":
        user = re.sub(r"[^a-zA-Z0-9._-]", "_", pwd.getpwuid(os.geteuid()).pw_name)[:64]
        root = "/tmp/virtuoso_bridge_" + user
    return expand_root(root)


def expand_root(root):
    import pwd
    if root.startswith("~/"):
        root = os.path.join(pwd.getpwuid(os.geteuid()).pw_dir, root[2:])
    if not os.path.isabs(root):
        raise ValueError("Scratch root must be absolute or start with ~/")
    return root


class Registry(object):
    def __init__(self, path=None, create=True, root=None, legacy=False):
        if path is None:
            import pwd
            root = expand_root(root) if root else scratch_root()
            root_fd = _private_directory(root, create)
            try:
                if legacy:
                    username = re.sub(r"[^a-zA-Z0-9._-]", "_", pwd.getpwuid(os.geteuid()).pw_name)[:64]
                    parent_fd = _private_directory("/proc/self/fd/%d/virtuoso_bridge_%s" % (root_fd, username), False)
                    try:
                        self.fd = _private_directory("/proc/self/fd/%d/runs" % parent_fd, False)
                    finally:
                        os.close(parent_fd)
                else:
                    self.fd = _private_directory("/proc/self/fd/%d/runs" % root_fd, create)
            finally:
                os.close(root_fd)
        else:
            self.fd = _private_directory(path, create)
        self.anchor = "/proc/self/fd/%d" % self.fd

    def close(self):
        os.close(self.fd)

    def files(self, temporaries=False):
        for name in os.listdir(self.anchor):
            if re.match(r"^[0-9a-f]{32}\.json$", name) or (
                    temporaries and re.match(r"^\.(record|request)-[0-9a-f]{32}$", name)):
                yield os.path.join(self.anchor, name)

    def sweep(self):
        import fcntl
        for path in self.files(temporaries=True):
            fd = None
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                    continue
                # Give a newly created temporary time to acquire its lock.
                # Age is only a grace period: the lock and inode checks below
                # are still required, including for arbitrarily old writers.
                if os.path.basename(path).startswith(".") and time.time() - info.st_mtime < 300:
                    continue
                _flock(fd, fcntl.LOCK_EX)
                if _same_file(fd, path):
                    os.unlink(path)
            except (IOError, OSError):
                pass  # active, symlink, special file, or concurrent removal
            finally:
                if fd is not None:
                    os.close(fd)


def _read_record(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_size > 16384):
        raise ValueError("Invalid private metadata file")
    return json.loads(os.read(fd, 16384).decode("utf-8"))


def _scope(work_dir):
    if work_dir and not os.path.isabs(work_dir):
        raise ValueError("Expected working directory must be absolute")
    return os.path.normpath(work_dir) if work_dir else ""


def _request_name(work_dir):
    return "fixed-port-" + hashlib.sha256(_scope(work_dir).encode("utf-8")).hexdigest() + ".json"


def configure_port(port=None, work_dir="", directory=None, root=None):
    """Atomically set/clear a persistent fixed-port request for this scope.

    Exact directory requests override the account-wide (empty scope) request.
    A request affects future daemon starts, never switches a live instance.
    """
    work_dir = _scope(work_dir)
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("Fixed port must be between 1 and 65535")
    registry = Registry(directory, root=root)
    target = registry.anchor + "/" + _request_name(work_dir)
    temporary = registry.anchor + "/.request-" + uuid.uuid4().hex
    try:
        if port is None:
            try:
                os.unlink(target)
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    raise
        else:
            import fcntl
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                _flock(stream.fileno(), fcntl.LOCK_EX)
                stream.write(json.dumps(dict(port=port, work_dir=work_dir)).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
                os.rename(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        registry.close()


def requested_port(registry, work_dir):
    for scope in (_scope(work_dir), ""):
        path = registry.anchor + "/" + _request_name(scope)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                continue
            raise
        try:
            data = _read_record(fd)
            if (not isinstance(data, dict) or data.get("work_dir") != scope
                    or type(data.get("port")) is not int or not 1 <= data["port"] <= 65535):
                raise ValueError("Invalid fixed-port request; refusing fallback to an ephemeral port")
            return data["port"]
        finally:
            os.close(fd)
    return 0


class Lease(object):
    def __init__(self, registry, name, fd, metadata):
        self.directory_fd = os.dup(registry.fd)
        self.path = "/proc/self/fd/%d/%s" % (self.directory_fd, name)
        self.fd, self.metadata = fd, metadata
        atexit.register(self.close)

    def close(self):
        if self.fd is None:
            return
        try:
            if _same_file(self.fd, self.path):
                os.unlink(self.path)
        except (IOError, OSError):
            pass
        finally:
            os.close(self.directory_fd)
            os.close(self.fd)
            self.fd = None


def bind_instance(sock, host, port=0, directory=None, pid=None, work_dir=None, backlog=8):
    """Bind once (zero means OS ephemeral), then publish a private live record."""
    import fcntl
    import pwd
    work_dir = work_dir or os.getcwd()
    registry = Registry(directory)
    fd = None
    published = False
    temporary = registry.anchor + "/.record-" + uuid.uuid4().hex
    path = temporary
    try:
        registry.sweep()
        fixed = port or requested_port(registry, work_dir)
        # Keep this socket open throughout. No probe, port scan, or filesystem
        # reservation participates in choosing the TCP port.
        sock.bind((host, fixed))
        sock.listen(backlog)
        actual_port = sock.getsockname()[1]
        metadata = dict(port=actual_port, pid=pid or os.getpid(), daemon_pid=os.getpid(),
                        uid=os.geteuid(), user=pwd.getpwuid(os.geteuid()).pw_name,
                        work_dir=work_dir, instance=uuid.uuid4().hex)
        fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        _flock(fd, fcntl.LOCK_EX)
        payload = json.dumps(metadata).encode("utf-8")
        if os.write(fd, payload) != len(payload):
            raise RuntimeError("Incomplete instance record")
        os.lseek(fd, 0, os.SEEK_SET)
        if _read_record(fd) != metadata:
            raise RuntimeError("Instance record failed readback")
        name = metadata["instance"] + ".json"
        path = registry.anchor + "/" + name
        os.rename(temporary, path)
        lease = Lease(registry, name, fd, metadata)
        published = True
        return lease
    finally:
        if fd is not None and not published:
            try:
                for candidate in (temporary, path):
                    try:
                        if _same_file(fd, candidate):
                            os.unlink(candidate)
                    except OSError:
                        pass
            finally:
                os.close(fd)
        registry.close()


def _discover(directory=None, root=None, legacy=False, read_only=False):
    """Sweep stale records first, then read only private, locked records."""
    import fcntl
    import pwd
    try:
        registry = Registry(directory, create=False, root=root, legacy=legacy)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return []
        raise
    rows = []
    try:
        if not read_only:
            registry.sweep()
        for path in registry.files():
            fd = None
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_mode & 0o077 or info.st_size > 16384):
                    continue
                try:
                    _flock(fd, fcntl.LOCK_EX)
                    # A daemon can die after sweep. Remove this newly stale
                    # record too, without ever returning it to the selector.
                    if not read_only and _same_file(fd, path):
                        os.unlink(path)
                    continue
                except IOError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                row = _read_record(fd)
                if (not isinstance(row, dict) or row.get("uid") != os.geteuid()
                        or row.get("user") != pwd.getpwuid(os.geteuid()).pw_name
                        or type(row.get("port")) is not int or not 1 <= row["port"] <= 65535
                        or type(row.get("pid")) is not int or row["pid"] <= 0
                        or not isinstance(row.get("work_dir"), type(u""))
                        or not row["work_dir"].startswith("/")
                        or row.get("instance") + ".json" != os.path.basename(path)
                        or not _same_file(fd, path)):
                    continue
                rows.append(row)
            except (IOError, OSError, ValueError, TypeError):
                pass
            finally:
                if fd is not None:
                    os.close(fd)
    finally:
        registry.close()
    return sorted(rows, key=lambda row: row["port"])


def discover(directory=None, root=None, read_only=False):
    rows = _discover(directory, root, read_only=read_only)
    # A live deployed daemon was verified using the prior nested layout.
    # Read both during explicit upgrades; never move a live record or lock.
    if directory is None:
        rows += _discover(root=root, legacy=True, read_only=read_only)
    return sorted(rows, key=lambda row: row["port"])


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch-root", default=None)
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--work-dir", default="")
    args = parser.parse_args()
    if args.configure:
        configure_port(args.port, args.work_dir, root=args.scratch_root)
    print(json.dumps(discover(root=args.scratch_root, read_only=args.read_only)))
