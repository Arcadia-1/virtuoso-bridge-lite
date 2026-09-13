#!/usr/bin/env python3
"""RAMIC Bridge Daemon - Virtuoso Skill Bridge Service (Python 3 Version)"""

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
# Bridge token authentication (wire protocol v1).
#
# The daemon port is host-global: any local user can end up bound to it (or
# deliberately squat it).  A token shared with legitimate clients via an
# atomic 0600 file (~/.virtuoso-bridge/bridge_token, or RB_TOKEN_PATH) gates
# every request:
#   - Requests need HMAC(token, canonical frame of the COMPLETE request:
#     proto, nonce, timeout, skill) -- no field can be swapped in flight.
#   - Responses carry HMAC(token, frame(nonce, marker, body)) so clients can
#     detect a squatter even on error replies.
#   - Server-side replay protection: request nonces are remembered for a TTL
#     exceeding any request timeout; duplicates are rejected.
#   - "hello" handshake (op=hello, no skill field) returns signed capabilities
#     without touching Virtuoso, so clients discover protocol/auth mismatch
#     before any SKILL executes.
# The token never crosses the TCP wire.  Running without a token requires the
# explicit RB_ALLOW_UNAUTHENTICATED=1 opt-in; by default an unusable token
# file is fatal.
# ---------------------------------------------------------------------------
TOKEN_PATH_ENV = "RB_TOKEN_PATH"
ALLOW_UNAUTH_ENV = "RB_ALLOW_UNAUTHENTICATED"
_PROTO = 1
_REQ_DOMAIN = "vb1-request"
_RESP_DOMAIN = "vb1-response"
_HELLO_DOMAIN = "vb1-hello"
_HEX_DIGITS = set("0123456789abcdefABCDEF")
_TRUTHY = ("1", "true", "yes", "on")


def _frame(*parts):
    """Length-prefixed canonical byte frame (mirrors daemon_auth.py)."""
    out = bytearray()
    for part in parts:
        if isinstance(part, str):
            part = part.encode("utf-8")
        out += str(len(part)).encode("ascii") + b":"
        out += part
    return bytes(out)


def _mac_hex(*parts):
    return _hmac.new(
        BRIDGE_TOKEN.encode("utf-8"), _frame(*parts), hashlib.sha256
    ).hexdigest()


def _is_hex_token(text, min_len, max_len):
    return (
        isinstance(text, str)
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
    except OSError:
        pass


def _token_file_path():
    override = os.environ.get(TOKEN_PATH_ENV, "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".virtuoso-bridge", "bridge_token")


def _load_or_create_token():
    """Read the token file, provisioning it atomically (0600, dir 0700).

    Unusable token files are FATAL unless RB_ALLOW_UNAUTHENTICATED=1 was
    explicitly set: an auth-less daemon silently accepting anyone else's
    SKILL is exactly the incident this daemon exists to prevent.
    """
    path = _token_file_path()
    try:
        with open(path, "r") as handle:
            token = handle.read().strip()
        if len(token) >= 32 and all(c in "0123456789abcdefABCDEF" for c in token):
            _harden_perms(os.path.dirname(path) or ".", dir_mode=0o700)
            _harden_perms(path, file_mode=0o600)
            return token.lower()
    except OSError:
        pass
    token = binascii.hexlify(os.urandom(32)).decode("ascii")
    try:
        parent = os.path.dirname(path) or "."
        if not os.path.isdir(parent):
            try:
                os.makedirs(parent)
            except OSError as exc:
                if exc.errno != errno.EEXIST or not os.path.isdir(parent):
                    raise
        _harden_perms(parent, dir_mode=0o700)
        fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".bridge_token.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
        except (OSError, AttributeError):
            pass
        with os.fdopen(fd, "w") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_path, path)  # create-if-absent: under a first-time
            created = True           # creation race the winner's complete
        except OSError as exc:       # file is always the one on disk
            if exc.errno == errno.EEXIST:
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
        except OSError:
            pass
        if not created:
            raise OSError("lost token creation race and winner's file unreadable")
    except OSError as exc:
        if _opted_out_of_auth():
            sys.stderr.write(
                "[RB-auth] WARNING: cannot read or create token file %s (%s); "
                "daemon runs UNAUTHENTICATED (explicit opt-in)\n" % (path, exc)
            )
            return None
        sys.stderr.write(
            "[RB-auth] FATAL: cannot read or create token file %s (%s).\n"
            "[RB-auth] Refusing to serve unauthenticated SKILL; set %s=1 to "
            "explicitly opt into insecure legacy mode.\n"
            % (path, exc, ALLOW_UNAUTH_ENV)
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
        if not isinstance(skill, str):
            return "AuthError: invalid skill field"
        expected = _mac_hex(
            _REQ_DOMAIN, str(proto), nonce, "%.6f" % timeout, skill
        )
        ttl = timeout
    if not _hmac.compare_digest(expected, str(mac).lower()):
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
            "[RB-stat] count={c} errors={e} uptime={u}\n".format(
                c=_RB_CALLS, e=_RB_ERRORS, u=int(now - _RB_START_T),
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

HOST = sys.argv[1]
PORT = int(sys.argv[2])

timeout_flag = False

# Get Virtuoso's PID (grandparent: virtuoso -> sh -> this daemon).
# /proc is Linux-only and psutil is optional, so degrade gracefully: the
# watchdog only needs a best-effort target, and the daemon must still boot
# on kernels without /proc (e.g. macOS) rather than die at import time.
def _resolve_virtuoso_pid():
    try:
        import psutil

        parent = psutil.Process().parent()
        if parent is not None:
            grandparent = parent.parent()
            if grandparent is not None:
                return grandparent.pid, "psutil"
    except Exception:
        pass
    try:
        with open("/proc/self/stat", "r") as f:
            parent_pid = int(f.read().split()[3])
        with open(f"/proc/{parent_pid}/stat", "r") as f:
            return int(f.read().split()[3]), "proc"
    except Exception:
        pass
    return os.getppid(), "getppid"


virtuoso_pid, _pid_source = _resolve_virtuoso_pid()
if _pid_source == "getppid":
    sys.stderr.write(
        "[RB-pid] WARNING: no /proc and no psutil; watchdog falls back to "
        "the direct parent PID %d (timeout interrupts may be imprecise)\n"
        % virtuoso_pid
    )
    sys.stderr.flush()

# Set stdin to non-blocking, keep stdout blocking.
stdin_fd = sys.stdin.fileno()
stdin_fl = _fcntl_or_die(stdin_fd, _f_getfl)
_fcntl_or_die(stdin_fd, _f_setfl, stdin_fl | _o_nonblock)

stdout_fd = sys.stdout.fileno()
stdout_fl = _fcntl_or_die(stdout_fd, _f_getfl)
_fcntl_or_die(stdout_fd, _f_setfl, stdout_fl & ~_o_nonblock)

watchdog_timer = None


def _safe_sendall(conn, data):
    try:
        conn.sendall(data)
    except OSError:
        pass


def _safe_close_connection(conn):
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass

def watchdog_callback():
    global timeout_flag
    if not timeout_flag:
        timeout_flag = True
        try:
            os.kill(virtuoso_pid, signal.SIGINT)
        except Exception:
            pass

def read_until_delimiter(start_ok=0x02, start_err=0x15, end=0x1e):
    """Read data from Virtuoso's stdout until specific delimiters are found."""
    result = bytearray()

    # Wait for start marker
    while True:
        try:
            ch = sys.stdin.buffer.read(1)
            if not ch:
                # EOF: Virtuoso (or its pipe) is gone -- no result can ever
                # arrive, so fail fast instead of waiting for the watchdog
                # (whose SIGINT target may be poorly resolved without /proc).
                return b"\x15TimeoutError"
            if ch[0] in (start_ok, start_err):
                result.extend(ch)
                break
        except IOError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                if timeout_flag:
                    return b"\x15TimeoutError"
                time.sleep(0.001)
                continue
            raise
        if timeout_flag:
            return b"\x15TimeoutError"

    # Read content until end marker
    while True:
        try:
            ch = sys.stdin.buffer.read(1)
            if not ch:
                return b"\x15TimeoutError"  # EOF mid-response: Virtuoso died
            if ch[0] == end:
                break
            result.extend(ch)
        except IOError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                if timeout_flag:
                    return b"\x15TimeoutError"
                time.sleep(0.001)
                continue
            raise
        if timeout_flag:
            return b"\x15TimeoutError"

    return result

def handle_external_connection(conn, addr):
    global watchdog_timer, timeout_flag

    try:
        chunks = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        request_data = json.loads(data.decode("utf-8"))
        request_nonce = request_data.get("nonce")

        # Capability handshake: describe this daemon WITHOUT executing
        # anything (the request carries no "skill" field, so even a
        # pre-token daemon cannot act on it -- it fails with a NAK and
        # the client learns the skew before its first real command).
        if request_data.get("op") == "hello":
            auth_error = _auth_error(request_data, "hello")
            if BRIDGE_TOKEN:
                if auth_error:
                    _safe_sendall(conn, ("\x15" + auth_error).encode("utf-8"))
                    return
                body = _capabilities_body().encode("utf-8")
                resp_mac = _mac_hex(_RESP_DOMAIN, str(request_nonce), b"\x02", body)
                _safe_sendall(conn, b"\x02" + resp_mac.encode("ascii") + body)
            else:
                _safe_sendall(conn, b"\x02" + _capabilities_body().encode("utf-8"))
            return

        auth_error = _auth_error(request_data, "req")
        if auth_error:
            # Unauthenticated or foreign client: refuse without executing.
            _safe_sendall(conn, ("\x15" + auth_error).encode("utf-8"))
            return

        skill_code = request_data["skill"]
        timeout_seconds = request_data["timeout"]

        timeout_flag = False

        # Clear stdin buffer before writing
        while True:
            try:
                ch = sys.stdin.buffer.read(1)
                if not ch:
                    break
            except IOError:
                break

        # Multi-line SKILL: write to temp file and load() it.
        # This preserves comments (;) which would break single-line flattening.
        # We wrap the code so the return value is captured in a global variable,
        # because load() itself only returns t, not the last expression's value.
        tmp_il_path = None
        if "\n" in skill_code:
            fd, tmp_il_path = tempfile.mkstemp(suffix=".il", prefix="vb_eval_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"_vb_eval_result = progn(\n{skill_code}\n)\n")
            escaped_path = tmp_il_path.replace("\\", "/")
            send_code = f'load("{escaped_path}") hiFlush() _vb_eval_result\n'
        else:
            send_code = f'let(((__vb_r {skill_code})) hiFlush() __vb_r)\n'

        sys.stdout.buffer.write(send_code.encode("utf-8"))
        sys.stdout.buffer.flush()

        # Start watchdog timer
        watchdog_timer = threading.Timer(timeout_seconds, watchdog_callback)
        watchdog_timer.daemon = True
        watchdog_timer.start()

        returnData = read_until_delimiter()

        if not timeout_flag:
            timeout_flag = True
        watchdog_timer.cancel()

        # Authenticate the response so the client can detect a squatted
        # port: the MAC covers the status marker AND the full body.
        if BRIDGE_TOKEN and request_nonce:
            resp_mac = _mac_hex(
                _RESP_DOMAIN, str(request_nonce), returnData[:1], returnData[1:]
            )
            _safe_sendall(
                conn,
                returnData[:1] + resp_mac.encode("ascii") + returnData[1:],
            )
        else:
            _safe_sendall(conn, returnData)

        # Stats: count this call and tag as error if SKILL sent NAK
        # (0x15) or the response is empty/malformed.  Throttled emit
        # below pushes the totals to SKILL via stderr.
        global _RB_CALLS, _RB_ERRORS
        _RB_CALLS += 1
        if not returnData or returnData[:1] != b"\x02":
            _RB_ERRORS += 1
        _emit_stat()

        # Clean up temp file if we used one
        if tmp_il_path:
            try:
                os.unlink(tmp_il_path)
            except OSError:
                pass

    except json.JSONDecodeError as e:
        _safe_sendall(conn, f"\x15JSONDecodeError: {e}".encode("utf-8"))
    except Exception as e:
        traceback.print_exc()
        _safe_sendall(conn, f"\x15{e}".encode("utf-8"))
    finally:
        timeout_flag = True
        if watchdog_timer:
            watchdog_timer.cancel()
        _safe_close_connection(conn)

def start_server():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, PORT))
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                sys.stderr.write(f"ERROR: Port {PORT} is already in use. Another daemon may be running.\n")
                sys.exit(1)
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
        # Best-effort outward-facing IPv4: ask the kernel which source
        # IP it would pick for outbound traffic.  UDP connect() sends
        # nothing on the wire, it just runs the route lookup so that
        # getsockname() returns the chosen local address.  Bypasses
        # /etc/hosts entries that map hostname to 127.0.0.1.
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
            "[RB-banner] pid={pid} bind={host}:{port} host={hn} ip={ip} auth={auth}\n".format(
                pid=os.getpid(), host=HOST, port=PORT, hn=_hn, ip=(_ip or "unknown"),
                auth=("on" if BRIDGE_TOKEN else "off"),
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

if __name__ == "__main__":
    start_server()
