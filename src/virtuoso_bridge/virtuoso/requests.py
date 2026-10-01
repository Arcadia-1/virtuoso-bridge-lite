"""Submit-once SKILL execution with independently queryable late receipts."""

from __future__ import annotations

import json
import math
import secrets
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from virtuoso_bridge import daemon_auth
from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.dialogs import DialogBlockedError

if TYPE_CHECKING:
    from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient


class RequestHandle(BaseModel):
    """Serializable identity, containing neither credentials nor SKILL source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(strict=True, pattern=r"^[0-9a-f]{32}$")
    daemon_instance: str = Field(strict=True, pattern=r"^[0-9a-f]{32}$")
    virtuoso_pid: int = Field(strict=True, gt=0)


class RequestRecoveryError(DialogBlockedError):
    """High-level API failure retaining the original request's recovery handle."""

    def __init__(self, result: VirtuosoResult) -> None:
        super().__init__(result)
        self.handle = RequestHandle.model_validate(result.metadata["request_handle"])


class _Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(strict=True)
    daemon_instance: str = Field(strict=True)
    state: Literal["running", "completed", "unknown", "busy", "rejected"]
    response: str | None = Field(default=None, strict=True)
    diagnostic: str | None = Field(default=None, strict=True)


def _budget(timeout: float) -> float:
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    return time.monotonic() + timeout


class RequestOps:
    """No cancel, restart, resubmit or automatic dialog dismissal operations."""

    def __init__(self, owner: VirtuosoClient) -> None:
        self._owner = owner

    @staticmethod
    def _check_capabilities(caps: dict[str, Any]) -> None:
        feature = caps.get("recoverable_requests")
        if type(feature) is not int or feature != 1 or caps.get("auth") != "on":
            raise ValueError(
                "Daemon does not support authenticated recoverable requests. "
                "Upgrade/reload only in an explicitly selected idle CIW; "
                "no legacy execution fallback was attempted."
            )
        RequestHandle(request_id="0" * 32, daemon_instance=caps.get("daemon_instance"),
                      virtuoso_pid=caps.get("virtuoso_pid"))
        server_time = caps.get("server_time_ms")
        if server_time is not None and (type(server_time) is not int or not 0 < server_time < 2**48):
            raise ValueError("Daemon returned invalid authenticated server time")

    def _exchange(
        self, op: str, handle: RequestHandle, deadline: float, skill: str = "",
    ) -> _Receipt:
        token = self._owner._daemon_token
        if token is None:
            raise daemon_auth.DaemonAuthError("Recoverable requests require authentication")
        nonce = secrets.token_hex(16)
        payload: dict[str, Any] = {
            "proto": daemon_auth.PROTOCOL_VERSION, "op": op, "nonce": nonce,
            "request_id": handle.request_id, "daemon_instance": handle.daemon_instance,
        }
        if op == "submit":
            payload["skill"] = skill
        payload["mac"] = daemon_auth.recoverable_mac(
            token, nonce=nonce, op=op, request_id=handle.request_id,
            daemon_instance=handle.daemon_instance, skill=skill,
        )
        raw = self._owner._exchange_payload(payload, deadline, max_response_bytes=64 * 1024 * 1024)
        # Even error replies must authenticate before their state is trusted.
        verified = daemon_auth.verify_response_bytes(raw, token, nonce)
        if verified[:1] != b"\x02":
            raise daemon_auth.DaemonAuthError("No authenticated recoverable receipt returned")
        receipt = _Receipt.model_validate(json.loads(verified[1:]))
        if (receipt.request_id != handle.request_id
                or receipt.daemon_instance != handle.daemon_instance):
            raise daemon_auth.DaemonAuthError("Receipt belongs to a different request/daemon")
        if receipt.state == "completed":
            if not receipt.response or receipt.response[:1] not in ("\x02", "\x15"):
                raise ValueError("Completed receipt has no valid execution response")
        elif receipt.response is not None:
            raise ValueError("Non-completed receipt carries an execution response")
        return receipt

    @staticmethod
    def _unknown(handle: RequestHandle, message: str, *, phase: str,
                 request_sent: bool | None = None) -> VirtuosoResult:
        return VirtuosoResult(
            status=ExecutionStatus.ERROR, errors=[message],
            warnings=["Do not repeat the operation. Query the original request receipt and reconcile design/run state."],
            metadata={"request_handle": handle.model_dump(), "outcome": "unknown",
                      "request_state": "unknown", "phase": phase,
                      "request_sent": request_sent},
        )

    def _result(self, handle: RequestHandle, receipt: _Receipt, *, submission: bool = False) -> VirtuosoResult:
        if receipt.state == "completed":
            result = self._owner._parse_response(receipt.response, 0)
            result.metadata.update(
                request_handle=handle.model_dump(), request_state="completed",
                outcome="completed", request_sent=True,
            )
            return result
        result = self._unknown(
            handle, receipt.diagnostic or f"Original request is {receipt.state}; no operation was repeated.",
            phase="receipt", request_sent=True if receipt.state == "running" else None,
        )
        result.metadata["request_state"] = receipt.state
        if submission and receipt.state in ("busy", "rejected"):
            result.metadata.update(outcome="not_started", request_sent=False)
            result.warnings = []
        return result

    def receipt(self, handle: RequestHandle | dict[str, Any], *, timeout: float = 10) -> VirtuosoResult:
        """Read an original receipt, even while the CIW is blocked.

        No SKILL or X11 action is sent. Restarted daemons, expired receipts and
        lost connections are unknown, never evidence that a retry is safe.
        """
        handle = RequestHandle.model_validate(handle)
        started = time.monotonic()
        deadline = _budget(timeout)
        try:
            caps = self._owner._ensure_daemon_capabilities(deadline, refresh=True)
            self._check_capabilities(caps)
            if (caps["daemon_instance"] != handle.daemon_instance
                    or caps["virtuoso_pid"] != handle.virtuoso_pid):
                raise ValueError("Daemon restarted or endpoint changed; original receipt cannot be recovered here")
            result = self._result(handle, self._exchange("receipt", handle, deadline))
        except Exception as exc:
            result = self._unknown(handle, f"Cannot verify original receipt: {exc}", phase="receipt")
        result.execution_time = time.monotonic() - started
        return result

    def _execute(self, skill: str, deadline: float) -> VirtuosoResult:
        """Called only after the opted-in dialog guard's request preflight."""
        caps = self._owner._daemon_caps or {}
        target = self._owner.dialogs.target
        try:
            self._check_capabilities(caps)
            if not isinstance(skill, str) or not skill.strip():
                raise ValueError("skill must be a nonempty string")
            handle = RequestHandle(
                request_id=f"{caps.get('server_time_ms', int(time.time() * 1000)):012x}" + secrets.token_hex(10),
                daemon_instance=caps["daemon_instance"],
                virtuoso_pid=caps["virtuoso_pid"],
            )
            if target is None or target.pid != handle.virtuoso_pid:
                raise ValueError("Recoverable execution must match the guarded CIW")
        except Exception as exc:
            return VirtuosoResult(status=ExecutionStatus.ERROR, errors=[str(exc)],
                                  metadata={"request_sent": False, "outcome": "not_started"})

        # A lost submit acknowledgement is uncertain, not permission to submit again.
        try:
            receipt = self._exchange("submit", handle, deadline, skill)
        except Exception as exc:
            return self._unknown(handle, f"Submission acknowledgement unavailable: {exc}", phase="submit")
        if receipt.state != "running":
            return self._result(handle, receipt, submission=True)

        next_inspection = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                receipt = self._exchange("receipt", handle, deadline)
            except Exception as exc:
                return self._unknown(handle, f"Original request receipt unavailable: {exc}",
                                     phase="receipt", request_sent=True)
            if receipt.state != "running":
                return self._result(handle, receipt)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if time.monotonic() >= next_inspection:
                report = self._owner.dialogs.inspect(
                    pid=target.pid, display=target.display, ciw_window=target.ciw_window,
                    timeout=min(5.0, remaining),
                )
                next_inspection = time.monotonic() + 5.0
                if report.status != "clear":
                    result = self._result(handle, receipt)
                    result.errors = [
                        "Original request is still pending and dialog inspection is " + report.status
                        + ". Bridge did not dismiss the dialog, interrupt Virtuoso or repeat the request."
                    ]
                    result.metadata.update(
                        dialog_guard=report.model_dump(), phase="awaiting_user",
                        waiting_for_user=report.status == "blocked",
                    )
                    return result
            time.sleep(min(1.0, max(0, deadline - time.monotonic())))
        result = self._unknown(handle, "Client wait budget exhausted; original request remains queryable.",
                               phase="wait_timeout", request_sent=True)
        result.metadata["request_state"] = "running"
        return result
