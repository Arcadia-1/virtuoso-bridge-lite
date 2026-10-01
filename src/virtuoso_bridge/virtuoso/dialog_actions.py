"""Explicit, single-use approval of one reviewed informational dialog."""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from virtuoso_bridge.virtuoso.dialogs import DialogTarget, _timeout


class DialogCloseTicket(BaseModel):
    """Review the preview before authorizing; it can contain sensitive text."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    ticket_id: str
    target: DialogTarget
    window_id: str
    title: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preview_png_b64: str
    valid_for_seconds: int = 120


class DialogCloseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["not_started", "requested", "closed", "unknown"]
    action_sent: bool | None
    diagnostics: list[str] = Field(default_factory=list)
    inspection: dict[str, Any] | None = None


class DialogActionOps:
    """No background policy, keyboard injection, retry, or workflow resumption."""

    def __init__(self, dialogs):
        self._dialogs = dialogs
        self._tickets: dict[str, tuple] = {}
        self._lock = threading.Lock()

    def _identity(self, deadline: float) -> dict[str, Any]:
        owner = self._dialogs._owner
        if not self._dialogs.enabled or self._dialogs.target is None:
            raise ValueError("Enable the authenticated dialog guard before preparing an action")
        if self._dialogs._endpoint != (owner.host, owner.port):
            raise ValueError("Bridge endpoint changed; explicitly rebind the guard")
        caps = owner._ensure_daemon_capabilities(deadline, refresh=True)
        if caps.get("auth") != "on" or caps.get("virtuoso_pid") != self._dialogs.target.pid:
            raise ValueError("Cannot verify the selected CIW identity")
        return caps

    def _exchange(self, payload, deadline):
        from virtuoso_bridge.transport.remote_paths import resolve_remote_username
        from virtuoso_bridge.virtuoso import x11

        owner = self._dialogs._owner
        runner = owner.gui_runner
        return x11.dialog_close_exchange(
            runner, resolve_remote_username(configured_user=getattr(runner, "user", None)),
            payload, profile=getattr(owner._tunnel, "_profile", None),
            timeout=_timeout(deadline - time.monotonic()),
        )

    def prepare(self, window_id: str, *, expected_title: str, timeout: float = 30) -> DialogCloseTicket:
        """Read a preview of ONE informational window; no action is authorized.

        The caller must review the visible content and Close behavior, or match
        an independently reviewed exact visual whitelist. Title matching alone
        cannot establish safety. Save/overwrite/SOS/run-control dialogs are out
        of scope, including when their title happens to match.
        """
        target = DialogTarget(pid=1, ciw_window=window_id)
        if (not isinstance(expected_title, str) or not expected_title.strip()
                or any(ord(c) < 32 for c in expected_title) or len(expected_title) > 512):
            raise ValueError("expected_title must be a nonempty exact window title")
        deadline = time.monotonic() + _timeout(timeout)
        caps = self._identity(deadline)
        bound = self._dialogs.target
        payload = self._exchange({"op": "prepare", "pid": bound.pid, "display": bound.display,
                                  "ciw_window": bound.ciw_window, "window_id": target.ciw_window,
                                  "title": expected_title}, deadline)
        if payload.get("status") != "prepared":
            raise ValueError("Dialog close preparation refused: " + str(payload.get("diagnostic", payload)))
        snapshot = payload["snapshot"]
        resolved = DialogTarget.model_validate(snapshot["target"])
        if resolved != bound or snapshot["dialog"]["title"] != expected_title:
            raise ValueError("Prepared window does not match selected target/title")
        actual_window = snapshot["dialog"]["window_id"]
        if int(actual_window, 16) != int(window_id, 16 if window_id.startswith("0x") else 10):
            raise ValueError("Prepared window ID changed")
        ticket = DialogCloseTicket(
            ticket_id=secrets.token_hex(16), target=resolved, window_id=actual_window,
            title=expected_title, content_sha256=snapshot["content_sha256"],
            preview_png_b64=payload["preview_png_b64"],
        )
        with self._lock:
            now = time.monotonic()
            self._tickets = {k: v for k, v in self._tickets.items() if v[1] > now}
            if len(self._tickets) >= 16:
                raise ValueError("Too many outstanding close previews; wait for expiry")
            self._tickets[ticket.ticket_id] = (ticket, now + 120, snapshot, caps.get("daemon_instance"))
        return ticket

    def close(self, ticket: DialogCloseTicket, *, authorized: bool = False,
              expected_content_sha256: str, timeout: float = 30) -> DialogCloseResult:
        """Send one targeted WM_DELETE_WINDOW request, never Enter or force-close.

        Explicit authorization and an independently approved content hash are
        required. Consumes the ticket BEFORE transport; even an unknown outcome
        cannot be retried with it. A closed window does not prove a design/run
        operation succeeded; query its original receipt separately.
        """
        if authorized is not True:
            return DialogCloseResult(status="not_started", action_sent=False,
                                     diagnostics=["Explicit authorization is required"])
        ticket = DialogCloseTicket.model_validate(ticket)
        deadline = time.monotonic() + _timeout(timeout)
        with self._lock:
            saved = self._tickets.pop(ticket.ticket_id, None)
        if (saved is None or saved[0] != ticket or saved[1] <= time.monotonic()
                or expected_content_sha256 != ticket.content_sha256):
            return DialogCloseResult(status="not_started", action_sent=False,
                                     diagnostics=["Ticket expired, changed, used, or content not approved"])
        try:
            caps = self._identity(deadline)
            if self._dialogs.target != ticket.target or caps.get("daemon_instance") != saved[3]:
                raise ValueError("CIW/daemon identity changed")
        except Exception as exc:
            return DialogCloseResult(status="not_started", action_sent=False, diagnostics=[str(exc)])
        try:
            payload = self._exchange({"op": "close", "snapshot": saved[2]}, deadline)
        except Exception as exc:
            return DialogCloseResult(status="unknown", action_sent=None,
                                     diagnostics=[str(exc), "Do not repeat the action; inspect and reconcile"])
        if payload.get("status") == "not_started" and payload.get("action_sent") is False:
            return DialogCloseResult(status="not_started", action_sent=False,
                                     diagnostics=[str(payload.get("diagnostic", "Action refused"))])
        if payload.get("status") != "requested" or payload.get("action_sent") is not True:
            return DialogCloseResult(status="unknown", action_sent=None,
                                     diagnostics=[str(payload), "Do not repeat the action"])
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                report = self._dialogs.inspect(timeout=remaining)
            except Exception as exc:
                return DialogCloseResult(status="requested", action_sent=True,
                                         diagnostics=[str(exc), "Closure not confirmed. Do not resend."])
            if report.status == "clear":
                return DialogCloseResult(status="closed", action_sent=True,
                                         inspection=report.model_dump(mode="json"))
            return DialogCloseResult(status="requested", action_sent=True,
                                     inspection=report.model_dump(mode="json"),
                                     diagnostics=["Request delivered; closure not confirmed. Do not resend."])
        return DialogCloseResult(status="requested", action_sent=True,
                                 diagnostics=["Wait budget exhausted; closure not verified"])
