"""Search helpers for local or remote Cadence documentation trees."""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import os
import re
import shlex
import sqlite3
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Mapping, Sequence


DIRECT_DOC_ROOT_ENV_VARS = ("CADENCE_DOC_ROOT", "CADENCE_DOC_ROOTS")
INSTALL_ROOT_ENV_VARS = ("CDS_INST_DIR", "CDSHOME", "CDS_HOME")
SEARCH_SUFFIXES = {".html", ".htm", ".txt", ".xml", ".json", ".tgf"}
CONTENT_SUFFIXES = SEARCH_SUFFIXES - {".tgf"}
SCHEMA_VERSION = 3
DOCUMENT_PREVIEW_BYTES = 64 * 1024
QUERY_STOPWORDS = {
    "a",
    "all",
    "an",
    "and",
    "are",
    "as",
    "be",
    "by",
    "can",
    "do",
    "does",
    "for",
    "from",
    "how",
    "in",
    "into",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}


@dataclass(frozen=True)
class TgfEntry:
    """A topic-map entry from a Cadence ``.tgf`` file."""

    topic_id: str
    target_path: Path
    anchor: str
    source_path: Path
    line: int | None = None


@dataclass(frozen=True)
class RemoteDocMatch:
    """A remote documentation file matched by lightweight SSH-side search."""

    doc_root: str
    path: str


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._title_depth = 0
        self._title_parts: list[str] = []
        self._body_parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered == "title":
            self._title_depth += 1
        elif lowered in {"script", "style"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "title" and self._title_depth:
            self._title_depth -= 1
        elif lowered in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._title_depth:
            self._title_parts.append(data)
        else:
            self._body_parts.append(data)

    @property
    def title(self) -> str:
        return _squash_whitespace(" ".join(self._title_parts))

    @property
    def text(self) -> str:
        return _squash_whitespace(" ".join(self._body_parts))


def resolve_doc_roots(
    explicit_roots: Sequence[str | Path] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> list[Path]:
    """Resolve configured Cadence documentation roots.

    Resolution is intentionally configuration-only: explicit roots win, then
    direct doc-root environment variables, then Cadence install roots with
    ``doc`` appended. No site-specific install paths are assumed.
    """
    env = os.environ if env is None else env
    paths: list[Path] = []
    seen: set[Path] = set()

    if explicit_roots:
        for root in explicit_roots:
            _append_existing_dir(paths, seen, root)
        return paths

    for name in DIRECT_DOC_ROOT_ENV_VARS:
        for root in _split_env_paths(env.get(name)):
            _append_existing_dir(paths, seen, root)

    for name in INSTALL_ROOT_ENV_VARS:
        for root in _split_env_paths(env.get(name)):
            _append_existing_dir(paths, seen, Path(root) / "doc")

    return paths


def iter_doc_files(doc_roots: Sequence[Path]) -> Iterable[tuple[Path, Path]]:
    """Yield searchable documentation files under each root."""
    for root in doc_roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in SEARCH_SUFFIXES:
                yield root, path


def parse_tgf_line(
    line: str,
    *,
    tgf_path: Path,
    doc_root: Path,
    line_no: int | None = None,
) -> TgfEntry | None:
    """Parse one Cadence topic-map line.

    Cadence ``.tgf`` topic maps generally contain records shaped like
    ``topicId $docSet/path.html anchor HTML``. The ``$docSet`` prefix maps to
    a child directory under the doc root.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    try:
        parts = shlex.split(stripped)
    except ValueError:
        return None
    if len(parts) < 4 or parts[-1].upper() != "HTML":
        return None

    topic_id, target_ref, anchor = parts[0], parts[1], parts[2]
    target_path = _resolve_tgf_target(target_ref, tgf_path=tgf_path, doc_root=doc_root)
    return TgfEntry(
        topic_id=topic_id,
        target_path=target_path,
        anchor=anchor,
        source_path=tgf_path,
        line=line_no,
    )


def search_docs(
    query: str,
    doc_roots: Sequence[str | Path],
    *,
    limit: int = 10,
    cache_root: str | Path | None = None,
    rebuild: bool = False,
) -> list[dict[str, object]]:
    """Search Cadence documentation roots for a query."""
    if cache_root is not None:
        return _search_docs_cached(query, doc_roots, cache_root=cache_root, limit=limit, rebuild=rebuild)

    roots = [Path(root).expanduser().resolve() for root in doc_roots]
    terms = _query_terms(query)
    if not terms or limit <= 0:
        return []

    if _is_identifier_query(query, terms):
        direct_results = _search_direct_identifier_files(query, terms, roots)
        if direct_results:
            return _finalize_results(direct_results, limit)

    files = list(iter_doc_files(roots))
    results: list[dict[str, object]] = []
    scanned_content_paths: set[Path] = set()

    for root, path in files:
        if path.suffix.lower() == ".tgf":
            results.extend(_search_tgf_file(query, terms, root, path))
            continue

        relative_path = _relative_path(path, root)
        if _matches_terms(f"{relative_path} {path.stem}", terms):
            scanned_content_paths.add(path)
            match = _search_content_file(query, terms, root, path)
            if match:
                results.append(match)

    if len(results) >= limit and _is_identifier_query(query, terms):
        return _finalize_results(results, limit)

    for root, path in files:
        if path.suffix.lower() not in CONTENT_SUFFIXES or path in scanned_content_paths:
            continue
        match = _search_content_file(query, terms, root, path)
        if match:
            results.append(match)

    return _finalize_results(results, limit)


def discover_remote_doc_roots(runner, *, profile: str | None = None) -> list[str]:
    """Discover Cadence documentation roots on a remote host.

    The SKILL Finder directory is the most reliable anchor because it already
    exists in this project as remote-capable discovery. Environment variables
    are also included so site-specific documentation roots are not ignored.
    """
    roots: list[str] = []
    seen: set[str] = set()

    try:
        from virtuoso_bridge.virtuoso.skill_finder import SKILLFinder

        finder_root = SKILLFinder().discover(remote_runner=runner, profile=profile)
    except Exception:
        finder_root = None
    if finder_root is not None:
        _append_remote_root(roots, seen, str(finder_root.parent.parent))

    env_result = runner.run_command(_remote_doc_env_script(profile), timeout=30)
    if env_result.returncode == 0:
        for kind, value in _parse_remote_doc_env(env_result.stdout):
            for root in _split_remote_env_paths(value):
                if kind == "INSTALL":
                    root = f"{root.rstrip('/')}/doc"
                _append_remote_root(roots, seen, root)

    return roots


def find_remote_doc_matches(
    runner,
    query: str,
    doc_roots: Sequence[str],
    *,
    limit: int = 10,
    candidate_limit: int | None = None,
) -> list[RemoteDocMatch]:
    """Find remote documentation files likely to satisfy *query*."""
    terms = _query_terms(query)
    if not terms or limit <= 0 or not doc_roots:
        return []

    max_candidates = candidate_limit or max(50, limit * 20)
    result = runner.run_command(
        _remote_doc_search_script(doc_roots, terms, max_candidates=max_candidates),
        timeout=120,
    )
    if result.returncode != 0:
        return []

    matches: list[RemoteDocMatch] = []
    seen: set[tuple[str, str]] = set()
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        root, path = line.split("\t", 1)
        key = (root, path)
        if key in seen:
            continue
        seen.add(key)
        matches.append(RemoteDocMatch(doc_root=root, path=path))
    return matches


def cache_remote_doc_matches(
    runner,
    matches: Sequence[RemoteDocMatch],
    cache_root: str | Path,
    *,
    timeout: int = 30,
) -> tuple[list[Path], dict[Path, str]]:
    """Download remote matches into a local cache and return searchable roots."""
    cache_base = Path(cache_root).expanduser()
    local_roots: list[Path] = []
    root_map: dict[Path, str] = {}
    seen_roots: set[Path] = set()

    for match in matches:
        local_root = cache_base / _safe_remote_root_segment(match.doc_root)
        if local_root not in seen_roots:
            seen_roots.add(local_root)
            local_roots.append(local_root)
            root_map[local_root] = match.doc_root

        relative = _remote_relative_path(match.path, match.doc_root)
        local_path = local_root / relative
        if local_path.exists():
            continue
        result = runner.download(match.path, local_path, recursive=False, timeout=timeout)
        if result.returncode != 0:
            try:
                local_path.unlink()
            except OSError:
                pass

    return local_roots, root_map


def remap_results_to_remote(
    results: Sequence[dict[str, object]],
    root_map: Mapping[Path, str],
) -> list[dict[str, object]]:
    """Rewrite cached local paths in search results back to remote paths."""
    remapped: list[dict[str, object]] = []
    for result in results:
        item = dict(result)
        for field in ("path", "target_path"):
            value = item.get(field)
            if isinstance(value, str):
                remote_value = _local_cache_path_to_remote(value, root_map)
                if remote_value is not None:
                    item[field] = remote_value
        remapped.append(item)
    return remapped


def _split_env_paths(value: str | None) -> Iterable[str]:
    if not value:
        return []
    return (part for part in value.split(os.pathsep) if part)


def _split_remote_env_paths(value: str | None) -> Iterable[str]:
    if not value:
        return []
    return (part for part in value.split(":") if part)


def _append_existing_dir(paths: list[Path], seen: set[Path], raw_path: str | Path) -> None:
    path = Path(raw_path).expanduser()
    if not path.is_dir():
        return
    resolved = path.resolve()
    if resolved not in seen:
        seen.add(resolved)
        paths.append(resolved)


def _append_remote_root(paths: list[str], seen: set[str], raw_path: str) -> None:
    path = raw_path.strip().rstrip("/")
    if not path or path in seen:
        return
    seen.add(path)
    paths.append(path)


def _remote_doc_env_script(profile: str | None) -> str:
    suffix = f"_{profile}" if profile else ""
    cadence_cshrc = os.environ.get(f"VB_CADENCE_CSHRC{suffix}", "") or os.environ.get("VB_CADENCE_CSHRC", "")
    quoted_cshrc = shlex.quote(cadence_cshrc)
    script = (
        'HOSTNAME=`hostname 2>/dev/null || echo localhost`; '
        'export HOSTNAME; '
        f'eval "$(csh -c \'source {quoted_cshrc}; env\' 2>/dev/null '
        '| grep -E "^(CADENCE_DOC_ROOT|CADENCE_DOC_ROOTS|CDS_INST_DIR|CDSHOME|CDS_HOME)=" '
        '| sed \'s/^/export /\')" 2>/dev/null; '
        'printf "DOC\\t%s\\n" "${CADENCE_DOC_ROOT:-}"; '
        'printf "DOC\\t%s\\n" "${CADENCE_DOC_ROOTS:-}"; '
        'printf "INSTALL\\t%s\\n" "${CDS_INST_DIR:-}"; '
        'printf "INSTALL\\t%s\\n" "${CDSHOME:-}"; '
        'printf "INSTALL\\t%s\\n" "${CDS_HOME:-}"'
    )
    return f"sh -lc {shlex.quote(script)}"


def _parse_remote_doc_env(stdout: str) -> Iterable[tuple[str, str]]:
    for line in stdout.splitlines():
        if "\t" not in line:
            continue
        kind, value = line.split("\t", 1)
        if value.strip():
            yield kind, value.strip()


def _remote_doc_search_script(doc_roots: Sequence[str], terms: Sequence[str], *, max_candidates: int) -> str:
    root_args = " ".join(shlex.quote(root) for root in doc_roots)
    term_args = " ".join(shlex.quote(term) for term in terms)
    suffix_expr = " -o ".join(f"-iname {shlex.quote('*' + suffix)}" for suffix in sorted(SEARCH_SUFFIXES))
    script = f"""
# vb_doc_search
count=0
max={int(max_candidates)}
for root in {root_args}; do
  [ -d "$root" ] || continue
  while IFS= read -r -d '' f; do
    rel="${{f#"$root"/}}"
    rel_lower="$(printf '%s' "$rel" | tr '[:upper:]' '[:lower:]')"
    rel_match=1
    for term in {term_args}; do
      case "$rel_lower" in
        *"$term"*) ;;
        *) rel_match=0; break ;;
      esac
    done
    if [ "$rel_match" -eq 1 ]; then
      printf '%s\\t%s\\n' "$root" "$f"
      count=$((count + 1))
      [ "$count" -ge "$max" ] && exit 0
      continue
    fi

    content_match=1
    for term in {term_args}; do
      if ! LC_ALL=C grep -F -I -i -q -- "$term" "$f" 2>/dev/null; then
        content_match=0
        break
      fi
    done
    if [ "$content_match" -eq 1 ]; then
      printf '%s\\t%s\\n' "$root" "$f"
      count=$((count + 1))
      [ "$count" -ge "$max" ] && exit 0
    fi
  done < <(find "$root" -type f \\( {suffix_expr} \\) -print0 2>/dev/null)
done
"""
    return f"bash -lc {shlex.quote(script)}"


def _safe_remote_root_segment(remote_root: str) -> str:
    stripped = remote_root.strip().strip("/") or "root"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stripped).strip("_")
    return safe or "root"


def _remote_relative_path(remote_path: str, remote_root: str) -> Path:
    root = remote_root.rstrip("/")
    if remote_path == root:
        return Path(Path(remote_path).name)
    if remote_path.startswith(root + "/"):
        return Path(remote_path[len(root) + 1 :])
    return Path(Path(remote_path).name)


def _local_cache_path_to_remote(path: str, root_map: Mapping[Path, str]) -> str | None:
    local_path = Path(path)
    for local_root, remote_root in root_map.items():
        try:
            rel = local_path.resolve().relative_to(local_root.resolve())
        except ValueError:
            continue
        return f"{remote_root.rstrip('/')}/{rel.as_posix()}"
    return None


def _search_content_file(query: str, terms: Sequence[str], root: Path, path: Path) -> dict[str, object] | None:
    raw = _read_text(path)
    title, text = _extract_document_text(path, raw)
    relative_path = _relative_path(path, root)
    haystack = f"{relative_path} {title} {text}"
    if not _matches_terms(haystack, terms):
        return None

    line = _first_matching_line(raw, terms)
    return {
        "kind": "document",
        "path": str(path),
        "relative_path": relative_path,
        "title": title or path.stem,
        "line": line,
        "snippet": _snippet(text or raw, query, terms),
        "score": _score_match(relative_path, title, text, terms),
    }


def _search_direct_identifier_files(query: str, terms: Sequence[str], roots: Sequence[Path]) -> list[dict[str, object]]:
    stem = _safe_identifier_stem(query)
    if stem is None:
        return []

    results: list[dict[str, object]] = []
    seen: set[Path] = set()
    for root in roots:
        for candidate in _direct_identifier_candidates(root, stem):
            resolved = candidate.resolve()
            if resolved in seen or not resolved.is_file():
                continue
            seen.add(resolved)
            match = _search_content_file(query, terms, root, resolved)
            if match:
                match["score"] = int(match["score"]) + 20
                results.append(match)
    return results


def _search_tgf_file(query: str, terms: Sequence[str], root: Path, path: Path) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for line_no, line in enumerate(_read_text(path).splitlines(), start=1):
        entry = parse_tgf_line(line, tgf_path=path, doc_root=root, line_no=line_no)
        if entry is None:
            continue
        haystack = f"{entry.topic_id} {entry.anchor} {entry.target_path.name} {entry.target_path}"
        if not _matches_terms(haystack, terms):
            continue
        target_relative = _relative_path(entry.target_path, root)
        results.append(
            {
                "kind": "topic",
                "path": str(entry.source_path),
                "relative_path": _relative_path(entry.source_path, root),
                "line": entry.line,
                "topic_id": entry.topic_id,
                "anchor": entry.anchor,
                "target_path": str(entry.target_path),
                "target_relative_path": target_relative,
                "title": entry.anchor or Path(target_relative).stem,
                "snippet": _snippet(f"{entry.topic_id} {entry.anchor} {target_relative}", query, terms),
                "score": _score_match(target_relative, entry.anchor, entry.topic_id, terms) + 2,
            }
        )
    return results


def _finalize_results(results: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
    results.sort(key=lambda item: (-int(item["score"]), str(item.get("relative_path") or item.get("target_relative_path"))))
    return [_without_score(item) for item in results[:limit]]


def _resolve_tgf_target(target_ref: str, *, tgf_path: Path, doc_root: Path) -> Path:
    variable_match = re.match(r"^\$([^/\\]+)[/\\]?(.*)$", target_ref)
    if variable_match:
        doc_dir, rest = variable_match.groups()
        return doc_root / doc_dir / rest
    target = Path(target_ref)
    if target.is_absolute():
        return target
    return (tgf_path.parent / target).resolve()


def _extract_document_text(path: Path, raw: str) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        parser = _HtmlTextExtractor()
        parser.feed(raw)
        return parser.title, parser.text

    text = html.unescape(_squash_whitespace(raw))
    title = path.stem
    return title, text


def _query_terms(query: str) -> list[str]:
    terms = [_normalize_query_term(term) for term in re.findall(r"[A-Za-z0-9_.$:/+-]+", query) if term]
    meaningful_terms = [term for term in terms if term not in QUERY_STOPWORDS]
    return meaningful_terms or terms


def _normalize_query_term(term: str) -> str:
    lowered = term.lower()
    if _looks_like_api_query_term(term) or not term.isalpha() or len(term) <= 3:
        return lowered
    if lowered.endswith("ies") and len(lowered) > 4:
        return lowered[:-3] + "y"
    if lowered.endswith(("ches", "shes", "sses", "xes", "zes")):
        return lowered[:-2]
    if lowered.endswith("s") and not lowered.endswith("ss"):
        return lowered[:-1]
    return lowered


def _looks_like_api_query_term(term: str) -> bool:
    return any(ch in term for ch in "_.$:/+-") or any(ch.isupper() for ch in term[1:])


def _safe_identifier_stem(query: str) -> str | None:
    stripped = query.strip()
    if re.fullmatch(r"[A-Za-z0-9_.$+-]+", stripped):
        return stripped
    return None


def _direct_identifier_candidates(root: Path, stem: str) -> Iterable[Path]:
    suffixes = (".html", ".htm", ".txt", ".xml", ".json")
    for suffix in suffixes:
        yield root / f"{stem}{suffix}"

    try:
        children = list(root.iterdir())
    except OSError:
        return
    for child in children:
        if not child.is_dir():
            continue
        for suffix in suffixes:
            yield child / f"{stem}{suffix}"


def _is_identifier_query(query: str, terms: Sequence[str]) -> bool:
    stripped = query.strip()
    return len(terms) == 1 and (
        any(character in stripped for character in "_.$:/+-")
        or (any(character.islower() for character in stripped) and any(character.isupper() for character in stripped))
        or len(stripped) >= 16
    )


def _matches_terms(haystack: str, terms: Sequence[str]) -> bool:
    lowered = haystack.lower()
    return all(term in lowered for term in terms)


def _score_match(relative_path: str, title: str, text: str, terms: Sequence[str]) -> int:
    score = 0
    relative_lower = relative_path.lower()
    title_lower = title.lower()
    text_lower = text.lower()
    for term in terms:
        if term in title_lower:
            score += 8
        if term in relative_lower:
            score += 5
        if term in text_lower:
            score += 1
    return score


def _snippet(text: str, query: str, terms: Sequence[str], *, radius: int = 90) -> str:
    cleaned = _squash_whitespace(text)
    lowered = cleaned.lower()
    needle = query.lower()
    index = lowered.find(needle)
    if index < 0:
        index = min((lowered.find(term) for term in terms if lowered.find(term) >= 0), default=0)

    start = max(0, index - radius)
    end = min(len(cleaned), index + len(needle) + radius)
    snippet = cleaned[start:end].strip()
    if start:
        snippet = "..." + snippet
    if end < len(cleaned):
        snippet += "..."
    return snippet


def _first_matching_line(raw: str, terms: Sequence[str]) -> int | None:
    for index, line in enumerate(raw.splitlines(), start=1):
        if _matches_terms(line, terms):
            return index
    for index, line in enumerate(raw.splitlines(), start=1):
        if any(term in line.lower() for term in terms):
            return index
    return None


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _squash_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _without_score(result: dict[str, object]) -> dict[str, object]:
    clean = dict(result)
    clean.pop("score", None)
    return clean


def _search_docs_cached(
    query: str,
    doc_roots: Sequence[str | Path],
    *,
    cache_root: str | Path,
    limit: int = 10,
    rebuild: bool = False,
) -> list[dict[str, object]]:
    """Search documentation roots using a persistent SQLite cache."""
    terms = _query_terms(query)
    if not terms or limit <= 0:
        return []

    results: list[dict[str, object]] = []
    for raw_root in doc_roots:
        root = Path(raw_root).expanduser().resolve()
        if not root.is_dir():
            continue
        index_dir = _index_dir_for_root(Path(cache_root).expanduser(), root)
        db_path = index_dir / "index.sqlite"
        manifest_path = index_dir / "manifest.json"
        if rebuild or not _index_is_usable(db_path, manifest_path, root):
            _build_index(root, db_path, manifest_path)
        results.extend(_search_index(db_path, query, terms))

    return _finalize_index_results(results, limit)


def search_remote_docs(
    runner,
    query: str,
    doc_roots: Sequence[str],
    *,
    cache_root: str | Path,
    limit: int = 10,
    rebuild: bool = False,
) -> list[dict[str, object]]:
    """Search remote documentation roots using a local SQLite cache."""
    terms = _query_terms(query)
    if not terms or limit <= 0:
        return []

    results: list[dict[str, object]] = []
    for raw_root in doc_roots:
        root_text = raw_root.rstrip("/")
        if not root_text:
            continue
        root = Path(root_text)
        index_dir = _index_dir_for_root(Path(cache_root).expanduser(), root)
        db_path = index_dir / "index.sqlite"
        manifest_path = index_dir / "manifest.json"
        if rebuild or not _index_is_usable(db_path, manifest_path, root):
            _build_remote_index(runner, root_text, db_path, manifest_path)
        results.extend(_search_index(db_path, query, terms))

    return _finalize_index_results(results, limit)


def _index_dir_for_root(cache_root: Path, doc_root: Path) -> Path:
    root_text = doc_root.as_posix()
    digest = hashlib.sha1(root_text.encode("utf-8")).hexdigest()[:12]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", root_text.strip("/")).strip("_")
    return cache_root / f"{safe or 'root'}-{digest}"


def _index_is_usable(db_path: Path, manifest_path: Path, doc_root: Path) -> bool:
    if not db_path.is_file() or not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if manifest.get("schema_version") != SCHEMA_VERSION:
        return False
    return manifest.get("doc_root") == doc_root.as_posix()


def _build_index(doc_root: Path, db_path: Path, manifest_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_suffix(".sqlite.tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    con = sqlite3.connect(tmp_path)
    try:
        _create_schema(con)
        doc_count = 0
        topic_count = 0
        for root, path in iter_doc_files([doc_root]):
            suffix = path.suffix.lower()
            if suffix == ".tgf":
                if _should_index_tgf(root, path):
                    topic_count += _index_tgf(con, root, path)
                continue
            if suffix in CONTENT_SUFFIXES:
                if _index_document(con, root, path):
                    doc_count += 1
        con.commit()
    finally:
        con.close()

    tmp_path.replace(db_path)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "doc_root": doc_root.as_posix(),
                "built_at": time.time(),
                "documents": doc_count,
                "topics": topic_count,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _build_remote_index(runner, doc_root: str, db_path: Path, manifest_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    records_path = db_path.parent / "remote_records.jsonl.gz"
    remote_records_path, remote_counts = _download_remote_records(runner, doc_root, records_path)

    tmp_path = db_path.with_suffix(".sqlite.tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    con = sqlite3.connect(tmp_path)
    doc_count = 0
    topic_count = 0
    try:
        _create_schema(con)
        with gzip.open(records_path, "rt", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("kind") == "document":
                    _insert_document_record(con, record)
                    doc_count += 1
                elif record.get("kind") == "topic":
                    _insert_topic_record(con, record)
                    topic_count += 1
        con.commit()
    finally:
        con.close()
        try:
            records_path.unlink()
        except OSError:
            pass

    tmp_path.replace(db_path)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "doc_root": doc_root,
                "built_at": time.time(),
                "documents": doc_count or int(remote_counts.get("documents") or 0),
                "topics": topic_count or int(remote_counts.get("topics") or 0),
                "remote_records_path": remote_records_path,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _download_remote_records(runner, doc_root: str, local_records_path: Path) -> tuple[str, dict[str, object]]:
    result = runner.run_command(_remote_doc_index_command(doc_root), timeout=900)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "remote doc index generation failed")

    summary = _parse_remote_index_summary(result.stdout)
    remote_records_path = str(summary.get("path") or "")
    if not remote_records_path:
        raise RuntimeError("remote doc index did not report an output path")

    try:
        download = runner.download(remote_records_path, local_records_path, recursive=False, timeout=300)
        if download.returncode != 0:
            raise RuntimeError(download.stderr.strip() or f"failed to download {remote_records_path}")
    finally:
        runner.run_command(f"rm -f {shlex.quote(remote_records_path)}", timeout=30)
    return remote_records_path, summary


def _parse_remote_index_summary(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise RuntimeError("remote doc index did not emit a JSON summary")


def _insert_document_record(con: sqlite3.Connection, record: dict[str, object]) -> None:
    path = str(record.get("path") or "")
    relative_path = str(record.get("relative_path") or path)
    suffix = str(record.get("suffix") or Path(path).suffix.lower())
    title = str(record.get("title") or Path(relative_path).stem)
    text = str(record.get("text") or "")
    search_text = _squash_whitespace(f"{relative_path} {title} {text}").lower()
    con.execute(
        """
        INSERT OR REPLACE INTO documents
        (path, relative_path, suffix, title, text, search_text)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (path, relative_path, suffix, title, text, search_text),
    )


def _insert_topic_record(con: sqlite3.Connection, record: dict[str, object]) -> None:
    path = str(record.get("path") or "")
    relative_path = str(record.get("relative_path") or path)
    line = record.get("line")
    topic_id = str(record.get("topic_id") or "")
    anchor = str(record.get("anchor") or "")
    target_path = str(record.get("target_path") or "")
    target_relative_path = str(record.get("target_relative_path") or target_path)
    title = str(record.get("title") or anchor or Path(target_relative_path).stem)
    text = str(record.get("text") or f"{topic_id} {anchor} {target_relative_path}")
    con.execute(
        """
        INSERT INTO topics
        (path, relative_path, line, topic_id, anchor, target_path,
         target_relative_path, title, text, search_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            path,
            relative_path,
            line if isinstance(line, int) else None,
            topic_id,
            anchor,
            target_path,
            target_relative_path,
            title,
            text,
            text.lower(),
        ),
    )


def _create_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE documents (
            path TEXT PRIMARY KEY,
            relative_path TEXT NOT NULL,
            suffix TEXT NOT NULL,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            search_text TEXT NOT NULL
        );
        CREATE TABLE topics (
            path TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            line INTEGER,
            topic_id TEXT NOT NULL,
            anchor TEXT NOT NULL,
            target_path TEXT NOT NULL,
            target_relative_path TEXT NOT NULL,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            search_text TEXT NOT NULL
        );
        CREATE INDEX idx_documents_search ON documents(search_text);
        CREATE INDEX idx_topics_search ON topics(search_text);
        """
    )


def _index_document(con: sqlite3.Connection, root: Path, path: Path) -> bool:
    try:
        raw = _read_preview_text(path)
    except OSError:
        return False
    title, text = _extract_document_text(path, raw)
    relative_path = _relative_path_fast(path, root)
    title = title or path.stem
    search_text = _squash_whitespace(f"{relative_path} {title} {text}").lower()
    con.execute(
        """
        INSERT OR REPLACE INTO documents
        (path, relative_path, suffix, title, text, search_text)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (str(path), relative_path, path.suffix.lower(), title, text, search_text),
    )
    return True


def _index_tgf(con: sqlite3.Connection, root: Path, path: Path) -> int:
    try:
        raw = _read_text(path)
    except OSError:
        return 0

    count = 0
    for line_no, line in enumerate(raw.splitlines(), start=1):
        entry = _parse_tgf_line_fast(line, tgf_path=path, doc_root=root, line_no=line_no)
        if entry is None:
            continue
        source_relative = _relative_path_fast(entry.source_path, root)
        target_relative = _relative_path_fast(entry.target_path, root)
        title = entry.anchor or Path(target_relative).stem
        text = f"{entry.topic_id} {entry.anchor} {target_relative}"
        con.execute(
            """
            INSERT INTO topics
            (path, relative_path, line, topic_id, anchor, target_path,
             target_relative_path, title, text, search_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(entry.source_path),
                source_relative,
                entry.line,
                entry.topic_id,
                entry.anchor,
                str(entry.target_path),
                target_relative,
                title,
                text,
                text.lower(),
            ),
        )
        count += 1
    return count


def _read_preview_text(path: Path, max_bytes: int = DOCUMENT_PREVIEW_BYTES) -> str:
    data = path.read_bytes()[:max_bytes]
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _should_index_tgf(root: Path, path: Path) -> bool:
    return _relative_path_fast(path, root) == "api_more_info/api_more_info.tgf"


def _parse_tgf_line_fast(
    line: str,
    *,
    tgf_path: Path,
    doc_root: Path,
    line_no: int,
):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if '"' in stripped or "'" in stripped:
        return parse_tgf_line(stripped, tgf_path=tgf_path, doc_root=doc_root, line_no=line_no)

    parts = stripped.split()
    if len(parts) < 4 or parts[-1].upper() != "HTML":
        return None

    topic_id, target_ref, anchor = parts[0], parts[1], parts[2]
    return TgfEntry(
        topic_id=topic_id,
        target_path=_resolve_tgf_target_fast(target_ref, tgf_path=tgf_path, doc_root=doc_root),
        anchor=anchor,
        source_path=tgf_path,
        line=line_no,
    )


def _resolve_tgf_target_fast(target_ref: str, *, tgf_path: Path, doc_root: Path) -> Path:
    variable_match = re.match(r"^\$([^/\\]+)[/\\]?(.*)$", target_ref)
    if variable_match:
        doc_dir, rest = variable_match.groups()
        return doc_root / doc_dir / rest
    target = Path(target_ref)
    if target.is_absolute():
        return target
    return tgf_path.parent / target


def _relative_path_fast(path: Path, root: Path) -> str:
    path_text = path.as_posix()
    root_text = root.as_posix().rstrip("/")
    if path_text.startswith(root_text + "/"):
        return path_text[len(root_text) + 1 :]
    return path_text


def _search_index(db_path: Path, query: str, terms: Sequence[str]) -> list[dict[str, object]]:
    where = " AND ".join("search_text LIKE ? ESCAPE '\\'" for _ in terms)
    params = [f"%{_escape_like(term)}%" for term in terms]
    con = sqlite3.connect(db_path)
    try:
        docs = [
            _document_row_to_result(row, query, terms)
            for row in con.execute(
                f"SELECT path, relative_path, title, text FROM documents WHERE {where}",
                params,
            )
        ]
        topics = [
            _topic_row_to_result(row, query, terms)
            for row in con.execute(
                (
                    "SELECT path, relative_path, line, topic_id, anchor, target_path, "
                    f"target_relative_path, title, text FROM topics WHERE {where}"
                ),
                params,
            )
        ]
    finally:
        con.close()
    return docs + topics


def _document_row_to_result(row: tuple, query: str, terms: Sequence[str]) -> dict[str, object]:
    path, relative_path, title, text = row
    return {
        "kind": "document",
        "path": path,
        "relative_path": relative_path,
        "title": title,
        "line": None,
        "snippet": _snippet(text, query, terms),
        "score": _score_index_result(relative_path, title, text, query, terms),
    }


def _topic_row_to_result(row: tuple, query: str, terms: Sequence[str]) -> dict[str, object]:
    path, relative_path, line, topic_id, anchor, target_path, target_relative_path, title, text = row
    return {
        "kind": "topic",
        "path": path,
        "relative_path": relative_path,
        "line": line,
        "topic_id": topic_id,
        "anchor": anchor,
        "target_path": target_path,
        "target_relative_path": target_relative_path,
        "title": title,
        "snippet": _snippet(text, query, terms),
        "score": _score_index_result(target_relative_path, title, text, query, terms) + 2,
    }


def _score_index_result(
    relative_path: str,
    title: str,
    text: str,
    query: str,
    terms: Sequence[str],
) -> int:
    score = _score_match(relative_path, title, text, terms)
    query_lower = query.lower().strip()
    title_lower = title.lower()
    stem_lower = Path(relative_path).stem.lower()

    if title_lower == query_lower:
        score += 120
    if stem_lower == query_lower:
        score += 90

    compact_query = _compact_alnum(query)
    compact_title = _compact_alnum(title)
    compact_stem = _compact_alnum(Path(relative_path).stem)
    if compact_query and compact_query in compact_title:
        score += 55
    if compact_query and compact_query in compact_stem:
        score += 40

    if _looks_like_identifier(title) and all(term in title_lower for term in terms):
        score += 30
    if re.search(r"/(?:sk|mae).*ref/", f"/{relative_path.lower()}"):
        score += 10
    return score


def _finalize_index_results(results: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
    results.sort(key=lambda item: (-int(item["score"]), str(item.get("relative_path") or item.get("target_relative_path"))))
    final: list[dict[str, object]] = []
    location_indexes: dict[str, int] = {}
    for item in results:
        location = str(item.get("target_relative_path") or item.get("relative_path") or item.get("path"))
        if location in location_indexes:
            existing = final[location_indexes[location]]
            if existing.get("kind") != "document" and item.get("kind") == "document":
                clean = dict(item)
                clean.pop("score", None)
                final[location_indexes[location]] = clean
            continue
        clean = dict(item)
        clean.pop("score", None)
        location_indexes[location] = len(final)
        final.append(clean)
        if len(final) >= limit:
            break
    return final


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _compact_alnum(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _looks_like_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.$:-]*", value)) and (
        any(ch.islower() for ch in value) and any(ch.isupper() for ch in value)
    )


def _remote_doc_index_command(doc_root: str) -> str:
    return (
        "# vb_doc_index\n"
        "set -e\n"
        f"vb_doc_root={shlex.quote(doc_root)}\n"
        "vb_install_root=${vb_doc_root%/doc}\n"
        "vb_py=\n"
        "for vb_candidate in \\\n"
        "  \"$CDSHOME/tools.lnx86/python/64bit/bin/python3\" \\\n"
        "  \"$vb_install_root/tools.lnx86/python/64bit/bin/python3\" \\\n"
        "  \"$(command -v python3 2>/dev/null || true)\" \\\n"
        "  \"$(command -v python 2>/dev/null || true)\"\n"
        "do\n"
        "  if [ -n \"$vb_candidate\" ] && [ -x \"$vb_candidate\" ] && \"$vb_candidate\" - <<'PYPROBE' >/dev/null 2>&1\n"
        "import gzip\n"
        "import json\n"
        "PYPROBE\n"
        "  then\n"
        "    vb_py=\"$vb_candidate\"\n"
        "    break\n"
        "  fi\n"
        "done\n"
        "if [ -z \"$vb_py\" ]; then\n"
        "  echo 'vb_doc_index: usable python not found' >&2\n"
        "  exit 127\n"
        "fi\n"
        f"\"$vb_py\" - \"$vb_doc_root\" {DOCUMENT_PREVIEW_BYTES} <<'PY'\n"
        f"{_REMOTE_DOC_INDEX_SCRIPT}\n"
        "PY\n"
    )


_REMOTE_DOC_INDEX_SCRIPT = r'''
from __future__ import print_function

import gzip
import json
import os
import re
import sys
import tempfile

try:
    from html import unescape
except ImportError:
    from HTMLParser import HTMLParser
    unescape = HTMLParser().unescape

ROOT = sys.argv[1].rstrip("/")
PREVIEW_BYTES = int(sys.argv[2])
SEARCH_SUFFIXES = set([".html", ".htm", ".txt", ".xml", ".json", ".tgf"])
CONTENT_SUFFIXES = SEARCH_SUFFIXES - set([".tgf"])
CANONICAL_TGF = "api_more_info/api_more_info.tgf"


def squash(text):
    return re.sub(r"\s+", " ", text or "").strip()


def relpath(path):
    root = ROOT.rstrip("/")
    if path.startswith(root + "/"):
        return path[len(root) + 1:]
    return os.path.relpath(path, root)


def read_preview(path):
    with open(path, "rb") as fh:
        data = fh.read(PREVIEW_BYTES)
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", "replace")


def extract_text(path, raw):
    suffix = os.path.splitext(path)[1].lower()
    if suffix not in (".html", ".htm"):
        return os.path.splitext(os.path.basename(path))[0], squash(unescape(raw))

    without_noise = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", without_noise)
    title = squash(unescape(re.sub(r"(?is)<[^>]+>", " ", title_match.group(1)))) if title_match else ""
    text = squash(unescape(re.sub(r"(?is)<[^>]+>", " ", without_noise)))
    return title, text


def resolve_tgf_target(target_ref, tgf_path):
    match = re.match(r"^\$([^/\\]+)[/\\]?(.*)$", target_ref)
    if match:
        doc_dir, rest = match.groups()
        return os.path.join(ROOT, doc_dir, rest)
    if os.path.isabs(target_ref):
        return target_ref
    return os.path.join(os.path.dirname(tgf_path), target_ref)


def iter_tgf_records(path):
    with open(path, "rb") as fh:
        raw = fh.read().decode("utf-8", "replace")
    for line_no, line in enumerate(raw.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 4 or parts[-1].upper() != "HTML":
            continue
        topic_id, target_ref, anchor = parts[0], parts[1], parts[2]
        target_path = resolve_tgf_target(target_ref, path)
        target_relative = relpath(target_path)
        text = "%s %s %s" % (topic_id, anchor, target_relative)
        yield {
            "kind": "topic",
            "path": path,
            "relative_path": relpath(path),
            "line": line_no,
            "topic_id": topic_id,
            "anchor": anchor,
            "target_path": target_path,
            "target_relative_path": target_relative,
            "title": anchor or os.path.splitext(os.path.basename(target_relative))[0],
            "text": text,
        }


def emit(out, record):
    out.write((json.dumps(record, ensure_ascii=True) + "\n").encode("ascii"))


fd, out_path = tempfile.mkstemp(prefix="vb_doc_index_", suffix=".jsonl.gz")
os.close(fd)
documents = 0
topics = 0

with gzip.open(out_path, "wb") as out:
    for dirpath, _dirnames, filenames in os.walk(ROOT):
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            suffix = os.path.splitext(filename)[1].lower()
            if suffix not in SEARCH_SUFFIXES:
                continue
            relative = relpath(path)
            if suffix == ".tgf":
                if relative == CANONICAL_TGF:
                    for record in iter_tgf_records(path):
                        emit(out, record)
                        topics += 1
                continue
            if suffix not in CONTENT_SUFFIXES:
                continue
            try:
                raw = read_preview(path)
                title, text = extract_text(path, raw)
            except Exception:
                continue
            emit(out, {
                "kind": "document",
                "path": path,
                "relative_path": relative,
                "suffix": suffix,
                "title": title or os.path.splitext(filename)[0],
                "text": text,
            })
            documents += 1

print(json.dumps({"path": out_path, "documents": documents, "topics": topics}))
'''
