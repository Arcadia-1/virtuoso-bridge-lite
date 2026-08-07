"""Transport-independent tar transfer plans and staged installation."""

from __future__ import annotations

import os
import posixpath
import shlex
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class TarUploadPlan:
    """One local-tar to remote-tar upload operation."""

    local_command: tuple[str, ...]
    remote_command: str
    remote_dir: str
    file_count: int


@dataclass(frozen=True)
class TarDownloadPlan:
    """One remote-tar to staged local-tar download operation."""

    remote_command: str
    local_command: tuple[str, ...]
    remote_path: str
    local_path: Path
    stage_path: Path
    staged_item: Path


def build_tar_upload_plans(
    tar_command: str,
    files: Iterable[tuple[Path, str]],
    *,
    platform_name: str | None = None,
) -> tuple[TarUploadPlan, ...]:
    """Group uploads by remote directory and build matching tar commands."""
    local_platform = os.name if platform_name is None else platform_name
    by_remote_dir: dict[str, list[tuple[Path, str]]] = {}
    for local_path, remote_path in files:
        normalized_remote = remote_path.replace("\\", "/")
        remote_dir = posixpath.dirname(normalized_remote) or "."
        by_remote_dir.setdefault(remote_dir, []).append(
            (local_path, normalized_remote)
        )

    plans: list[TarUploadPlan] = []
    for remote_dir, entries in by_remote_dir.items():
        remote_dir_q = shlex.quote(remote_dir)
        command_parts = [
            f"mkdir -p {remote_dir_q}",
            f"tar xf - -C {remote_dir_q}",
        ]
        if local_platform == "nt":
            for local_path, _remote_path in entries:
                if local_path.is_dir():
                    extracted_path = (
                        f"{remote_dir_q}/{shlex.quote(local_path.name)}"
                    )
                    command_parts.append(
                        f"find {extracted_path} -type d "
                        "-exec chmod u+w {} +"
                    )

        rename_pairs = []
        for local_path, remote_path in entries:
            remote_basename = posixpath.basename(remote_path)
            if remote_basename and remote_basename != local_path.name:
                rename_pairs.append((local_path.name, remote_path))
        if len(entries) == 1 and rename_pairs:
            extracted, target = rename_pairs[0]
            command_parts.append(
                f"mv {remote_dir_q}/{shlex.quote(extracted)} "
                f"{shlex.quote(target)}"
            )
        elif rename_pairs:
            token = uuid.uuid4().hex[:8]
            for index, (extracted, _target) in enumerate(rename_pairs):
                temporary = f".vbatch-{token}-{index}"
                command_parts.append(
                    f"mv {remote_dir_q}/{shlex.quote(extracted)} "
                    f"{remote_dir_q}/{shlex.quote(temporary)}"
                )
            for index, (_extracted, target) in enumerate(rename_pairs):
                temporary = f".vbatch-{token}-{index}"
                command_parts.append(
                    f"mv {remote_dir_q}/{shlex.quote(temporary)} "
                    f"{shlex.quote(target)}"
                )

        local_command = [tar_command, "cf", "-"]
        for local_path, _remote_path in entries:
            local_command.extend(
                [
                    "-C",
                    str(local_path.absolute().parent).replace("\\", "/"),
                    local_path.name,
                ]
            )
        plans.append(
            TarUploadPlan(
                local_command=tuple(local_command),
                remote_command=" && ".join(command_parts),
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
        'cd "$d" && tar czf - "$b"'
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
    """Atomically install one staged file or directory."""
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
            if backup_path.is_dir() and not backup_path.is_symlink():
                shutil.rmtree(backup_path)
            else:
                backup_path.unlink()
        discard_stage(stage_path)


def discard_stage(stage_path: Path) -> None:
    """Remove a transfer staging directory when it exists."""
    if stage_path.exists():
        shutil.rmtree(stage_path, ignore_errors=True)
