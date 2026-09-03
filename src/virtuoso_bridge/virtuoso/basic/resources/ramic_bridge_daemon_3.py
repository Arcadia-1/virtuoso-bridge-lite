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
import traceback

# ---------------------------------------------------------------------------
# Bridge token authentication.
#
# The daemon port is host-global: any local user can end up bound to it (or
# deliberately squat it).  A token shared with legitimate clients via a
# 0600 file (~/.virtuoso-bridge/bridge_token, or RB_TOKEN_PATH) gates every
# request: no valid HMAC(token, nonce) -> no SKILL execution, and responses
# carry HMAC(token, nonce + ":resp") so clients can detect a squatter.
# ---------------------------------------------------------------------------
TOKEN_PATH_ENV = "RB_TOKEN_PATH"
_MAC_LEN = 64
_RESP_SALT = ":resp"


def _token_file_path():
    override = os.environ.get(TOKEN_PATH_ENV, "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".virtuoso-bridge", "bridge_token")


def _load_or_create_token():
    path = _token_file_path()
    try:
        with open(path, "r") as handle:
            token = handle.read().strip()
        if len(token) >= 32 and all(c in "0123456789abcdefABCDEF" for c in token):
            return token.lower()
    except OSError:
        pass
    token = binascii.hexlify(os.urandom(32)).decode("ascii")
    try:
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
            os.chmod(parent, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(token + "\n")
        os.chmod(path, 0o600)
    except OSError:
        sys.stderr.write(
            "[RB-auth] WARNING: cannot read or create token file %s; "
            "daemon runs UNAUTHENTICATED\n" % path
        )
        return None
    return token


BRIDGE_TOKEN = _load_or_create_token()


def _hmac_hex(message):
    return _hmac.new(
        BRIDGE_TOKEN.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _check_request_auth(request_data):
    """Return an error string for unauthenticated requests, else None."""
    if not BRIDGE_TOKEN:
        return None
    nonce = request_data.get("nonce")
    mac = request_data.get("mac")
    if not nonce or not mac:
        return (
            "AuthError: bridge token required — this daemon rejects "
            "unauthenticated SKILL (client too old, or unauthorized)"
        )
    if not _hmac_hex(str(nonce)) == str(mac).lower():
        return (
            "AuthError: bridge token mismatch — the daemon on this port "
            "belongs to a different user (or the token was rotated); run "
            "`virtuoso-bridge restart` after RBStop()"
        )
    return None


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

# Get Virtuoso's PID (grandparent: virtuoso -> sh -> this daemon)
def get_grandparent_pid():
    try:
        with open('/proc/self/stat', 'r') as f:
            parent_pid = int(f.read().split()[3])
        with open(f'/proc/{parent_pid}/stat', 'r') as f:
            return int(f.read().split()[3])
    except Exception:
        raise Exception("Failed to get Virtuoso PID")

virtuoso_pid = get_grandparent_pid()

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
                if timeout_flag:
                    return b"\x15TimeoutError"
                time.sleep(0.001)
                continue
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
                if timeout_flag:
                    return b"\x15TimeoutError"
                time.sleep(0.001)
                continue
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

        auth_error = _check_request_auth(request_data)
        if auth_error:
            # Unauthenticated or foreign client: refuse without executing.
            _safe_sendall(conn, ("\x15" + auth_error).encode("utf-8"))
            return

        skill_code = request_data["skill"]
        timeout_seconds = request_data["timeout"]
        request_nonce = request_data.get("nonce")

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
            import tempfile
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
        # port: STX/NAK + HMAC(token, nonce + ":resp") prefix + body.
        if BRIDGE_TOKEN and request_nonce:
            resp_mac = _hmac_hex(str(request_nonce) + _RESP_SALT)
            _safe_sendall(
                conn,
                returnData[:1] + resp_mac.encode("utf-8") + returnData[1:],
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
