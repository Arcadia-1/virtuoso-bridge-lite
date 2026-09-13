#!/usr/bin/env python2.7
"""RAMIC Bridge Daemon - Virtuoso Skill Bridge Service (Python 2.7 Version)"""

import sys
import socket
import os
import json
import signal
import threading
import time
import errno
import hashlib
import hmac as _hmac
import binascii
import tempfile
import traceback

# ---------------------------------------------------------------------------
# Bridge token authentication (wire protocol v1, see ramic_bridge_daemon_3.py
# and virtuoso_bridge.daemon_auth).
#
# NOTE: this file is parsed by Python 2.7, whose default source encoding is
# ASCII -- keep every byte of this file ASCII-only (no em-dashes, no unicode
# punctuation) or the daemon dies with SyntaxError before it can serve.
#
# Requests need HMAC(token, canonical frame of the COMPLETE request: proto,
# nonce, timeout, skill); responses carry HMAC(token, frame(nonce, marker,
# body)); request nonces are single-use (server-side replay protection); the
# "op=hello" handshake returns signed capabilities without executing SKILL.
# An unusable token file is FATAL unless RB_ALLOW_UNAUTHENTICATED=1 is
# explicitly set.
# ---------------------------------------------------------------------------
TOKEN_PATH_ENV = "RB_TOKEN_PATH"
ALLOW_UNAUTH_ENV = "RB_ALLOW_UNAUTHENTICATED"
_PROTO = 1
_REQ_DOMAIN = "vb1-request"
_RESP_DOMAIN = "vb1-response"
_HELLO_DOMAIN = "vb1-hello"
_HEX_DIGITS = set("0123456789abcdefABCDEF")
_TRUTHY = ("1", "true", "yes", "on")


def _frame(parts):
    """Length-prefixed canonical byte frame (mirrors daemon_auth.py)."""
    out = []
    for part in parts:
        if isinstance(part, unicode):
            part = part.encode("utf-8")
        elif not isinstance(part, str):
            part = str(part)  # bytearray (e.g. response body) -> bytes
        out.append(str(len(part)) + ":")
        out.append(part)
    return "".join(out)


def _mac_hex(*parts):
    return _hmac.new(
        BRIDGE_TOKEN, _frame(parts), hashlib.sha256
    ).hexdigest()


try:
    _compare_digest = _hmac.compare_digest  # python2.7.7+
except AttributeError:  # python2.7.0 - 2.7.6: constant-time fallback
    def _compare_digest(a, b):
        if len(a) != len(b):
            return False
        diff = 0
        for x, y in zip(bytearray(a), bytearray(b)):
            diff |= x ^ y
        return diff == 0


def _is_hex_token(text, min_len, max_len):
    return (
        isinstance(text, basestring)
        and min_len <= len(text) <= max_len
        and all(c in _HEX_DIGITS for c in text)
    )


def _opted_out_of_auth():
    return os.environ.get(ALLOW_UNAUTH_ENV, "").strip().lower() in _TRUTHY


def _harden_perms(path, dir_mode=None, file_mode=None):
    try:
        if dir_mode is not None:
            os.chmod(path, dir_mode)
        elif file_mode is not None:
            os.chmod(path, file_mode)
    except (OSError, IOError):
        pass


def _token_file_path():
    override = os.environ.get(TOKEN_PATH_ENV, "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".virtuoso-bridge", "bridge_token")


def _load_or_create_token():
    """Read the token file, provisioning it atomically (0600, dir 0700).

    Unusable token files are FATAL unless RB_ALLOW_UNAUTHENTICATED=1 was
    explicitly set.
    """
    path = _token_file_path()
    try:
        with open(path, "r") as handle:
            token = handle.read().strip()
        if len(token) >= 32 and all(c in "0123456789abcdefABCDEF" for c in token):
            _harden_perms(os.path.dirname(path) or ".", dir_mode=0o700)
            _harden_perms(path, file_mode=0o600)
            return token.lower()
    except (OSError, IOError):
        pass
    token = binascii.hexlify(os.urandom(32))
    try:
        parent = os.path.dirname(path) or "."
        if not os.path.isdir(parent):
            try:
                os.makedirs(parent)
            except OSError as exc:
                if getattr(exc, "errno", None) != errno.EEXIST \
                        or not os.path.isdir(parent):
                    raise
        _harden_perms(parent, dir_mode=0o700)
        fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".bridge_token.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
        except (OSError, IOError, AttributeError):
            pass
        with os.fdopen(fd, "wb") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_path, path)  # create-if-absent: under a first-time
            created = True           # creation race the winner's complete
        except OSError as exc:       # file is always the one on disk
            if getattr(exc, "errno", None) == errno.EEXIST:
                created = False
            else:
                raise
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        _harden_perms(path, file_mode=0o600)
        # Adopt the on-disk token so all racers converge on one secret.
        try:
            with open(path, "r") as handle:
                disk = handle.read().strip()
            if len(disk) >= 32 and all(c in "0123456789abcdefABCDEF" for c in disk):
                return disk.lower()
        except (OSError, IOError):
            pass
        if not created:
            raise OSError("lost token creation race and winner's file unreadable")
    except (OSError, IOError) as exc:
        if _opted_out_of_auth():
            sys.stderr.write(
                "[RB-auth] WARNING: cannot read or create token file {0} "
                "({1}); daemon runs UNAUTHENTICATED (explicit opt-in)\n".format(path, exc)
            )
            return None
        sys.stderr.write(
            "[RB-auth] FATAL: cannot read or create token file {0} ({1}).\n"
            "[RB-auth] Refusing to serve unauthenticated SKILL; set {2}=1 to "
            "explicitly opt into insecure legacy mode.\n".format(path, exc, ALLOW_UNAUTH_ENV)
        )
        sys.exit(1)
    return token


BRIDGE_TOKEN = _load_or_create_token()

# Server-side replay protection: nonce -> expiry.  Requests are served
# serially (single accept loop), so a plain dict is safe.
_NONCE_MARK = {}
_NONCE_MARK_MAX = 4096


def _consume_nonce(nonce, ttl_seconds):
    """Mark *nonce* as used.  Returns "ok", "replay", or "full".

    Fail-closed at capacity: when the cache is full even after expiring old
    entries, NEW requests are rejected ("full") -- live entries are never
    dropped, so no replay window can be opened by memory pressure.
    """
    now = time.time()
    expiry = _NONCE_MARK.get(nonce)
    if expiry is not None and expiry > now:
        return "replay"
    if len(_NONCE_MARK) >= _NONCE_MARK_MAX:
        for key in [k for k, v in _NONCE_MARK.items() if v <= now]:
            del _NONCE_MARK[key]
        if len(_NONCE_MARK) >= _NONCE_MARK_MAX:
            return "full"
    _NONCE_MARK[nonce] = now + max(900.0, 2.0 * float(ttl_seconds or 0) + 60.0)
    return "ok"


def _auth_error(request_data, kind):
    """Return an error string for unauthenticated/invalid requests, else None.

    *kind* selects the MAC domain: "hello" (capability handshake) or "req"
    (regular SKILL execution).  The MAC covers the complete request for its
    kind, so no field can be tampered with in flight.
    """
    if not BRIDGE_TOKEN:
        return None
    nonce = request_data.get("nonce")
    mac = request_data.get("mac")
    if not nonce or not mac:
        return (
            "AuthError: bridge token required - this daemon rejects "
            "unauthenticated SKILL (client too old, or unauthorized)"
        )
    nonce = str(nonce)
    if not _is_hex_token(nonce, 16, 128):
        return "AuthError: invalid nonce"
    try:
        proto = int(request_data.get("proto") or 0)
    except (TypeError, ValueError):
        return "AuthError: invalid protocol field"
    if kind == "hello":
        expected = _mac_hex(_HELLO_DOMAIN, str(proto), nonce)
        ttl = 300.0
    else:
        try:
            timeout = float(request_data.get("timeout"))
        except (TypeError, ValueError):
            return "AuthError: invalid timeout field"
        skill = request_data.get("skill")
        if not isinstance(skill, basestring):
            return "AuthError: invalid skill field"
        expected = _mac_hex(
            _REQ_DOMAIN, str(proto), nonce, "%.6f" % timeout, skill
        )
        ttl = timeout
    if not _compare_digest(expected, str(mac).lower()):
        return (
            "AuthError: bridge token mismatch - the daemon on this port "
            "belongs to a different user (or the token was rotated); run "
            "`virtuoso-bridge restart` after RBStop()"
        )
    verdict = _consume_nonce(nonce, ttl)
    if verdict == "replay":
        return (
            "AuthError: replayed request nonce - rejected by server-side "
            "replay protection"
        )
    if verdict == "full":
        return (
            "AuthError: nonce cache at capacity - request rejected "
            "(fail-closed; retry shortly)"
        )
    if proto != _PROTO:
        return "AuthError: protocol version mismatch (daemon speaks v1)"
    return None


def _capabilities_body():
    """Side-effect-free handshake payload describing this daemon."""
    return json.dumps(
        {
            "proto": _PROTO,
            "auth": "on" if BRIDGE_TOKEN else "off",
            "daemon": "ramic-bridge",
            "virtuoso_pid": virtuoso_pid,
        }
    )


# Counters surfaced to the SKILL monitor via stderr [RB-stat] lines.
# Throttled to ~1 Hz so heavy traffic doesn't flood stderr.
_RB_START_T = time.time()
_RB_CALLS = 0
_RB_ERRORS = 0
_RB_LAST_STAT_T = 0.0


def _emit_stat(force=False):
    global _RB_LAST_STAT_T
    now = time.time()
    if not force and now - _RB_LAST_STAT_T < 1.0:
        return
    _RB_LAST_STAT_T = now
    try:
        sys.stderr.write(
            "[RB-stat] count={0} errors={1} uptime={2}\n".format(
                _RB_CALLS, _RB_ERRORS, int(now - _RB_START_T)
            )
        )
        sys.stderr.flush()
    except Exception:
        pass


try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None

_fcntl_fn = getattr(_fcntl, "fcntl", None)
_f_getfl = getattr(_fcntl, "F_GETFL", 3)
_f_setfl = getattr(_fcntl, "F_SETFL", 4)
_o_nonblock = int(getattr(os, "O_NONBLOCK", 0))


def _fcntl_or_die(*args):
    if _fcntl_fn is None:
        raise RuntimeError("fcntl is unavailable on this platform")
    return _fcntl_fn(*args)

# Python 2.7 compatibility: try to import psutil, fallback to manual PID detection
psutil = None
try:
    import psutil as _psutil
    psutil = _psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# Command line arguments for host and port
HOST = sys.argv[1]
PORT = int(sys.argv[2])

# Global timeout control flag
timeout_flag = False

# Get Virtuoso's PID - this is the process we need to send signals to.
# psutil -> /proc -> getppid: degrade gracefully instead of dying at import
# time on kernels without /proc.
def _resolve_virtuoso_pid():
    if PSUTIL_AVAILABLE and psutil is not None:
        try:
            parent_process = psutil.Process().parent()
            if parent_process is not None:
                grandparent_process = parent_process.parent()
                if grandparent_process is not None:
                    return grandparent_process.pid, "psutil"
        except Exception:
            pass
        return os.getppid(), "getppid"
    try:
        # Read current process info from /proc; parent PID is the 4th field.
        with open('/proc/self/stat', 'r') as f:
            parent_pid = int(f.read().split()[3])
        with open('/proc/{0}/stat'.format(parent_pid), 'r') as f2:
            return int(f2.read().split()[3]), "proc"
    except Exception:
        return os.getppid(), "getppid"


virtuoso_pid, _pid_source = _resolve_virtuoso_pid()
if _pid_source == "getppid":
    sys.stderr.write(
        "[RB-pid] WARNING: no /proc and no psutil; watchdog falls back to "
        "the direct parent PID {0} (timeout interrupts may be imprecise)\n".format(
            virtuoso_pid
        )
    )
    sys.stderr.flush()

# Python 2.7 compatibility: print statement instead of print() function
# print("Virtuoso PID: {0}".format(virtuoso_pid))

# Set stdin to non-blocking mode for reading Virtuoso responses
# Note: Only stdin needs to be non-blocking, stdout should remain blocking
stdin_fd = sys.stdin.fileno()
stdin_fl = _fcntl_or_die(stdin_fd, _f_getfl)
_fcntl_or_die(stdin_fd, _f_setfl, stdin_fl | _o_nonblock)

# Keep stdout blocking for reliable writes
stdout_fd = sys.stdout.fileno()
stdout_fl = _fcntl_or_die(stdout_fd, _f_getfl)
_fcntl_or_die(stdout_fd, _f_setfl, stdout_fl & ~_o_nonblock)  # Ensure blocking

# Global watchdog timer reference
watchdog_timer = None


def _safe_sendall(conn, data):
    try:
        conn.sendall(data)
    except socket.error:
        pass


def _safe_close_connection(conn):
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except socket.error:
        pass
    try:
        conn.close()
    except socket.error:
        pass

def watchdog_callback():
    """Watchdog callback function that sends SIGINT signal to Virtuoso process when timeout occurs."""
    global timeout_flag
    if not timeout_flag:  # If not set yet, it means timeout occurred
        timeout_flag = True
        try:
            os.kill(virtuoso_pid, signal.SIGINT)
        except Exception:
            pass

def read_until_delimiter(start_ok=b'\x02', start_err=b'\x15', end=b'\x1e'):
    """Read data from Virtuoso's stdout until specific delimiters are found."""
    result = bytearray()

    # Wait for start marker
    while True:
        try:
            ch = sys.stdin.read(1)
            if ch in [start_ok, start_err]:
                break
            if not ch:
                # EOF: Virtuoso (or its pipe) is gone -- fail fast instead
                # of waiting for the watchdog.
                return "\x15TimeoutError"
        except IOError as e:
            if e.errno == errno.EAGAIN or e.errno == errno.EWOULDBLOCK:
                # No data available, check timeout and continue
                if timeout_flag:
                    return "\x15TimeoutError"
                time.sleep(0.001)  # Short sleep to avoid busy waiting
                continue
            else:
                raise
        if timeout_flag:
            # Python 2.7 compatibility: return string directly
            return "\x15TimeoutError"

    # Python 2.7 compatibility: convert string to bytes for bytearray
    if isinstance(ch, str):
        result.extend(ch.encode('latin1'))
    else:
        result.extend(ch)

    # Read content until end marker
    while True:
        try:
            ch = sys.stdin.read(1)
            if timeout_flag:
                # Python 2.7 compatibility: return string directly
                return "\x15TimeoutError"
            if not ch:  # EOF: Virtuoso died mid-response
                return "\x15TimeoutError"
            if ch == end:
                break
            # Python 2.7 compatibility: convert string to bytes for bytearray
            if isinstance(ch, str):
                result.extend(ch.encode('latin1'))
            else:
                result.extend(ch)
        except IOError as e:
            if e.errno == errno.EAGAIN or e.errno == errno.EWOULDBLOCK:
                # No data available, check timeout and continue
                if timeout_flag:
                    return "\x15TimeoutError"
                time.sleep(0.001)  # Short sleep to avoid busy waiting
                continue
            else:
                raise

    return result

def handle_external_connection(conn, addr):
    """Handle incoming TCP connections from Python clients."""
    global watchdog_timer, timeout_flag

    try:
        # Receive JSON formatted request data
        chunks = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        # Python 2.7 compatibility: data is already bytes/string
        request_data = json.loads(data)
        request_nonce = request_data.get("nonce")

        # Capability handshake: describe this daemon WITHOUT executing
        # anything (no "skill" field in the request, so even a pre-token
        # daemon cannot act on it).
        if request_data.get("op") == "hello":
            auth_error = _auth_error(request_data, "hello")
            if BRIDGE_TOKEN:
                if auth_error:
                    _safe_sendall(conn, "\x15" + auth_error)
                    return
                body = _capabilities_body()
                if isinstance(body, unicode):
                    body = body.encode("utf-8")
                resp_mac = _mac_hex(_RESP_DOMAIN, str(request_nonce), "\x02", body)
                _safe_sendall(conn, "\x02" + resp_mac + body)
            else:
                body = _capabilities_body()
                if isinstance(body, unicode):
                    body = body.encode("utf-8")
                _safe_sendall(conn, "\x02" + body)
            return

        auth_error = _auth_error(request_data, "req")
        if auth_error:
            # Unauthenticated or foreign client: refuse without executing.
            if isinstance(auth_error, unicode):
                auth_error = auth_error.encode("utf-8")
            _safe_sendall(conn, "\x15" + auth_error)
            return

        skill_code = request_data["skill"]
        timeout_seconds = request_data["timeout"]

        # Reset timeout flag
        timeout_flag = False

        # Python 2.7: json.loads yields unicode; normalize SKILL to utf-8
        # bytes once so every later use (temp file, stdout) is byte-exact.
        if isinstance(skill_code, unicode):
            skill_code = skill_code.encode("utf-8")

        # Clear stdin buffer before writing (non-blocking read until empty)

        while True:
            try:
                ch = sys.stdin.read(1)
                if not ch:  # No more data
                    break
            except IOError as e:
                if e.errno == errno.EAGAIN or e.errno == errno.EWOULDBLOCK:
                    break  # No data available
                else:
                    break  # Other error, stop clearing

        # Multi-line SKILL: write to temp file and load() it.
        # This preserves comments (;) which would break single-line flattening.
        # We wrap the code so the return value is captured in a global variable,
        # because load() itself only returns t, not the last expression's value.
        # The file is written in binary mode so arbitrary utf-8 SKILL cannot
        # trip the implicit ascii codec of Python 2 text streams.
        tmp_il_path = None
        if b"\n" in skill_code:
            fd, tmp_il_path = tempfile.mkstemp(suffix=".il", prefix="vb_eval_")
            with os.fdopen(fd, "wb") as f:
                f.write(b"_vb_eval_result = progn(\n" + skill_code + b"\n)\n")
            escaped_path = tmp_il_path.replace("\\", "/")
            send_code = 'load("%s") hiFlush() _vb_eval_result\n' % escaped_path
        else:
            send_code = b'let(((__vb_r ' + skill_code + b')) hiFlush() __vb_r)\n'

        sys.stdout.write(send_code)
        sys.stdout.flush()

        # Start watchdog timer
        watchdog_timer = threading.Timer(timeout_seconds, watchdog_callback)
        watchdog_timer.daemon = True
        watchdog_timer.start()

        # Wait for Virtuoso response
        returnData = read_until_delimiter()

        # If normal return, set timeout flag to True to stop watchdog
        if not timeout_flag:
            timeout_flag = True

        # Cancel watchdog timer
        watchdog_timer.cancel()

        # Python 2.7 compatibility: handle returnData properly
        if isinstance(returnData, bytearray):
            returnData = str(returnData)
        elif hasattr(returnData, 'encode'):  # Check if it's unicode
            returnData = returnData.encode('utf-8')

        # Authenticate the response so the client can detect a squatted
        # port: the MAC covers the status marker AND the full body.
        if BRIDGE_TOKEN and request_nonce:
            resp_mac = _mac_hex(
                _RESP_DOMAIN, str(request_nonce), returnData[:1], returnData[1:]
            )
            _safe_sendall(conn, returnData[:1] + resp_mac + returnData[1:])
        else:
            _safe_sendall(conn, returnData)

        # Stats: count this call and tag as error if SKILL sent NAK
        # (0x15) or the response is empty/malformed.  Throttled emit
        # below pushes the totals to SKILL via stderr.
        global _RB_CALLS, _RB_ERRORS
        _RB_CALLS += 1
        _first = returnData[:1] if returnData else b""
        if isinstance(_first, str):
            _is_ok = (_first == "\x02")
        else:
            _is_ok = (_first == b"\x02")
        if not _is_ok:
            _RB_ERRORS += 1
        _emit_stat()

        # Clean up temp file if we used one
        if tmp_il_path:
            try:
                os.unlink(tmp_il_path)
            except OSError:
                pass

    except ValueError as e:
        # Python 2.7 compatibility: handle JSON decode errors
        error_msg = "\x15JSONDecodeError: {0}".format(str(e))
        if hasattr(error_msg, 'encode'):  # Check if it's unicode
            error_msg = error_msg.encode('utf-8')
        _safe_sendall(conn, error_msg)
    except Exception as e:
        # Python 2.7 compatibility: except Exception, e syntax
        traceback.print_exc()
        error_msg = "\x15{0}".format(str(e))
        if hasattr(error_msg, 'encode'):  # Check if it's unicode
            error_msg = error_msg.encode('utf-8')
        _safe_sendall(conn, error_msg)
    finally:
        # Ensure watchdog timer is cleaned up
        timeout_flag = True
        if watchdog_timer:
            watchdog_timer.cancel()
        _safe_close_connection(conn)

def start_server():
    """Start the TCP server to accept client connections."""
    # Python 2.7 compatibility: don't use context manager for socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Socket options for address reuse
        # Only use SO_REUSEADDR to allow quick restart after crash
        # Remove SO_REUSEPORT to prevent multiple daemons on same port
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Try to bind with error handling for port conflicts
        try:
            s.bind((HOST, PORT))
        except socket.error as e:
            if e.errno == errno.EADDRINUSE:
                sys.stderr.write("ERROR: Port {0} is already in use. Another daemon may be running.\n".format(PORT))
                sys.exit(1)
            else:
                raise

        s.listen(1)
        # Banner -- SKILL side parses this from stderr to populate
        # RBLastPid / RBLastBind / RBLastHost / RBLastIP for the monitor
        # display.  Format is frozen:
        #   "[RB-banner] pid=N bind=H:P host=NAME ip=A.B.C.D"
        try:
            _hn = socket.gethostname() or "unknown"
        except Exception:
            _hn = "unknown"
        _ip = ""
        try:
            _probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                _probe.connect(("8.8.8.8", 80))
                _ip = _probe.getsockname()[0]
            finally:
                _probe.close()
        except Exception:
            try:
                _ip = socket.gethostbyname(socket.gethostname())
            except Exception:
                _ip = ""
        sys.stderr.write(
            "[RB-banner] pid={0} bind={1}:{2} host={3} ip={4} auth={5}\n".format(
                os.getpid(), HOST, PORT, _hn, (_ip or "unknown"),
                ("on" if BRIDGE_TOKEN else "off"),
            )
        )
        sys.stderr.flush()
        while True:
            conn, addr = s.accept()
            try:
                handle_external_connection(conn, addr)
            except Exception:
                traceback.print_exc()
                _safe_close_connection(conn)
    finally:
        s.close()

# Start the server
if __name__ == "__main__":
    start_server()
