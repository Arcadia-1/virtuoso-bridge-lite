"""Read-only dialog inspection and opt-in shared-CIW request protection."""

from __future__ import annotations

import math
import re
import time
from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult

if TYPE_CHECKING:
    from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient


class DialogTarget(BaseModel):
    model_config = ConfigDict(frozen=True)

    pid: int = Field(strict=True, gt=0)
    display: str | None = None
    ciw_window: str | None = None

    @field_validator("display", "ciw_window")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is not None and (
            not value or value != value.strip() or any(c in value for c in "\x00\r\n")
        ):
            raise ValueError("window/display must be nonempty and contain no control characters")
        return value

    @field_validator("ciw_window")
    @classmethod
    def validate_window(cls, value: str | None) -> str | None:
        if value is not None and (not re.fullmatch(r"(?:0x[0-9a-fA-F]+|[0-9]+)", value)
                                  or int(value, 16 if value.startswith("0x") else 10) <= 0):
            raise ValueError("ciw_window must be a positive X11 window id")
        return value

    @field_validator("display")
    @classmethod
    def validate_display(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_./\[\]:-]*:[0-9]+(?:\.[0-9]+)?", value):
            raise ValueError("display must be an X11 DISPLAY name")
        return value


class DialogInspection(BaseModel):
    """A candidate blocker is evidence of interference, not proof of origin."""

    model_config = ConfigDict(frozen=True)

    status: Literal["clear", "blocked", "indeterminate"]
    target: DialogTarget
    dialogs: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class DialogBlockedError(RuntimeError):
    """High-level API failure retaining request outcome and dialog evidence."""

    def __init__(self, result: VirtuosoResult) -> None:
        super().__init__("; ".join(result.errors))
        self.result = result
        self.inspection = result.metadata.get("dialog_guard")
        self.outcome = result.metadata.get("outcome", "unknown")


def parse_inspection(payload: Any, target: DialogTarget) -> DialogInspection:
    report = DialogInspection.model_validate(payload)
    if report.target.pid != target.pid or (
        target.display is not None and report.target.display != target.display
    ) or (
        target.ciw_window is not None and report.target.ciw_window != target.ciw_window
    ):
        raise ValueError("inspection returned a different target")
    if report.status != "indeterminate" and (
        not report.target.display or not report.target.ciw_window
        or (report.status == "clear" and report.dialogs)
        or (report.status == "blocked" and not report.dialogs)
    ):
        raise ValueError("inspection status is inconsistent with its CIW/display/dialog evidence")
    for dialog in report.dialogs:
        dialog["source"] = "unknown"
        dialog["suggested_action"] = None
    return report


def _timeout(value: float) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be positive and finite")
    return value


class DialogOps:
    """The guard observes only. It never dismisses dialogs or retries SKILL."""

    def __init__(self, owner: VirtuosoClient) -> None:
        self._owner = owner
        self._target: DialogTarget | None = None
        self._endpoint: tuple[str, int] | None = None
        self._protect_inflight = False

    @property
    def enabled(self) -> bool:
        return self._target is not None

    @property
    def protect_inflight(self) -> bool:
        return self.enabled and self._protect_inflight

    @property
    def target(self) -> DialogTarget | None:
        return self._target

    def inspect(
        self, *, pid: int | None = None, display: str | None = None,
        ciw_window: str | None = None, timeout: float = 15,
    ) -> DialogInspection:
        """Inspect one explicit process, or the previously bound guard target.

        Uses the GUI host via SSH/X11; sends no SKILL request. It does not
        start/restart a bridge. Missing X11 ownership evidence is indeterminate.
        """
        from virtuoso_bridge.virtuoso import x11
        from virtuoso_bridge.transport.remote_paths import resolve_remote_username

        _timeout(timeout)
        if pid is None:
            if self._target is None:
                raise ValueError("specify pid or enable the dialog guard first")
            target = self._target
            if display is not None or ciw_window is not None:
                target = target.model_copy(update={
                    "display": display if display is not None else target.display,
                    "ciw_window": ciw_window if ciw_window is not None else target.ciw_window,
                })
                target = DialogTarget.model_validate(target.model_dump())
        else:
            target = DialogTarget(pid=pid, display=display, ciw_window=ciw_window)
        profile = getattr(self._owner._tunnel, "_profile", None)
        try:
            runner = self._owner.gui_runner
            payload = x11.inspect_dialogs(
                runner, resolve_remote_username(configured_user=getattr(runner, "user", None)),
                pid=target.pid, display=target.display, ciw_window=target.ciw_window,
                profile=profile, timeout=timeout,
            )
            return parse_inspection(payload, target)
        except Exception as exc:
            return DialogInspection(status="indeterminate", target=target,
                                    diagnostics=[f"X11 inspection failed: {exc}"])

    def enable_guard(
        self, *, pid: int | None = None, display: str | None = None,
        ciw_window: str | None = None, timeout: float = 15, local_gui: bool = False,
        protect_inflight: bool = False,
    ) -> DialogInspection:
        """Bind to the authenticated daemon's CIW PID without executing SKILL.

        A blocked initial inspection still enables protection. Authentication,
        missing PID and split-host failures preserve any previous binding.
        ``protect_inflight=True`` additionally requires recoverable-request
        support, with no legacy watchdog fallback; it is off by default.
        """
        _timeout(timeout)
        if not isinstance(local_gui, bool):
            raise ValueError("local_gui must be an explicit boolean")
        if not isinstance(protect_inflight, bool):
            raise ValueError("protect_inflight must be an explicit boolean")
        if pid is not None:
            DialogTarget(pid=pid, display=display, ciw_window=ciw_window)
        tunnel = self._owner._tunnel
        if tunnel is not None and getattr(tunnel, "gui_host", None) != getattr(tunnel, "daemon_host", None):
            raise ValueError("shared guard requires GUI and daemon on the same host; inspect an explicit GUI PID instead")
        if self._owner.gui_runner is None and not local_gui:
            raise ValueError("No GUI SSH transport: pass local_gui=True only for a genuinely local GUI, not an SSH-forwarded loopback endpoint")
        deadline = time.monotonic() + timeout
        caps = self._owner._ensure_daemon_capabilities(deadline, refresh=True)
        actual_pid = caps.get("virtuoso_pid")
        if caps.get("auth") != "on" or self._owner._daemon_token is None:
            raise ValueError("shared guard requires authenticated daemon identity")
        if not isinstance(actual_pid, int) or isinstance(actual_pid, bool) or actual_pid <= 0:
            raise ValueError("daemon has no verified Virtuoso PID")
        if pid is not None and pid != actual_pid:
            raise ValueError("selected PID does not match the connected daemon")
        if protect_inflight:
            self._owner.requests._check_capabilities(caps)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("dialog guard binding timed out")
        report = self.inspect(pid=actual_pid, display=display,
                              ciw_window=ciw_window, timeout=remaining)
        # Retain only resolved identity; a subsequent preflight must revalidate it.
        self._target = report.target
        self._endpoint = (self._owner.host, self._owner.port)
        self._protect_inflight = protect_inflight
        return report

    def disable_guard(self) -> None:
        """Disable opt-in protection; does not dismiss or recover anything."""
        self._target = None
        self._endpoint = None
        self._protect_inflight = False

    def preflight(self, timeout: float) -> VirtuosoResult | None:
        if not self.enabled:
            return None
        if self._endpoint != (self._owner.host, self._owner.port):
            report = DialogInspection(
                status="indeterminate", target=self._target,
                diagnostics=["Bridge endpoint changed; explicitly rebind the guard before sending SKILL."],
            )
        else:
            deadline = time.monotonic() + timeout
            report = self.inspect(timeout=timeout)
            if report.status == "clear":
                try:
                    caps = self._owner._ensure_daemon_capabilities(deadline, refresh=True)
                    if caps.get("auth") != "on" or caps.get("virtuoso_pid") != self._target.pid:
                        raise ValueError("daemon identity no longer matches the guarded CIW")
                except Exception as exc:
                    report = DialogInspection(
                        status="indeterminate", target=self._target,
                        diagnostics=[f"Cannot verify guarded endpoint: {exc}"],
                    )
        if report.status == "clear":
            return None
        return VirtuosoResult(
            status=ExecutionStatus.ERROR,
            errors=["Shared CIW request not sent: dialog inspection is " + report.status],
            metadata={"dialog_guard": report.model_dump(), "request_sent": False,
                      "outcome": "not_started"},
        )

    def annotate_failure(self, result: VirtuosoResult, timeout: float) -> VirtuosoResult:
        """Inspect out-of-band once; never replay an uncertain operation."""
        if not self.enabled or result.ok:
            return result
        if timeout > 0:
            report = self.inspect(timeout=timeout)
        else:
            report = DialogInspection(
                status="indeterminate", target=self._target,
                diagnostics=["Request budget exhausted; use dialogs.inspect() for out-of-band diagnosis."],
            )
        result.metadata["dialog_guard"] = report.model_dump()
        result.metadata["outcome"] = "unknown"
        result.warnings.append(
            "A failed in-flight request may have executed. Resolve the dialog and "
            "verify operation state before retrying; this guard does not change the daemon watchdog."
        )
        return result
