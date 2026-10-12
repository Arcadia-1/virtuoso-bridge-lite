"""Bounded, in-memory recovery for RAMIC daemon requests.

This module is deliberately ASCII-only and Python 2.7 compatible because it
is deployed beside both daemon variants.  It owns the single Virtuoso IPC
lane; callers provide the version-specific SKILL write/read hook.
"""

import binascii
import errno
import hashlib
import os
import sys
import threading
import time


try:
    text_type = unicode
except NameError:
    text_type = str


DEFAULT_TTL_SECONDS = 3600.0
DEFAULT_MAX_FINISHED = 4096
DEFAULT_MAX_FRAME_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_CACHE_BYTES = 32 * 1024 * 1024
SUBMIT_MAX_PAST_MS = 300000
SUBMIT_MAX_FUTURE_MS = 30000
# Longest a submit reply waits for its request to finish (client asks wait_ms).
MAX_SUBMIT_WAIT_SECONDS = 2.0
_LOWER_HEX = set("0123456789abcdef")


class RequestNotStarted(Exception):
    """The SKILL frame was definitely not transmitted."""


class ExecutionUncertain(Exception):
    """Execution or response attribution is uncertain; poison the IPC lane."""


def random_instance_id():
    value = binascii.hexlify(os.urandom(16))
    if not isinstance(value, str):
        value = value.decode("ascii")
    return value


def valid_request_id(value):
    return (
        isinstance(value, (str, text_type))
        and len(value) == 32
        and all(ch in _LOWER_HEX for ch in value)
    )


def _utf8(value):
    if isinstance(value, text_type):
        return value.encode("utf-8")
    if isinstance(value, str):
        return value
    raise TypeError("value must be text")


def _as_bytes(value):
    if isinstance(value, bytearray):
        if sys.version_info[0] >= 3:
            return bytes(value)
        return str(value)
    if isinstance(value, text_type):
        return value.encode("latin1")
    return value


def read_response_frame(read_one, max_frame_bytes=DEFAULT_MAX_FRAME_BYTES):
    """Read one STX/NAK ... RS response without discarding any pipe bytes.

    ``read_one`` returns one byte, ``None`` for EAGAIN, or an empty value for
    EOF.  Once a request may have reached Virtuoso, every malformed condition
    is attribution-unsafe and therefore raises ``ExecutionUncertain``.
    """
    result = bytearray()
    started = False
    while True:
        try:
            chunk = read_one()
        except IOError as exc:
            if getattr(exc, "errno", None) in (errno.EAGAIN, errno.EWOULDBLOCK):
                chunk = None
            else:
                raise ExecutionUncertain("Virtuoso response read failed: %s" % exc)
        if chunk is None:
            time.sleep(0.001)
            continue
        chunk = _as_bytes(chunk)
        if not chunk:
            raise ExecutionUncertain("Virtuoso response pipe closed before a full frame")
        if len(chunk) != 1:
            raise ExecutionUncertain("Virtuoso response reader returned a non-byte chunk")
        code = ord(chunk) if not isinstance(chunk[0], int) else chunk[0]
        if not started:
            if code not in (0x02, 0x15):
                raise ExecutionUncertain(
                    "Malformed Virtuoso response before the status marker"
                )
            result.extend(chunk)
            started = True
            continue
        if code == 0x1e:
            return _as_bytes(result)
        result.extend(chunk)
        if len(result) > max_frame_bytes:
            raise ExecutionUncertain("Virtuoso response frame exceeds the size limit")


def resynchronize(send, read_one):
    """Send a probe and discard the IPC stream up to the probe's own reply.

    Virtuoso evaluates requests in order, so once the reply to a fresh probe
    has been read, every earlier byte (the late reply of a timed-out request,
    the rest of a half-read frame) has been consumed and the next request's
    reply is attributable again. ``send`` writes one SKILL expression;
    ``read_one`` behaves as for ``read_response_frame``. Returns False if the
    pipe closes first. Waits as long as Virtuoso does not answer: the lane
    stays poisoned meanwhile.
    """
    probe = '"vb-resync-%s"' % random_instance_id()
    marker = _as_bytes(probe) + b"\x1e"
    send(probe)
    window = b""
    while True:
        try:
            chunk = read_one()
        except IOError as exc:
            if getattr(exc, "errno", None) not in (errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            chunk = None
        if chunk is None:
            time.sleep(0.001)
            continue
        chunk = _as_bytes(chunk)
        if not chunk:
            return False
        window = (window + chunk)[-len(marker):]
        if window == marker:
            return True


class RecoverableRequestManager(object):
    """Serialize legacy/recoverable SKILL and retain bounded receipts."""

    def __init__(
        self,
        instance_id=None,
        ttl_seconds=DEFAULT_TTL_SECONDS,
        max_finished=DEFAULT_MAX_FINISHED,
        max_frame_bytes=DEFAULT_MAX_FRAME_BYTES,
        max_cache_bytes=DEFAULT_MAX_CACHE_BYTES,
        clock=None,
        thread_factory=None,
    ):
        if max_finished < 1 or max_frame_bytes < 1 or max_cache_bytes < max_frame_bytes:
            raise ValueError("invalid recovery cache limits")
        self.instance_id = instance_id or random_instance_id()
        self.ttl_seconds = float(ttl_seconds)
        self.max_finished = int(max_finished)
        self.max_frame_bytes = int(max_frame_bytes)
        self.max_cache_bytes = int(max_cache_bytes)
        self._clock = clock or time.time
        self._thread_factory = thread_factory or threading.Thread
        self._lock = threading.RLock()
        # Notified whenever a running request finishes (see wait()).
        self._finished = threading.Condition(self._lock)
        self._entries = {}
        self._cached_bytes = 0
        self._lane_owner = None
        self._lane_since = None
        self._poisoned = None
        self._poisoned_at = None
        self._resync = None
        self._resyncing = False

    @property
    def poisoned(self):
        with self._lock:
            return self._poisoned

    def set_resync(self, resync):
        """Register ``resync()``, run on a worker thread whenever the lane is
        poisoned; a true result clears the poison."""
        self._resync = resync

    def _poison_locked(self, diagnostic):
        if self._poisoned:
            return
        self._poisoned = diagnostic
        self._poisoned_at = self._clock()
        if self._resync is None:
            return
        try:
            worker = self._thread_factory(
                target=self._run_resync, args=(diagnostic,), name="ramic-lane-resync",
            )
            worker.daemon = True
            worker.start()
            self._resyncing = True
        except Exception:
            self._resyncing = False

    def _run_resync(self, diagnostic):
        try:
            synced = bool(self._resync())
        except Exception:
            synced = False
        with self._lock:
            self._resyncing = False
            if synced and self._poisoned == diagnostic:
                self._poisoned = None
                self._poisoned_at = None

    def _poisoned_message_locked(self):
        message = "IPC lane is poisoned: %s" % self._poisoned
        if self._resyncing:
            message += "; waiting for Virtuoso to answer a resync probe"
        return message

    def _take_lane_locked(self, owner):
        self._lane_owner = owner
        self._lane_since = self._clock()

    def lane_state(self):
        """JSON-ready lane state for the capability handshake."""
        with self._lock:
            if self._poisoned:
                return {
                    "state": "resyncing" if self._resyncing else "poisoned",
                    "detail": self._poisoned,
                    "since_ms": int(self._poisoned_at * 1000.0),
                }
            if self._lane_owner is not None:
                return {
                    "state": "busy",
                    "detail": "legacy" if self._lane_owner == "legacy" else "recoverable",
                    "since_ms": int(self._lane_since * 1000.0),
                }
            return {"state": "idle"}

    def _fingerprint(self, skill):
        return hashlib.sha256(_utf8(skill)).hexdigest()

    def _cleanup_locked(self):
        now = self._clock()
        expired = []
        for request_id, entry in self._entries.items():
            finished_at = entry.get("finished_at")
            if finished_at is not None and now - finished_at >= self.ttl_seconds:
                expired.append(request_id)
        for request_id in expired:
            entry = self._entries.pop(request_id)
            self._cached_bytes -= entry.get("response_bytes", 0)

    def _snapshot_locked(self, entry):
        result = {
            "request_id": entry["request_id"],
            "daemon_instance": self.instance_id,
            "state": entry["state"],
        }
        if entry.get("response") is not None:
            result["response"] = entry["response"]
        if entry.get("diagnostic"):
            result["diagnostic"] = entry["diagnostic"]
        return result

    def _ephemeral(self, request_id, state, diagnostic):
        return {
            "request_id": request_id,
            "daemon_instance": self.instance_id,
            "state": state,
            "diagnostic": diagnostic,
        }

    def _finished_count_locked(self):
        return sum(1 for entry in self._entries.values() if entry["state"] != "running")

    def _remember_terminal_locked(self, request_id, fingerprint, state, diagnostic):
        # A running entry will eventually become finished, so reserve its slot
        # while recording busy/rejected tombstones around it.
        if len(self._entries) >= self.max_finished:
            return self._ephemeral(request_id, "rejected", "receipt cache at capacity")
        entry = {
            "request_id": request_id,
            "fingerprint": fingerprint,
            "state": state,
            "response": None,
            "response_bytes": 0,
            "diagnostic": diagnostic,
            "finished_at": self._clock(),
        }
        self._entries[request_id] = entry
        return self._snapshot_locked(entry)

    def try_begin_legacy(self):
        with self._lock:
            if self._poisoned:
                return False, self._poisoned_message_locked()
            if self._lane_owner is not None:
                return False, "IPC lane is busy"
            self._take_lane_locked("legacy")
            return True, None

    def finish_legacy(self, poison_diagnostic=None):
        with self._lock:
            if poison_diagnostic:
                self._poison_locked(poison_diagnostic)
            if self._lane_owner == "legacy":
                self._lane_owner = None

    def submit(self, request_id, skill, execute):
        fingerprint = self._fingerprint(skill)
        with self._lock:
            self._cleanup_locked()
            existing = self._entries.get(request_id)
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    return self._ephemeral(
                        request_id,
                        "rejected",
                        "request_id was already used with different SKILL",
                    )
                return self._snapshot_locked(existing)
            now_ms = int(self._clock() * 1000.0)
            request_ms = int(request_id[:12], 16)
            if (
                request_ms < now_ms - SUBMIT_MAX_PAST_MS
                or request_ms > now_ms + SUBMIT_MAX_FUTURE_MS
            ):
                return self._remember_terminal_locked(
                    request_id,
                    fingerprint,
                    "rejected",
                    "new request_id timestamp is outside the -300s/+30s submit window",
                )
            if self._poisoned:
                return self._remember_terminal_locked(
                    request_id, fingerprint, "rejected", self._poisoned_message_locked(),
                )
            if self._lane_owner is not None:
                return self._remember_terminal_locked(
                    request_id, fingerprint, "busy", "IPC lane is busy"
                )
            if self._finished_count_locked() >= self.max_finished:
                return self._ephemeral(request_id, "rejected", "receipt cache at capacity")
            if self._cached_bytes + self.max_frame_bytes > self.max_cache_bytes:
                return self._remember_terminal_locked(
                    request_id, fingerprint, "rejected", "response cache at capacity"
                )
            entry = {
                "request_id": request_id,
                "fingerprint": fingerprint,
                "state": "running",
                "response": None,
                "response_bytes": 0,
                "diagnostic": None,
                "finished_at": None,
            }
            self._entries[request_id] = entry
            self._take_lane_locked(request_id)
            try:
                worker = self._thread_factory(
                    target=self._run_request,
                    args=(request_id, skill, execute),
                    name="ramic-recoverable-request",
                )
                worker.daemon = True
                worker.start()
            except Exception as exc:
                self._lane_owner = None
                entry["state"] = "rejected"
                entry["diagnostic"] = "worker could not start: %s" % exc
                entry["finished_at"] = self._clock()
            return self._snapshot_locked(entry)

    def _run_request(self, request_id, skill, execute):
        state = "completed"
        response = None
        diagnostic = None
        poison = None
        try:
            response = execute(skill)
            if not isinstance(response, (str, text_type)):
                raise ExecutionUncertain("execution hook returned a non-text response")
            if response[:1] not in ("\x02", "\x15"):
                raise ExecutionUncertain("execution hook returned no valid response marker")
            response_size = len(_utf8(response))
            if response_size > self.max_frame_bytes:
                raise ExecutionUncertain("Virtuoso response frame exceeds the size limit")
        except RequestNotStarted as exc:
            state = "rejected"
            response = None
            diagnostic = str(exc) or "SKILL was not transmitted"
            response_size = 0
        except ExecutionUncertain as exc:
            state = "unknown"
            response = None
            diagnostic = str(exc) or "execution outcome is unknown"
            poison = diagnostic
            response_size = 0
        except Exception as exc:
            state = "unknown"
            response = None
            diagnostic = "unexpected execution failure: %s" % exc
            poison = diagnostic
            response_size = 0
        with self._lock:
            entry = self._entries[request_id]
            if state == "completed" and self._cached_bytes + response_size > self.max_cache_bytes:
                state = "unknown"
                diagnostic = "response cache capacity was exceeded after execution"
                poison = diagnostic
                response = None
                response_size = 0
            entry["state"] = state
            entry["response"] = response
            entry["response_bytes"] = response_size
            entry["diagnostic"] = diagnostic
            entry["finished_at"] = self._clock()
            self._cached_bytes += response_size
            if poison:
                self._poison_locked(poison)
            if self._lane_owner == request_id:
                self._lane_owner = None
            self._finished.notify_all()

    def wait(self, request_id, seconds):
        """Wait up to *seconds* (capped) for a running request to finish.

        Lets a submit reply carry the result of a quick request instead of
        "running", which saves the client a receipt round trip. Returns the
        receipt either way; a request still running stays queryable.
        """
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = 0.0
        seconds = max(0.0, min(seconds, MAX_SUBMIT_WAIT_SECONDS))
        deadline = time.time() + seconds
        with self._lock:
            while True:
                entry = self._entries.get(request_id)
                if entry is None or entry["state"] != "running":
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._finished.wait(remaining)
        return self.receipt(request_id)

    def receipt(self, request_id):
        with self._lock:
            self._cleanup_locked()
            entry = self._entries.get(request_id)
            if entry is None:
                return self._ephemeral(
                    request_id, "unknown", "request_id is not retained by this daemon"
                )
            if entry["state"] in ("busy", "rejected"):
                return self._ephemeral(
                    request_id,
                    "unknown",
                    "request was not started: %s"
                    % (entry.get("diagnostic") or entry["state"]),
                )
            return self._snapshot_locked(entry)
