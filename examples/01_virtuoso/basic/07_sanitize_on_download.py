#!/usr/bin/env python3
"""Auto-sanitize every file you download from the remote.

``SanitizingClient`` wraps a :class:`VirtuosoClient` so that each
``download_file()`` call produces BOTH the raw file and a sanitized
sibling copy under ``<local_path>.parent/sanitized/<local_path.name>``.

The bridge is policy-free: you supply the ``sanitize_fn`` callable.
The redaction map (who maps to what) is inherently project-specific
— user names, library names, PDK paths — so it must live in your
project, not in the bridge.

Prerequisites:
- virtuoso-bridge tunnel running (``virtuoso-bridge start``)
- A remote file you have permission to read

Customize ``REMOTE_PATH`` and the ``TOKENS`` dict below, then run.
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))

from pathlib import Path

from virtuoso_bridge import VirtuosoClient, SanitizingClient


# ----------------------------------------------------------------------
# Customize: a remote text file to pull, and your redaction map.
# ----------------------------------------------------------------------
REMOTE_PATH = "/tmp/example_file.scs"

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


def main() -> int:
    client = SanitizingClient(
        VirtuosoClient.from_env(),
        make_sanitize_fn(TOKENS),
    )

    # Non-download calls delegate transparently to the wrapped client.
    r = client.execute_skill("1+2")
    print(f"[execute_skill] pass-through result: {r.output!r}")

    local = Path("downloads") / "example.scs"
    local.parent.mkdir(parents=True, exist_ok=True)

    # Default: produces BOTH downloads/example.scs (raw) and
    # downloads/sanitized/example.scs (redacted).
    client.download_file(REMOTE_PATH, str(local))
    raw_size = local.stat().st_size
    redacted = local.parent / "sanitized" / local.name
    redacted_size = redacted.stat().st_size if redacted.exists() else 0
    print(f"[download]       raw:       {local}  ({raw_size} B)")
    print(f"[download]       sanitized: {redacted}  ({redacted_size} B)")

    # Opt out per call when you want just the raw file.
    raw_only = Path("downloads") / "raw_only.scs"
    client.download_file(REMOTE_PATH, str(raw_only), sanitize=False)
    sibling = raw_only.parent / "sanitized" / raw_only.name
    print(f"[sanitize=False] sibling exists? {sibling.exists()}  (expected False)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
