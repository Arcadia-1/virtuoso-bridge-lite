# core/ — The Entire Bridge in 3 Files

This is the raw mechanism. No package, no pip install, no CLI.

These 3 files correspond to the two decoupled layers in the full package:

| File | Lines | Layer | Full package equivalent |
|---|---|---|---|
| `ramic_bridge.il` | 33 | Virtuoso side | `resources/ramic_bridge.il` |
| `ramic_daemon.py` | 90 | TCP relay | `resources/ramic_bridge_daemon_*.py` |
| `bridge_client.py` | 40 | Client side | `VirtuosoClient` (pure TCP) |

The SSH tunnel (`SSHClient` in the full package) is not included here — you set it up manually.

## How to Use

```bash
# 1. Copy daemon to remote
scp core/ramic_daemon.py remote:/tmp/

# 2. In Virtuoso CIW, load the IL file:
#    load("/tmp/ramic_bridge.il")
#    (it auto-starts the daemon on port 65432)

# 3. SSH tunnel (this is what SSHClient does automatically)
ssh -N -L 65432:localhost:65432 remote &

# 4. Run SKILL from your machine
python core/bridge_client.py '1+2'
python core/bridge_client.py 'hiGetCurrentWindow()'
python core/bridge_client.py 'geGetEditCellView()~>cellName'
```

## How It Works

```
Your Machine                          Remote Virtuoso Server
────────────                          ──────────────────────

bridge_client.py                      Virtuoso process
(= VirtuosoClient)                       (= ramic_bridge.il)
    │                                     │
    │ TCP: {"skill":"1+2"}                │
    ├──── SSH tunnel ────────────► ramic_daemon.py
    │     (= SSHClient)               │
    │                                     │ stdout: "1+2"
    │                                     ├──► evalstring("1+2")
    │                                     │        │
    │                                     │        ▼
    │                                     │ stdin: "\x02 3 \x1e"
    │                                     ◄──┘
    │ TCP: "\x02 3"                       │
    ◄──── SSH tunnel ─────────────┘
    │
    ▼
   "3"
```

`core/` is for understanding the mechanism. For production use, install the full package (`pip install -e .`) which adds SSHClient (auto SSH tunnel, reconnection, file transfer) and VirtuosoClient (layout/schematic API, Spectre integration).
