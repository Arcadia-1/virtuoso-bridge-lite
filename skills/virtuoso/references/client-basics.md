# Client and CLI basics

Use the CLI for one-off expressions/files and Python for structured operations
or a multi-step workflow. Run both in the existing project virtual environment.

```bash
virtuoso-bridge eval '1+2'
virtuoso-bridge load my_script.il
```

Use `eval --stdin` for multiline SKILL instead of nesting shell, Python and
SKILL quoting. The CLI wraps multiple forms in `progn` and returns the last form.
For a larger Python workflow, use a saved script; simple Python-only diagnostics
do not require a new file.

```python
from virtuoso_bridge import VirtuosoClient

client = VirtuosoClient.from_env()
result = client.execute_skill("1+2")
print(result.status, result.output)
```

`execute_skill()` returns the expression's value, not text printed to CIW.
Use `printf` explicitly when CIW output is desired; return a structured value
when Python needs it. Check status before treating output as successful data.

## Shell commands in Virtuoso's environment

`client.run_shell_command(command, timeout=...)` executes through SKILL's
`csh()` in the selected Virtuoso process's environment. Commands are csh,
including their quoting, variables and redirection. The bridge captures stdout
and stderr together in `result.output`; check `result.ok` before using it.

```python
result = client.run_shell_command("uname -sr", timeout=15)
if not result.ok:
    raise RuntimeError(result.errors)
print(result.output)
```

For combined output redirected to a file, csh uses `>& run.log`, rather than
POSIX `> run.log 2>&1`. A command containing POSIX syntax can instead invoke an
explicitly quoted `sh -c '...'` inside csh when that shell is available; quoting
must survive both shells. The bridge passes command text through unchanged and
does not attempt to infer or reject the syntax of nested commands or strings.

`SSHRunner.run_command()` is available for commands that belong in an SSH
session. That session has its own environment; it does not inherit changes made
inside the running Virtuoso process. Choose the execution context deliberately.

The timeout bounds the bridge's wait. It does not cancel the command, its child
processes or the SKILL request. Preserve any request handle and reconcile its
outcome before retrying; see [shared-CIW protection](shared-ciw-dialogs.md).

## Batched object attributes

Opaque handles such as `db:0x...` are not Python objects. Read attributes in
one round-trip with `fetch()` / `fetch_one()` instead of one call per field:

```python
instances = client.fetch(
    "geGetEditCellView()~>instances",
    ["name", "cellName", "libName", "viewName"],
)
cellview = client.fetch_one(
    "geGetEditCellView()", ["libName", "cellName", "viewName"],
)
```

Bind to an explicitly selected cellview/window for mutations; these
current-view expressions are convenient read probes, not ownership proof.
Strings are unquoted, `nil` becomes `None`, `t` becomes `True`, and nested
lists are decoded. Bare numeric/symbol atoms remain strings: coerce only after
checking the expected shape.

## Files and captures

`load_il()` uploads a local SKILL file when needed.
`upload_file(local, remote)` and `download_file(remote, local)` explicitly cross
the filesystem boundary; a path returned by SKILL remains on the Virtuoso host.
Choose unique remote/local artifact paths for concurrent workflows.

For one-shot Maestro capture use `virtuoso-bridge snapshot -o output`; confirm
the selected/focused Maestro belongs to the intended design. Use the Python
snapshot API when it is part of a larger same-connection workflow.
A screenshot that depends on SKILL can itself block behind a modal: prefer
out-of-band X11 inspection for that case. Screenshot/window targets and API
signatures must be checked against the installed version.

For execution errors see [troubleshooting.md](troubleshooting.md); for dialog
recovery see [shared-ciw-dialogs.md](shared-ciw-dialogs.md).
