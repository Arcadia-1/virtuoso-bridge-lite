"""Optional decorators around VirtuosoClient.

These are pluggable utilities — the bridge itself stays policy-free.
Users supply their own callables (sanitize_fn, etc.) so the bridge has
no opinion on what is sensitive in a given project.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable


class SanitizingClient:
    """Wrap a :class:`VirtuosoClient`; every ``download_file()`` also writes
    a sanitized sibling copy to ``<local.parent>/sanitized/<local.name>``.

    The caller supplies the ``sanitize_fn`` callable — the bridge has no
    opinion on *what* is sensitive.  All non-``download_file`` methods are
    transparently delegated to the wrapped client.

    Example::

        from virtuoso_bridge import VirtuosoClient, SanitizingClient

        def my_redactor(text: str) -> str:
            return text.replace("mycompany", "REDACTED")

        client = SanitizingClient(VirtuosoClient.from_env(), my_redactor)
        client.download_file("/remote/file.scs", "local/file.scs")
        # -> local/file.scs              (raw)
        # -> local/sanitized/file.scs    (redacted)

        # Per-call opt-out:
        client.download_file(remote, local, sanitize=False)
    """

    def __init__(self, inner, sanitize_fn: Callable[[str], str]):
        self._inner = inner
        self._sanitize = sanitize_fn

    def download_file(self, remote_path, local_path, *,
                      sanitize: bool = True, **kwargs):
        result = self._inner.download_file(remote_path, local_path, **kwargs)
        if sanitize:
            local = Path(local_path)
            try:
                text = local.read_text(encoding="utf-8")
            except (UnicodeDecodeError, FileNotFoundError):
                # Binary or missing — leave raw, don't produce sanitized copy
                return result
            out_dir = local.parent / "sanitized"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / local.name).write_text(
                self._sanitize(text), encoding="utf-8"
            )
        return result

    def __getattr__(self, name):
        # Delegate execute_skill, upload_file, load_il, open_window, etc.
        return getattr(self._inner, name)
