# LangGraph + Virtuoso Bridge

LangGraph agents that execute SKILL in a running Virtuoso session.

## Environment Variables

Set these in your shell before starting Virtuoso:

```bash
export MY_BRIDGE_DIR=/path/to/this/directory   # full path of this directory
export OPENAI_API_KEY=your_key_here
```

## Python Environment

Requires Python 3.11, 3.12, or 3.13. Check first:

```bash
python3 --version
```

Then create a virtual environment and install dependencies:

```bash
python3 -m venv langgraph-env
source langgraph-env/bin/activate
pip install -r requirements.txt
```

## Start the Virtuoso Daemon

1. Start Virtuoso (with `MY_BRIDGE_DIR` already exported in the shell)

2. In the Virtuoso CIW, load the bridge:
   ```
   load("/path/to/this/directory/my_bridge.il")
   ```
   You should see:
   ```
   [my_bridge] daemon: /path/to/this/directory/my_daemon.py  port: 12345
   ```

3. The daemon stays alive as long as Virtuoso is running. It exits automatically when Virtuoso exits.

To stop the daemon manually from CIW:
```
ipcKillProcess(myIpc)
```

## Run Examples

```bash
source langgraph-env/bin/activate
python example/router.py
```
