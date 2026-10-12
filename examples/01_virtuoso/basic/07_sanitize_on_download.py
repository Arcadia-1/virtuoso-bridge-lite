#!/usr/bin/env python3
"""Auto-sanitize every file you download from the remote.

``SanitizingClient`` wraps a :class:`VirtuosoClient` so that each
``download_file()`` call produces BOTH the raw file and a sanitized
sibling copy under ``<local_path>.parent/sanitized/<local_path.name>``.

The bridge is policy-free: you supply the ``sanitize_fn`` callable.
The redaction map (who maps to what) is inherently project-specific
— user names, library names, PDK paths — so it must live in your
project, not in the bridge.

Usage::

    python 07_sanitize_on_download.py                 # writes and pulls a demo file
    python 07_sanitize_on_download.py /path/on/remote/netlist.scs

Prerequisites:
- virtuoso-bridge tunnel running (``virtuoso-bridge start``)

Downloads land in ``output/sanitize/`` next to this script.  Customize the
``TOKENS`` dict below for your own redaction map.
"""

import argparse
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))

from pathlib import Path

from virtuoso_bridge import VirtuosoClient, SanitizingClient
from virtuoso_bridge.virtuoso.ops import escape_skill_string

OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "sanitize"
DEMO_TEXT = "// netlist by alice for acme-corp\ninclude \"/pdk/proprietary/models.scs\"\n"


# ----------------------------------------------------------------------
# Customize: your redaction map.
# ----------------------------------------------------------------------

# Keys (what to find) are matched longest-first so a short substring
# never overwrites a longer enclosing token.  Values are the replacements.
TOKENS = {
    "acme-corp":        "REDACTED_CORP",
    "alice":            "USER_A",
    "bob":              "USER_B",
    "/home/alice":      "/home/USER_A",
    "/home/bob":        "/home/USER_B",
    "/pdk/proprietary": "/PDK/REDACTED",
}
# ----------------------------------------------------------------------


def make_sanitize_fn(tokens: dict[str, str]):
    """Return a callable that applies ``tokens`` as a pure dict replacement.

    Longer keys first — prevents "alice" eating the start of "alicebob".
    """
    ordered = sorted(tokens, key=len, reverse=True)

    def apply(text: str) -> str:
        for src in ordered:
            text = text.replace(src, tokens[src])
        return text

    return apply


def write_demo_file(client) -> str:
    """Write DEMO_TEXT to a temp file on the Virtuoso host."""
    text = escape_skill_string(DEMO_TEXT).replace("\n", "\\n")
    result = client.execute_skill(
        'let((vbPath vbPort) vbPath = makeTempFileName("/tmp/vb_sanitize_demo") '
        f'vbPort = outfile(vbPath) fprintf(vbPort "%s" "{text}") close(vbPort) vbPath)'
    )
    path = (result.output or "").strip().strip('"')
    if not result.ok or not path.startswith("/"):
        raise SystemExit(f"Could not write a demo file on the Virtuoso host: {result.errors or result.output}")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "remote_path", nargs="?",
        help="text file on the Virtuoso host to pull (default: a demo file written for this run)",
    )
    args = parser.parse_args()

    client = SanitizingClient(
        VirtuosoClient.from_env(),
        make_sanitize_fn(TOKENS),
    )

    # Non-download calls delegate transparently to the wrapped client.
    r = client.execute_skill("1+2")
    print(f"[execute_skill] pass-through result: {r.output!r}")

    demo = args.remote_path is None
    remote_path = write_demo_file(client) if demo else args.remote_path
    try:
        return pull(client, remote_path)
    finally:
        if demo:
            client.execute_skill(f'deleteFile("{escape_skill_string(remote_path)}")')


def pull(client, remote_path: str) -> int:
    local = OUTPUT_DIR / "example.scs"

    # Default: produces BOTH output/sanitize/example.scs (raw) and
    # output/sanitize/sanitized/example.scs (redacted).
    result = client.download_file(remote_path, str(local))
    if not result.ok:
        print(f"[download] failed: {'; '.join(result.errors)}")
        return 1
    raw_size = local.stat().st_size
    redacted = local.parent / "sanitized" / local.name
    redacted_size = redacted.stat().st_size if redacted.exists() else 0
    print(f"[download]       raw:       {local}  ({raw_size} B)")
    print(f"[download]       sanitized: {redacted}  ({redacted_size} B)")

    # Opt out per call when you want just the raw file.
    raw_only = OUTPUT_DIR / "raw_only.scs"
    client.download_file(remote_path, str(raw_only), sanitize=False)
    sibling = raw_only.parent / "sanitized" / raw_only.name
    print(f"[sanitize=False] sibling exists? {sibling.exists()}  (expected False)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
