"""SKILL Finder — query Cadence SKILL API documentation from .fnd database.

The SKILL Finder database lives under
``<ic_install_path>/doc/finder/SKILL/<functionArea>/*.fnd``.
Each entry has three fields: name, syntax, and description.

Usage (local mode)::

    from virtuoso_bridge.virtuoso.skill_finder import SKILLFinder

    finder = SKILLFinder()
    results = finder.search("dbOpenCellViewByType")

Usage (remote mode, via VirtuosoClient)::

    client = VirtuosoClient.from_env()
    results = client.find_skill("dbOpen")
"""

from __future__ import annotations

import enum
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .parser import SkillEntry, parse_fnd_directory


class SearchMode(enum.Enum):
    """Supported search modes for SKILL Finder queries."""

    FUZZY = "fuzzy"  # Case-insensitive substring match (default)
    PREFIX = "prefix"  # Name starts with query
    SUFFIX = "suffix"  # Name ends with query
    EXACT = "exact"  # Exact name match
    REGEX = "regex"  # Regular expression match


@dataclass
class SearchOptions:
    """Options for a SKILL Finder search."""

    mode: SearchMode = SearchMode.FUZZY
    limit: int = 50
    case_sensitive: bool = False


@dataclass
class SKILLFinder:
    """SKILL API documentation finder backed by Cadence .fnd files.

    Attributes
    ----------
    source_dir : Path | None
        Path to the SKILL Finder root directory.  None until
        :meth:`discover` or :meth:`load` is called.
    entries : list[SkillEntry]
        All loaded entries.  Empty until :meth:`load` is called.
    loaded : bool
        Whether entries have been loaded from disk.
    """

    source_dir: Path | None = None
    entries: list[SkillEntry] = field(default_factory=list)
    loaded: bool = False

    # ------------------------------------------------------------------
    # Discovery & loading
    # ------------------------------------------------------------------

    def discover(
        self, remote_runner=None, profile: str | None = None
    ) -> Path | None:
        """Find the SKILL Finder directory on a remote server.

        Strategy: find the virtuoso binary (``which virtuoso``), then walk
        up its parent directories until ``doc/finder/SKILL`` is found.

        Parameters
        ----------
        remote_runner : SSHRunner | None
            SSH runner for remote discovery.  If None, uses the local
            filesystem.
        profile : str | None
            Connection profile name, passed to resolve VB_CADENCE_CSHRC_<profile>
            env var (same suffix mechanism as spectre status).

        Returns
        -------
        Path | None
            The SKILL Finder root directory, or None if not found.
        """
        self._profile = profile
        if remote_runner is None:
            return self._discover_local()

        return self._discover_remote(remote_runner, profile)

    def _discover_local(self) -> Path | None:
        import shutil

        virtuoso_path = shutil.which("virtuoso")
        if not virtuoso_path:
            return None
        return self._walk_up_find(Path(virtuoso_path), "doc/finder/SKILL")

    def _discover_remote(self, runner, profile: str | None) -> Path | None:
        """Find SKILL Finder root on a remote server via SSH.

        Strategy: establish the Cadence environment (Lmod modules and/or
        cshrc files, via cadence_env_setup_csh), then use ``which virtuoso``
        to locate the binary, then walk up its parent directories to find
        ``doc/finder/SKILL``.
        """
        # 1. Establish Cadence env, then find virtuoso.  Same detection shape
        # as the spectre probe (cli._print_spectre_status): a bash fast path
        # for when virtuoso is already on PATH, falling back to a csh shell
        # that loads modules / sources cshrc.  csh module chatter goes to
        # stderr and is discarded (2>/dev/null).
        from virtuoso_bridge.spectre.runner import cadence_env_setup_csh

        suffix = f"_{profile}" if profile else ""
        setup = cadence_env_setup_csh(suffix)
        fast = "which virtuoso 2>/dev/null"
        if setup:
            csh_inner = f"{setup}; which virtuoso"
            slow = f"csh -f -c {shlex.quote(csh_inner)} 2>/dev/null"
            combined = f"{{ {fast}; }} || {{ {slow}; }}"
        else:
            combined = fast
        find_virtuoso_script = f"bash -l -c {shlex.quote(combined)}"
        r = runner.run_command(find_virtuoso_script, timeout=30)

        virtuoso_path = ""
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            # Ignore any stray chatter; accept only a real virtuoso path.
            if line.endswith("/virtuoso") or line == "virtuoso":
                virtuoso_path = line
                break
        if not virtuoso_path:
            return None

        # 2. Walk up from virtuoso to find doc/finder/SKILL.
        walk_script = (
            f'p="{virtuoso_path}"; '
            'while [ -n "$p" ] && [ "$p" != "/" ]; do '
            '  if [ -d "$p/doc/finder/SKILL" ]; then echo "$p/doc/finder/SKILL"; exit 0; fi; '
            '  p=$(dirname "$p"); '
            'done; exit 1'
        )
        r2 = runner.run_command(f"bash -c {shlex.quote(walk_script)}", timeout=15)
        if r2.returncode == 0 and r2.stdout.strip():
            return Path(r2.stdout.strip())
        return None

    @staticmethod
    def _walk_up_find(start: Path, target_tail: str) -> Path | None:
        """Walk from ``start`` up to root, looking for ``target_tail``."""
        current = start.resolve()
        while True:
            candidate = current / target_tail
            if candidate.is_dir():
                return candidate
            parent = current.parent
            if parent == current:
                break
            current = parent
        return None

    def load(self, source_dir: Path | str | None = None) -> None:
        """Load all .fnd entries from *source_dir*.

        If *source_dir* is None, uses :attr:`source_dir`.  Raises if
        neither is set.

        Parameters
        ----------
        source_dir : Path | str | None
            Path to the SKILL Finder root directory.
        """
        root = Path(source_dir) if source_dir else self.source_dir
        if root is None:
            raise ValueError("source_dir must be provided or already set")

        self.source_dir = Path(root)
        self.entries = parse_fnd_directory(self.source_dir)
        self.loaded = True

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        mode: SearchMode | str = SearchMode.FUZZY,
        limit: int = 50,
        include_desc: bool = False,
    ) -> list[SkillEntry]:
        """Search for SKILL entries matching *query*.

        Parameters
        ----------
        query : str
            Search string.
        mode : SearchMode | str
            Search mode (default: fuzzy substring).
        limit : int
            Maximum number of results (default: 50).
        include_desc : bool
            Also search in the description field (default: False).

        Returns
        -------
        list[SkillEntry]
            Matching entries, best-effort sorted by relevance.
        """
        if not self.loaded:
            return []

        if isinstance(mode, str):
            try:
                mode = SearchMode(mode)
            except ValueError:
                mode = SearchMode.FUZZY

        if mode == SearchMode.EXACT:
            results = self._exact(query, include_desc)
        elif mode == SearchMode.PREFIX:
            results = self._prefix(query, include_desc)
        elif mode == SearchMode.SUFFIX:
            results = self._suffix(query, include_desc)
        elif mode == SearchMode.REGEX:
            results = self._regex(query, include_desc)
        else:
            results = self._fuzzy(query, include_desc)

        return sorted(results, key=lambda e: e.name)[:limit]

    def _exact(self, query: str, include_desc: bool = False) -> list[SkillEntry]:
        return [e for e in self.entries if e.name == query]

    def _prefix(self, query: str, include_desc: bool = False) -> list[SkillEntry]:
        ql = query.lower()
        return [e for e in self.entries
                if e.name.startswith(query)
                or (include_desc and ql in e.description.lower())]

    def _suffix(self, query: str, include_desc: bool = False) -> list[SkillEntry]:
        ql = query.lower()
        return [e for e in self.entries
                if e.name.endswith(query)
                or (include_desc and ql in e.description.lower())]

    def _regex(self, query: str, include_desc: bool = False) -> list[SkillEntry]:
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            return []
        return [e for e in self.entries
                if pattern.search(e.name)
                or (include_desc and pattern.search(e.description))]

    def _fuzzy(self, query: str, include_desc: bool = False) -> list[SkillEntry]:
        q = query.lower()
        return [e for e in self.entries
                if q in e.name.lower()
                or (include_desc and q in e.description.lower())]

    # ------------------------------------------------------------------
    # CLI helper
    # ------------------------------------------------------------------

    def format_result(self, entry: SkillEntry) -> str:
        """Format a single entry for human-readable CLI output."""
        desc = entry.description.strip('"').strip()
        # Normalise syntax: collapse embedded newlines/spaces into single spaces
        syntax = re.sub(r'\s+', ' ', entry.syntax.strip('"')).strip()
        source = f" [{entry.source_file}]" if entry.source_file else ""
        return (
            f"  {entry.name}{source}\n"
            f"    Syntax : {syntax}\n"
            f"    Desc   : {desc}\n"
        )

    def format_results(self, results: list[SkillEntry], query: str) -> str:
        """Format search results for CLI output."""
        if not results:
            return f"No results for: {query}"

        header = f"SKILL Finder — {len(results)} result(s) for '{query}':\n"
        lines = [self.format_result(e) for e in results]
        return header + "\n".join(lines)
