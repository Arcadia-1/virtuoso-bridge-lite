"""Owner lifetime checks shared by the Python 2.7 and Python 3 daemons.

A PID is meaningful only on its owner's host. The SKILL launcher reads the
kernel boot ID in Virtuoso, before Cadence IPC can dispatch to another host.
Unknown/different boots retain IPC cleanup without local polling or signals.
"""

import errno
import os
import signal
import sys
import threading
import time


BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def _boot_id():
    try:
        with open(BOOT_ID_PATH, "r") as handle:
            return handle.read().strip()
    except (IOError, OSError):
        return None


def _identity(pid):
    try:
        with open("/proc/{0}/stat".format(pid), "r") as handle:
            # comm can contain spaces and parentheses: split after it.
            fields = handle.read().rsplit(")", 1)[1].split()
    except (IOError, OSError) as exc:
        if exc.errno in (errno.ENOENT, errno.ESRCH):
            return None
        raise
    return None if fields[0] in ("Z", "X") else fields[19]


class OwnerProcess(object):
    def __init__(self, pid, boot_id):
        self.pid = pid
        self.local = bool(boot_id and boot_id == _boot_id())
        self.identity = _identity(pid) if self.local else None

    def alive(self):
        if not self.local or self.identity is None:
            return False
        try:
            return _identity(self.pid) == self.identity
        except (IOError, OSError):
            return False

    def interrupt(self):
        # Never signal a foreign, unknown, dead or recycled owner PID.
        if self.alive():
            os.kill(self.pid, signal.SIGINT)

    def start_monitor(self):
        if not self.local:
            sys.stderr.write(
                "[RB-owner] owner boot differs or is unknown; local owner "
                "monitoring and signals disabled; relying on IPC/exit hook cleanup\n"
            )
            sys.stderr.flush()
            return
        if not self.alive():
            sys.exit(0)

        def watch():
            while True:
                time.sleep(0.25)
                if not self.alive():
                    # sys.exit only stops this thread. The kernel must close
                    # sockets even while accept/recv/SKILL I/O is blocked.
                    os._exit(0)

        monitor = threading.Thread(target=watch, name="virtuoso-owner")
        monitor.daemon = True
        monitor.start()
