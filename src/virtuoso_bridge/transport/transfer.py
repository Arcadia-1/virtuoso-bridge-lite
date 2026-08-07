"""Transport-independent tar transfer plans and staged installation."""

from __future__ import annotations

import base64
import logging
import os
import posixpath
import shlex
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TarUploadPlan:
    """One local-tar to remote-tar upload operation."""

    local_command: tuple[str, ...]
    remote_command: str
    remote_dir: str
    file_count: int


@dataclass(frozen=True)
class TextUploadPlan:
    """One atomic UTF-8 text upload operation."""

    remote_command: str
    remote_path: str
    remote_dir: str
    work_path: str

    def persistent_command(self, text: str) -> str:
        """Build the equivalent command for an existing remote shell."""
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        writer = f"printf %s {shlex.quote(encoded)} | base64 -d > \"$payload\""
        return _text_upload_script(self, writer)


@dataclass(frozen=True)
class TarDownloadPlan:
    """One remote-tar to staged local-tar download operation."""

    remote_command: str
    local_command: tuple[str, ...]
    remote_path: str
    local_path: Path
    stage_path: Path
    staged_item: Path


def _atomic_tar_upload_command(
    remote_dir: str,
    entries: list[tuple[Path, str]],
    *,
    platform_name: str,
) -> str:
    token = uuid.uuid4().hex
    work_path = posixpath.join(remote_dir, f".vbtmp-{token}")
    remote_dir_q = shlex.quote(remote_dir)
    work_path_q = shlex.quote(work_path)
    lines = [
        "set -eu",
        f"work={work_path_q}",
        'payload="$work/payload"',
        'state="$work/state"',
        "created=0",
        "installed=0",
        "cleanup() {",
        "  rc=$?",
        "  trap - EXIT HUP INT TERM",
        '  if [ "$installed" -ne 1 ]; then',
    ]
    for index, (_local_path, remote_path) in enumerate(entries):
        target_q = shlex.quote(remote_path)
        lines.extend(
            [
                f'    if [ -e "$state/installed-{index}" ]; then '
                f"rm -rf -- {target_q} || :; fi",
                f'    if [ -e "$state/backup-{index}" ] || '
                f'[ -L "$state/backup-{index}" ]; then '
                f'mv -- "$state/backup-{index}" {target_q} || :; fi',
            ]
        )
    lines.extend(
        [
            "  fi",
            '  if [ "$created" -eq 1 ]; then rm -rf -- "$work" || :; fi',
            '  exit "$rc"',
            "}",
            "trap cleanup EXIT",
            "trap 'exit 129' HUP",
            "trap 'exit 130' INT",
            "trap 'exit 143' TERM",
            f"mkdir -p {remote_dir_q}",
            'mkdir -- "$work"',
            "created=1",
            'mkdir -- "$payload" "$state"',
            'tar xf - -C "$payload"',
        ]
    )
    if platform_name == "nt":
        for local_path, _remote_path in entries:
            if local_path.is_dir():
                lines.append(
                    f"find \"$payload\"/{shlex.quote(local_path.name)} "
                    "-type d -exec chmod u+w {} +"
                )
    for index, (local_path, remote_path) in enumerate(entries):
        target_q = shlex.quote(remote_path)
        lines.extend(
            [
                f"if [ -e {target_q} ] || [ -L {target_q} ]; then "
                f'mv -- {target_q} "$state/backup-{index}"; fi',
                f': > "$state/installed-{index}"',
                f"mv -- \"$payload\"/{shlex.quote(local_path.name)} {target_q}",
            ]
        )
    lines.extend(
        [
            "installed=1",
            'rm -rf -- "$work"',
            "created=0",
            "trap - EXIT HUP INT TERM",
        ]
    )
    return "sh -c " + shlex.quote("\n".join(lines))


def _text_upload_script(plan: TextUploadPlan, writer: str) -> str:
    remote_dir_q = shlex.quote(plan.remote_dir)
    remote_path_q = shlex.quote(plan.remote_path)
    work_path_q = shlex.quote(plan.work_path)
    lines = [
        "(",
        "set -eu",
        f"work={work_path_q}",
        'payload="$work/payload"',
        "created=0",
        "cleanup() {",
        "  rc=$?",
        "  trap - EXIT HUP INT TERM",
        '  if [ "$created" -eq 1 ]; then rm -rf -- "$work" || :; fi',
        '  exit "$rc"',
        "}",
        "trap cleanup EXIT",
        "trap 'exit 129' HUP",
        "trap 'exit 130' INT",
        "trap 'exit 143' TERM",
        f"mkdir -p {remote_dir_q}",
        'mkdir -- "$work"',
        "created=1",
        writer,
        f'mv -fT -- "$payload" {remote_path_q}',
        'rmdir -- "$work"',
        "created=0",
        "trap - EXIT HUP INT TERM",
        ")",
    ]
    return "\n".join(lines)


def build_text_upload_plan(remote_path: str) -> TextUploadPlan:
    """Build a same-directory staged text upload."""
    normalized_remote = remote_path.replace("\\", "/")
    remote_basename = posixpath.basename(normalized_remote)
    if not remote_basename:
        raise ValueError(f"Invalid remote file path: {remote_path}")
    remote_dir = posixpath.dirname(normalized_remote) or "."
    work_path = posixpath.join(remote_dir, f".vbtmp-{uuid.uuid4().hex}")
    plan = TextUploadPlan(
        remote_command="",
        remote_path=normalized_remote,
        remote_dir=remote_dir,
        work_path=work_path,
    )
    script = _text_upload_script(plan, 'cat > "$payload"')
    return TextUploadPlan(
        remote_command=f"sh -c {shlex.quote(script)}",
        remote_path=plan.remote_path,
        remote_dir=plan.remote_dir,
        work_path=plan.work_path,
    )


def build_tar_upload_plans(
    tar_command: str,
    files: Iterable[tuple[Path, str]],
    *,
    platform_name: str | None = None,
) -> tuple[TarUploadPlan, ...]:
    """Build collision-free tar uploads with option-safe member names."""
    local_platform = os.name if platform_name is None else platform_name
    grouped: dict[tuple[str, Path], list[tuple[Path, str]]] = {}
    remote_targets: set[str] = set()
    for local_path, remote_path in files:
        normalized_remote = remote_path.replace("\\", "/")
        if normalized_remote in remote_targets:
            raise ValueError(f"Duplicate remote upload target: {remote_path}")
        remote_targets.add(normalized_remote)
        remote_dir = posixpath.dirname(normalized_remote) or "."
        local_parent = local_path.absolute().parent
        grouped.setdefault((remote_dir, local_parent), []).append(
            (local_path, normalized_remote)
        )

    plans: list[TarUploadPlan] = []
    upload_groups: list[tuple[str, Path, list[tuple[Path, str]]]] = []
    for (remote_dir, local_parent), entries in grouped.items():
        partitions: list[list[tuple[Path, str]]] = []
        partition_names: list[set[str]] = []
        for entry in entries:
            local_name = entry[0].name
            for partition, names in zip(partitions, partition_names):
                if local_name not in names:
                    partition.append(entry)
                    names.add(local_name)
                    break
            else:
                partitions.append([entry])
                partition_names.append({local_name})
        upload_groups.extend(
            (remote_dir, local_parent, partition) for partition in partitions
        )

    for remote_dir, local_parent, entries in upload_groups:
        local_command = [
            tar_command,
            "cf",
            "-",
            "-C",
            str(local_parent).replace("\\", "/"),
            "--",
        ]
        local_command.extend(local_path.name for local_path, _ in entries)
        plans.append(
            TarUploadPlan(
                local_command=tuple(local_command),
                remote_command=_atomic_tar_upload_command(
                    remote_dir,
                    entries,
                    platform_name=local_platform,
                ),
                remote_dir=remote_dir,
                file_count=len(entries),
            )
        )
    return tuple(plans)


def build_tar_download_plan(
    tar_command: str,
    remote_path: str,
    local_path: Path,
) -> TarDownloadPlan:
    """Build a recursive download that extracts beside the final target."""
    normalized_remote = remote_path.replace("\\", "/").rstrip("/")
    remote_basename = posixpath.basename(normalized_remote)
    if not remote_basename:
        raise ValueError(f"Invalid remote directory path: {remote_path}")

    quoted_remote = shlex.quote(normalized_remote)
    inner_command = (
        f"p={quoted_remote}; "
        'd=$(dirname "$p"); b=$(basename "$p"); '
        'cd "$d" && tar czf - -- "$b"'
    )
    stage_path = local_path.parent / f".vbtmp-{uuid.uuid4().hex}"
    return TarDownloadPlan(
        remote_command=f"sh -c {shlex.quote(inner_command)}",
        local_command=(tar_command, "xzf", "-"),
        remote_path=remote_path,
        local_path=local_path,
        stage_path=stage_path,
        staged_item=stage_path / remote_basename,
    )


def install_staged_path(plan: TarDownloadPlan) -> None:
    """Atomically replace the requested target with a completed download."""
    install_staged_item(plan.stage_path, plan.staged_item, plan.local_path)


def install_staged_item(
    stage_path: Path,
    staged_item: Path,
    local_path: Path,
) -> None:
    """Atomically install one staged item, then retire the previous target."""
    backup_path: Path | None = None
    try:
        if local_path.exists() or local_path.is_symlink():
            backup_path = local_path.parent / f".vbbak-{uuid.uuid4().hex}"
            local_path.rename(backup_path)
        staged_item.rename(local_path)
    except Exception:
        discard_stage(stage_path)
        if (
            backup_path is not None
            and not (local_path.exists() or local_path.is_symlink())
            and (backup_path.exists() or backup_path.is_symlink())
        ):
            backup_path.rename(local_path)
        raise
    else:
        if backup_path is not None and (backup_path.exists() or backup_path.is_symlink()):
            try:
                if backup_path.is_dir() and not backup_path.is_symlink():
                    shutil.rmtree(backup_path)
                else:
                    backup_path.unlink()
            except OSError as exc:
                logger.warning(
                    "Installed %s but could not remove previous content at %s: %s",
                    local_path,
                    backup_path,
                    exc,
                )
        discard_stage(stage_path)


def discard_stage(stage_path: Path) -> None:
    """Remove a transfer staging directory when it exists."""
    if stage_path.exists():
        shutil.rmtree(stage_path, ignore_errors=True)
