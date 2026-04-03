#!/usr/bin/env python2.7
"""RAMIC Bridge Daemon - Virtuoso Skill Bridge Service (Python 2.7 Version)"""

import sys
import socket
import os
import fcntl
import json
import signal
import threading
import time
import errno
import traceback

# Python 2.7 compatibility: try to import psutil, fallback to manual PID detection
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# Command line arguments for host and port
HOST = sys.argv[1]
PORT = int(sys.argv[2])

# Global timeout control flag
timeout_flag = False

# Get Virtuoso's PID - this is the process we need to send signals to
if PSUTIL_AVAILABLE:
    # Use psutil if available
    current_process = psutil.Process()
    parent_process = current_process.parent()
    # Python 2.7 compatibility: handle None case
    if parent_process and parent_process.parent():
        virtuoso_pid = parent_process.parent().pid
    else:
        virtuoso_pid = os.getppid()
else:
    # Fallback: use /proc filesystem to get parent process info
    def get_grandparent_pid():
        try:
            # Read current process info from /proc
            with open('/proc/self/stat', 'r') as f:
                stat_data = f.read().split()
                # Parent PID is the 4th field (index 3)
                parent_pid = int(stat_data[3])

                # Now get the parent's parent PID (grandparent)
                with open('/proc/{0}/stat'.format(parent_pid), 'r') as f2:
                    stat_data2 = f2.read().split()
                    # Grandparent PID is the 4th field (index 3) of parent's stat
                    grandparent_pid = int(stat_data2[3])
                    return grandparent_pid
        except:
            # If /proc is not available, raise an error
            raise Exception("Failed to get Virtuoso PID")

    virtuoso_pid = get_grandparent_pid()

# Python 2.7 compatibility: print statement instead of print() function
# print("Virtuoso PID: {0}".format(virtuoso_pid))

# Set stdin to non-blocking mode for reading Virtuoso responses
# Note: Only stdin needs to be non-blocking, stdout should remain blocking
stdin_fd = sys.stdin.fileno()
stdin_fl = fcntl.fcntl(stdin_fd, fcntl.F_GETFL)
fcntl.fcntl(stdin_fd, fcntl.F_SETFL, stdin_fl | os.O_NONBLOCK)

# Keep stdout blocking for reliable writes
stdout_fd = sys.stdout.fileno()
stdout_fl = fcntl.fcntl(stdout_fd, fcntl.F_GETFL)
fcntl.fcntl(stdout_fd, fcntl.F_SETFL, stdout_fl & ~os.O_NONBLOCK)  # Ensure blocking

# Global watchdog timer reference
watchdog_timer = None

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
            if not ch:  # Python 2.7: empty string means no data
                continue
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

        skill_code = request_data["skill"]
        timeout_seconds = request_data["timeout"]

        # Reset timeout flag
        timeout_flag = False

        # Send skill script to Virtuoso
        # Python 2.7 compatibility: ensure skill_code is string
        if hasattr(skill_code, 'encode'):  # Check if it's unicode
            skill_code = skill_code.encode('utf-8')

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

        sys.stdout.write(skill_code)
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
            conn.sendall(str(returnData))
        elif hasattr(returnData, 'encode'):  # Check if it's unicode
            conn.sendall(returnData.encode('utf-8'))
        else:
            conn.sendall(returnData)

    except ValueError as e:
        # Python 2.7 compatibility: handle JSON decode errors
        error_msg = "\x15JSONDecodeError: {0}".format(str(e))
        if hasattr(error_msg, 'encode'):  # Check if it's unicode
            error_msg = error_msg.encode('utf-8')
        conn.sendall(error_msg)
    except Exception as e:
        # Python 2.7 compatibility: except Exception, e syntax
        traceback.print_exc()
        error_msg = "\x15{0}".format(str(e))
        if hasattr(error_msg, 'encode'):  # Check if it's unicode
            error_msg = error_msg.encode('utf-8')
        conn.sendall(error_msg)
    finally:
        # Ensure watchdog timer is cleaned up
        timeout_flag = True
        if watchdog_timer:
            watchdog_timer.cancel()
        conn.shutdown(socket.SHUT_RDWR)
        conn.close()

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
        while True:
            conn, addr = s.accept()
            handle_external_connection(conn, addr)
    finally:
        s.close()

# Start the server
if __name__ == "__main__":
    start_server()