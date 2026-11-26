"""Single-cellview GDM operations with independent SOS state verification."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import math
import posixpath
import re
import time
from typing import Literal, TYPE_CHECKING

from virtuoso_bridge.models import ExecutionStatus
from virtuoso_bridge.virtuoso.ops import q as skill_quote
from virtuoso_bridge.virtuoso.skill_output import is_single_complete_skill_list, parse_sexpr
from .environment import SOSEnvironmentError, resolve_soscmd, sos_runner
from .workarea import SOSWorkarea, _is_calibre_target

if TYPE_CHECKING:
    from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient


Outcome = Literal["success", "noop", "dry_run", "blocked", "failed", "unknown"]
_OA_TYPES = {"schematic", "schematicSymbol", "maskLayout"}
# Explicit columns avoid dependence on the user's SOS display preferences.
_STATUS_FORMAT = "%T\t%S\t%C\t%L\t%N\t%R\t%V\t%P"
_STATUS_NOTICES = {"** The flags and attributes have been updated."}
_ONLINE_NOTICE = re.compile(r"## All servers for project '[^'\r\n\t]+' are online\.")
_NATIVE_PROBE_MARKER = "VB_SOS_NATIVE_PROBE"
_NATIVE_DISPATCH_MARKER = "VB_SOS_NATIVE_DISPATCH"
_NATIVE_RESULT_MARKER = "VB_SOS_NATIVE_RESULT"
_MAESTRO_PROBE_MARKER = "VB_SOS_MAESTRO_PROBE"


class _ObjectUnavailable(RuntimeError):
    """SOS cannot provide a state for the requested object."""


@dataclass(frozen=True)
class SOSCellViewTarget:
    lib: str
    cell: str
    view: str
    directory: str = ""
    master: str = ""
    view_type: str = ""
    workarea: str = ""
    path: str = ""
    unsaved: bool | None = None


@dataclass(frozen=True)
class SOSCellViewState:
    object_type: str
    state: str
    change: str
    lock: str
    newer: str
    rso: str
    revision: str


@dataclass(frozen=True)
class SOSCellViewResult:
    action: str
    outcome: Outcome
    target: SOSCellViewTarget
    before: SOSCellViewState | None = None
    after: SOSCellViewState | None = None
    diagnostics: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.outcome in {"success", "noop", "dry_run"}

    def to_dict(self) -> dict:
        return {"ok": self.ok, **asdict(self)}


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\x00\r\n\t"):
        raise ValueError(f"{label} must be a non-empty single-line string without tabs.")
    return value


def _absolute_path(value: str) -> str:
    _text(value, "Remote path")
    if not posixpath.isabs(value) or value.startswith("//") or ".." in value.split("/"):
        raise ValueError(f"Expected an absolute remote path: {value!r}")
    return posixpath.normpath(value)


def _resolve_body(target: SOSCellViewTarget, required_action: str) -> str:
    lib, cell, view = (skill_quote(v) for v in (target.lib, target.cell, target.view))
    required = ["ddGetObj", "ddGetObjDMSys", "ddGetObjWritePath", "ddMapGetFileViewType",
                "dbGetOpenCellViews", "dbIsCellViewModified"]
    if required_action in {"co", "ci", "register"}:
        required.append("ddCheckout" if required_action == "co" else "ddCheckin")
    capabilities = " ".join("'" + name for name in required)
    return f'''
  foreach(vbFn list({capabilities})
    unless(isCallable(vbFn)
      return(list("blocked" sprintf(nil "Required Cadence API unavailable: %L" vbFn)))))
  vbLib = ddGetObj({lib})
  unless(vbLib return(list("blocked" "Library not found.")))
  unless(equal(ddGetObjDMSys(vbLib) "sos")
    return(list("blocked" "Library is not managed by SOS.")))
  vbView = ddGetObj({lib} {cell} {view} nil nil "r")
  vbFile = ddGetObj({lib} {cell} {view} "*" nil "r")
  unless(vbView && vbFile return(list("blocked" "Cellview or master file not found.")))
  vbDir = ddGetObjWritePath(vbView)
  vbMaster = ddGetObjWritePath(vbFile)
  vbType = ddMapGetFileViewType(vbFile)
  vbDirty = nil
  foreach(vbCV dbGetOpenCellViews()
    when(equal(vbCV~>libName {lib}) && equal(vbCV~>cellName {cell}) &&
         equal(vbCV~>viewName {view}) && dbIsCellViewModified(vbCV)
      vbDirty = t))
'''


def _skill(target: SOSCellViewTarget, action: str = "status", message: str = "", *,
           required_action: str | None = None) -> str:
    body = _resolve_body(target, required_action or action)
    if action == "status":
        tail = 'return(list("ok" vbDir vbMaster vbType vbDirty))'
    else:
        call = "ddCheckout(vbFile)" if action == "co" else f"ddCheckin(vbFile {skill_quote(message)})"
        # These guards run in the same CIW expression as GDM, not in an earlier RPC.
        tail = f'''
  unless(equal(vbDir {skill_quote(target.directory)}) &&
         equal(vbMaster {skill_quote(target.master)}) &&
         equal(vbType {skill_quote(target.view_type)})
    return(list("blocked" "Target path or view type changed before execution.")))
  when(vbDirty return(list("blocked" "Unsaved changes; save the cellview and retry.")))
  unless(member(vbType list("schematic" "schematicSymbol" "maskLayout"))
    return(list("blocked" "Unsupported view type for GDM writes.")))
  if({call} then return(list("ok"))
    else return(list("failed" "GDM returned nil.")))
'''
    return f"prog((vbLib vbView vbFile vbDir vbMaster vbType vbDirty vbCV vbFn)\n{body}\n{tail}\n)"


def _native_probe_skill() -> str:
    required = (
        "SosHMHierBrowserMenuCB", "SosHMCheckInMB", "hiTreeTableGetItems",
        "hiTreeTableDeselectAllItems", "hiTreeTableSelectItem",
        "hiReportDeselectAllItems", "hiReportSelectItem",
        "hiReportGetSelectedItems", "hiGetCurrentForm", "hiFormList",
        "hiIsForm", "hiIsFormDisplayed", "hiSetCurrentForm", "hiFormDone",
        "hiFormCancel", "hiRegTimer", "get_string", "symeval",
    )
    capabilities = " ".join("'" + name for name in required)
    return f'''prog((vbFn)
  ; {_NATIVE_PROBE_MARKER}
  foreach(vbFn list({capabilities})
    unless(isCallable(vbFn) return(list("ok" nil))))
  return(list("ok" t))
)'''


def _native_dispatch_skill(target: SOSCellViewTarget, action: str, message: str) -> str:
    lib, cell, view, description = (
        skill_quote(value) for value in (target.lib, target.cell, target.view, message)
    )
    expected_status = "Unmanaged" if action == "register" else "Checkedout"
    return f'''prog((vbLibItem vbItem vbRows vbRow vbIndex vbMatch vbStatus)
  ; {_NATIVE_DISPATCH_MARKER}
  when(boundp('vbSosBridgeActive) && vbSosBridgeActive
    return(list("blocked" "Another SOS Design Manager operation is still active.")))
  when(hiGetCurrentForm()
    return(list("blocked" "A modal form is already active in the connected CIW.")))
  unless(boundp('SosHMLibForm) && boundp('SosHierManageForm) &&
         SosHMLibForm->libTree && SosHierManageForm->designHierRpt
    SosHMHierBrowserMenuCB())
  unless(boundp('SosHMLibForm) && boundp('SosHierManageForm) &&
         SosHMLibForm->libTree && SosHierManageForm->designHierRpt
    return(list("failed" "SOS Design Manager forms are unavailable.")))
  foreach(vbItem hiTreeTableGetItems(SosHMLibForm->libTree)
    when(equal(get_string(vbItem) strcat("_sos" {lib})) vbLibItem=vbItem))
  unless(vbLibItem return(list("blocked" "Target library is not listed by SOS Design Manager.")))
  hiTreeTableDeselectAllItems(SosHMLibForm->libTree nil)
  hiTreeTableSelectItem(SosHMLibForm->libTree vbLibItem t)
  vbRows=SosHierManageForm->designHierRpt->choices
  vbIndex=0
  foreach(vbRow vbRows
    when(length(vbRow)>=4 && equal(nth(0 vbRow) {lib}) &&
         equal(nth(1 vbRow) {cell}) && equal(nth(2 vbRow) {view})
      if(vbMatch then
        return(list("blocked" "SOS Design Manager returned duplicate target rows."))
      else
        vbMatch=vbIndex
        vbStatus=nth(3 vbRow)))
    vbIndex=vbIndex+1)
  unless(numberp(vbMatch)
    return(list("blocked" "Target cellview is not listed by SOS Design Manager.")))
  unless(rexMatchp(strcat("^" {skill_quote(expected_status)}) vbStatus)
    return(list("blocked" sprintf(nil "SOS Design Manager status changed: %s" vbStatus))))
  hiReportDeselectAllItems(SosHierManageForm->designHierRpt nil)
  hiReportSelectItem(SosHierManageForm->designHierRpt vbMatch t)
  unless(equal(hiReportGetSelectedItems(SosHierManageForm->designHierRpt) list(vbMatch))
    return(list("failed" "SOS Design Manager did not retain the unique target selection.")))

  procedure(vbSosBridgeSubmit()
    prog((vbSym vbForm vbForms vbFormRows vbFormRow vbFormIndex vbFormMatch)
      vbSosBridgeFormAttempts=vbSosBridgeFormAttempts+1
      foreach(vbSym hiFormList()
        unless(member(vbSym vbSosBridgeFormsBefore)
          when(boundp(vbSym)
            vbForm=symeval(vbSym)
            when(hiIsForm(vbForm) && hiIsFormDisplayed(vbForm) &&
                 vbForm->descItem && vbForm->cellViewListBox
              vbForms=cons(vbForm vbForms)))))
      when(length(vbForms)>1
        vbSosBridgeError="More than one new SOS checkin form was displayed."
        foreach(vbForm vbForms hiFormCancel(vbForm))
        return(nil))
      when(length(vbForms)==1
        vbForm=car(vbForms)
        vbSosBridgeForm=vbForm
        vbFormIndex=0
        vbFormRows=vbForm->cellViewListBox->choices
        foreach(vbFormRow vbFormRows
          when(length(vbFormRow)>=4 && equal(nth(1 vbFormRow) {lib}) &&
               equal(nth(2 vbFormRow) {cell}) && equal(nth(3 vbFormRow) {view})
            if(vbFormMatch then
              vbSosBridgeError="SOS checkin form returned duplicate target rows."
              hiFormCancel(vbForm)
              return(nil)
            else vbFormMatch=vbFormIndex))
          vbFormIndex=vbFormIndex+1)
        unless(numberp(vbFormMatch)
          vbSosBridgeError="SOS checkin form target did not match the requested cellview."
          hiFormCancel(vbForm)
          return(nil))
        hiReportDeselectAllItems(vbForm->cellViewListBox nil)
        hiReportSelectItem(vbForm->cellViewListBox vbFormMatch nil)
        unless(equal(hiReportGetSelectedItems(vbForm->cellViewListBox) list(vbFormMatch))
          vbSosBridgeError="SOS checkin form did not retain the unique target selection."
          hiFormCancel(vbForm)
          return(nil))
        vbForm->descItem->value={description}
        unless(hiSetCurrentForm(vbForm)
          vbSosBridgeError="SOS checkin form could not be made current for its callback."
          hiFormCancel(vbForm)
          return(nil))
        vbSosBridgeFormSubmitted=t
        hiFormDone(vbForm)
        return(t))
      if(vbSosBridgeFormAttempts<50 then
        hiRegTimer("vbSosBridgeSubmit()" 2)
      else
        vbSosBridgeError="SOS checkin form did not become available." )
      return(nil)))

  procedure(vbSosBridgeLaunch()
    prog((vbResult)
      vbResult=errset(SosHMCheckInMB() t)
      vbSosBridgeLaunchOk=if(vbResult then t else nil)
      unless(vbSosBridgeLaunchOk
        vbSosBridgeError="SOS Design Manager checkin callback failed.")
      vbSosBridgeDone=t
      vbSosBridgeActive=nil
      return(vbSosBridgeLaunchOk)))

  vbSosBridgeActive=list({lib} {cell} {view})
  vbSosBridgeDone=nil
  vbSosBridgeFormSubmitted=nil
  vbSosBridgeFormAttempts=0
  vbSosBridgeLaunchOk=nil
  vbSosBridgeError=nil
  vbSosBridgeForm=nil
  vbSosBridgeFormsBefore=hiFormList()
  unless(hiRegTimer("vbSosBridgeSubmit()" 5)
    vbSosBridgeActive=nil
    return(list("failed" "Could not schedule the SOS checkin form handler.")))
  unless(hiRegTimer("vbSosBridgeLaunch()" 1)
    vbSosBridgeActive=nil
    return(list("failed" "Could not schedule the SOS Design Manager callback.")))
  return(list("scheduled" "sos_design_manager"))
)'''


def _native_result_skill() -> str:
    return f'''prog(()
  ; {_NATIVE_RESULT_MARKER}
  unless(boundp('vbSosBridgeDone)
    return(list("failed" "SOS Design Manager result state is unavailable.")))
  return(list("ok" vbSosBridgeDone vbSosBridgeFormSubmitted
              vbSosBridgeLaunchOk vbSosBridgeError))
)'''


def _native_available(owner: VirtuosoClient, timeout: float) -> bool:
    result = _decode(owner.execute_skill(_native_probe_skill(), timeout=timeout))
    if len(result) != 2 or result[0] != "ok" or result[1] not in {True, None}:
        raise RuntimeError("Malformed SOS Design Manager capability result.")
    return result[1] is True


def _native_result(owner: VirtuosoClient, timeout: float) -> list:
    result = _decode(owner.execute_skill(_native_result_skill(), timeout=timeout))
    if (len(result) != 5 or result[0] != "ok"
            or any(value not in {True, None} for value in result[1:4])
            or (result[4] is not None and not isinstance(result[4], str))):
        raise RuntimeError("Malformed SOS Design Manager operation result.")
    return result


def _maestro_active(owner: VirtuosoClient, timeout: float) -> bool:
    result = _decode(owner.execute_skill(f'''prog((vbSessions vbSessionsResult)
  ; {_MAESTRO_PROBE_MARKER}
  when(isCallable('maeGetSessions)
    vbSessionsResult=errset(maeGetSessions() nil)
    unless(vbSessionsResult return(list("failed" "maeGetSessions failed.")))
    vbSessions=car(vbSessionsResult))
  return(list("ok" if(vbSessions then t else nil)))
)''', timeout=timeout))
    if (len(result) == 2 and result[0] == "failed"
            and isinstance(result[1], str)):
        raise RuntimeError(result[1])
    if len(result) != 2 or result[0] != "ok" or result[1] not in {True, None}:
        raise RuntimeError("Malformed Maestro session probe result.")
    return result[1] is True


def _decode(result) -> list:
    if result.status != ExecutionStatus.SUCCESS or result.errors:
        raise RuntimeError("; ".join(result.errors) or "Bridge SKILL execution failed.")
    if not is_single_complete_skill_list(result.output):
        raise RuntimeError("Malformed SKILL result; expected one complete tagged list.")
    value = parse_sexpr(result.output)
    if (not isinstance(value, list) or not value or not isinstance(value[0], str)
            or value[0] not in {"ok", "scheduled", "blocked", "failed"}):
        raise RuntimeError("Malformed SKILL result tag.")
    return value


def _resolve_ciw_target(
    owner: VirtuosoClient,
    target: SOSCellViewTarget,
    timeout: float,
    *,
    required_action: str = "status",
) -> SOSCellViewTarget:
    """Resolve one cellview in CIW without invoking an SOS command."""
    resolved = _decode(owner.execute_skill(
        _skill(target, required_action=required_action), timeout=timeout,
    ))
    if resolved[0] != "ok":
        if len(resolved) != 2 or not isinstance(resolved[1], str):
            raise RuntimeError("Malformed cellview resolution error.")
        raise _ObjectUnavailable(resolved[1])
    if len(resolved) != 5 or (resolved[4] is not None and resolved[4] is not True):
        raise RuntimeError("Malformed cellview resolution result.")
    directory, master = (_absolute_path(value) for value in resolved[1:3])
    if master == directory or posixpath.commonpath([directory, master]) != directory:
        raise RuntimeError("Resolved master file is outside the cellview directory.")
    if resolved[3] is not None and not isinstance(resolved[3], str):
        raise RuntimeError("Malformed cellview type.")
    return replace(
        target,
        directory=directory,
        master=master,
        view_type=resolved[3] or "",
        unsaved=(resolved[4] is True) if resolved[3] in _OA_TYPES else None,
    )


_NOBJSTATUS_ATTRIBUTES = (
    "-gaRevision", "-gaCurrentVer", "-gaModified", "-gaCiModified", "-gaOutOfDate",
    "-gaReference",
)


def _parse_nobjstatus_record(output: str) -> tuple[str, str, str, dict[str, str]]:
    """Parse one length-prefixed SOS nobjstatus record without interpreting it."""
    payload = output.encode("utf-8")
    offset = 0

    def read_line(label: str) -> bytes:
        nonlocal offset
        end = payload.find(b"\n", offset)
        if end < 0:
            raise RuntimeError(f"Malformed SOS nobjstatus output: missing {label}.")
        value = payload[offset:end]
        offset = end + 1
        return value[:-1] if value.endswith(b"\r") else value

    header = read_line("header")
    match = re.fullmatch(br"!nObjStatus! ([0-9]+)", header)
    if match is None or int(match.group(1)) != 1:
        raise RuntimeError("SOS nobjstatus must return exactly one record.")
    if read_line("record marker") != b"!Record!":
        raise RuntimeError("Malformed SOS nobjstatus record marker.")

    path = read_line("object path").decode("utf-8", "strict")
    status_code = read_line("status code").decode("ascii", "strict")
    type_code = read_line("object type").decode("ascii", "strict")

    attributes: dict[str, str] = {}
    while offset < len(payload):
        if payload[offset:].strip(b"\r\n") == b"":
            break
        key = read_line("attribute name").decode("utf-8", "strict")
        length_text = read_line("attribute length")
        if not re.fullmatch(br"[0-9]+", length_text):
            raise RuntimeError("Malformed SOS nobjstatus attribute length.")
        length = int(length_text)
        end = offset + length
        if end > len(payload):
            raise RuntimeError("Truncated SOS nobjstatus attribute value.")
        value = payload[offset:end].decode("utf-8", "strict")
        offset = end
        if length == 0:
            pass
        elif payload[offset:offset + 2] == b"\r\n":
            offset += 2
        elif payload[offset:offset + 1] == b"\n":
            offset += 1
        else:
            raise RuntimeError("Malformed SOS nobjstatus attribute terminator.")
        if not key or key in attributes:
            raise RuntimeError("Duplicate or empty SOS nobjstatus attribute.")
        attributes[key] = value

    return path, status_code, type_code, attributes


def _parse_nobjstatus(output: str, target: SOSCellViewTarget) -> SOSCellViewState:
    """Parse one length-prefixed, server-queried SOS nobjstatus record."""
    path, status_code, type_code, attributes = _parse_nobjstatus_record(output)
    if path not in {target.path, "./" + target.path}:
        raise RuntimeError("SOS nobjstatus returned a different object path.")

    object_types = {"1": "f", "2": "d", "3": "p"}
    object_type = object_types.get(type_code)
    if object_type is None:
        raise RuntimeError(f"Unsupported SOS nobjstatus object type: {type_code!r}.")
    if status_code in {"0", "1"}:
        raise _ObjectUnavailable("SOS nobjstatus reports that the target is unavailable.")
    if status_code == "2":
        return SOSCellViewState(object_type, "?", "?", "?", "?", "?", "?")
    state_and_lock = {
        "3": ("O", "-"),
        "4": ("-", "L"),
        "5": ("-", "-"),
        "6": ("O", "?"),
    }
    if status_code not in state_and_lock:
        raise RuntimeError(f"Unsupported SOS nobjstatus state: {status_code!r}.")

    flags = {}
    for name in ("Modified", "OutOfDate"):
        value = attributes.get(name)
        if value not in {"0", "1"}:
            raise RuntimeError(f"SOS nobjstatus is missing a valid {name} flag.")
        flags[name] = value
    ci_modified = attributes.get("CiModified", "0")
    if ci_modified not in {"0", "1"}:
        raise RuntimeError("SOS nobjstatus returned an invalid CiModified flag.")
    flags["CiModified"] = ci_modified
    if "Reference" not in attributes:
        raise RuntimeError("SOS nobjstatus is missing the Reference attribute.")
    revision = attributes.get("Revision") or attributes.get("CurrentVer")
    current = attributes.get("CurrentVer")
    if (not revision or any(char in revision for char in "\x00\r\n\t")
            or (current and current != revision)):
        raise RuntimeError("SOS nobjstatus returned an invalid or inconsistent revision.")

    state, lock = state_and_lock[status_code]
    change = "M" if flags["Modified"] == "1" or flags["CiModified"] == "1" else "-"
    newer = "N" if flags["OutOfDate"] == "1" else "-"
    rso = "R" if attributes["Reference"] else "-"
    return SOSCellViewState(object_type, state, change, lock, newer, rso, revision)


def _read_nobj_state(
    area: SOSWorkarea, target: SOSCellViewTarget, timeout: float,
) -> SOSCellViewState:
    result = area._run(
        "nobjstatus", *_NOBJSTATUS_ATTRIBUTES, "./" + target.path, timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(
            result.stderr.strip() or result.stdout.strip() or "SOS nobjstatus failed."
        )
    return _parse_nobjstatus(result.stdout, target)

def _read_state(area: SOSWorkarea, target: SOSCellViewTarget, timeout: float, *,
                include_unmanaged: bool = False) -> SOSCellViewState:
    # Selection filters imply recursion in SOS unless -sNr explicitly disables it.
    selection = ("-sall", "-sNr") if include_unmanaged else ()
    result = area._run("status", "-Nhdr", "-f" + _STATUS_FORMAT,
                       *selection, "./" + target.path, timeout=timeout)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "SOS status failed."
        try:
            return _read_nobj_state(area, target, timeout)
        except Exception as exc:
            raise RuntimeError(
                f"{detail}; server-queried nobjstatus fallback failed: {exc}"
            ) from exc
    lines = [
        line for line in result.stdout.splitlines()
        if line.strip() and line.strip() not in _STATUS_NOTICES
        and not _ONLINE_NOTICE.fullmatch(line.strip())
    ]
    if lines == ["!! Warning: No objects selected to display status."]:
        raise _ObjectUnavailable(
            "SOS did not select this object; it may be unmanaged or unavailable in this workarea."
        )
    if len(lines) != 1:
        raise RuntimeError(
            f"Expected exactly one SOS object, got {len(lines)} status rows: {result.stdout.strip()!r}"
        )
    fields = lines[0].split("\t", 7)
    if len(fields) != 8 or fields[7] not in {target.path, "./" + target.path}:
        raise RuntimeError("SOS returned a malformed status row or a different object path.")
    allowed = ("fpdsFPDS", "O-WNX?", "M!-?", "L-?", "N-?", "R-?")
    if any(len(v) != 1 or v not in choices for v, choices in zip(fields[:6], allowed)):
        raise RuntimeError("Unrecognized SOS status flags.")
    if not fields[6].strip():
        raise RuntimeError("SOS status is missing a revision.")
    return SOSCellViewState(*fields[:7])


def _target_precondition(target: SOSCellViewTarget):
    if target.view_type not in _OA_TYPES:
        return "blocked", f"Unsupported view type {target.view_type!r}; writes support OA schematic, symbol and layout."
    if not target.master.endswith(".oa"):
        return "blocked", "Writes require an OpenAccess (.oa) master file."
    if target.unsaved:
        return "blocked", "Unsaved changes in the connected CIW; save the cellview and retry."
    return None


def _precondition(action: str, state: SOSCellViewState):
    if action == "register":
        if state.object_type not in {"d", "p"} or (
            state.state, state.change, state.lock, state.newer, state.rso, state.revision
        ) != ("?", "?", "?", "?", "?", "?"):
            return "blocked", (
                "Initial registration requires an explicitly unmanaged directory/package "
                "with no revision. Managed, reference, unpopulated or ambiguous objects "
                "cannot be registered; use the normal checkout/checkin workflow for managed views."
            )
        return None
    if action == "cancel_co":
        if state.object_type != "p" or state.revision == "?":
            return "blocked", "Target must be an existing managed SOS package."
        if state.rso != "-":
            return "blocked", "Reference or ambiguous SOS objects cannot be mutated."
        if state.lock != "-":
            return "blocked", "Target is locked in another workarea, or lock status is unavailable."
        if state.state == "-" and state.change == "-":
            return "noop", "Target is already checked in; no checkout was released."
        if state.state != "O" or state.change != "-":
            return "blocked", "Cancel checkout requires this workarea's checkout to be saved and unmodified."
        return None
    if state.object_type != "p" or state.revision == "?" or state.rso != "-":
        return "blocked", "Target must be an existing managed SOS package, not a reference or unmanaged object."
    if state.lock != "-":
        return "blocked", "Target is locked in another workarea, or lock status is unavailable."
    if state.state not in {"O", "-"} or state.change not in {"M", "-"}:
        return "blocked", "Target is unavailable, missing, or in an unsupported SOS state."
    if action == "co":
        if state.state == "O":
            return "noop", "Already checked out in this workarea."
        if state.change == "M":
            return "blocked", "Modified without checkout; resolve the workarea state before checkout."
    else:
        if state.state != "O":
            return "blocked", "Target is not checked out in this workarea."
        if state.change == "-":
            return "noop", "No saved modifications to check in; checkout is retained."
    return None


def _verified(action: str, before: SOSCellViewState, after: SOSCellViewState) -> bool:
    if (after.object_type != "p" or after.lock != "-" or after.rso != "-"
            or after.revision == "?"):
        return False
    if action == "co":
        return after.state == "O" and after.change in {"-", "M"}
    if action == "cancel_co":
        return (after.state == "-" and after.change == "-"
                and after.revision == before.revision)
    if action == "register":
        return (after.state == "-" and after.change == "-"
                and after.newer == "-" and after.rso == "-"
                and re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", after.revision) is not None)
    return after.state == "-" and after.change == "-" and after.revision != before.revision


def _read_post_state(owner: VirtuosoClient, area: SOSWorkarea, target: SOSCellViewTarget,
                     action: str, before: SOSCellViewState, timeout: float, *, native: bool):
    """Wait only for an already-dispatched native SOS action; never redispatch it."""
    if not native:
        return _read_state(area, target, timeout, include_unmanaged=action == "register")
    deadline = time.monotonic() + timeout
    last_state = None
    last_error = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if last_state is not None:
                return last_state
            if last_error is not None:
                raise last_error
            raise TimeoutError("Timed out waiting for SOS Design Manager completion.")
        try:
            completion = _native_result(owner, remaining)
        except Exception:
            completion = None
        if completion is not None and completion[1] is True:
            fresh_timeout = deadline - time.monotonic()
            return _read_state(
                area,
                target,
                fresh_timeout if fresh_timeout > 0 else timeout,
                include_unmanaged=action == "register",
            )
        remaining_after = deadline - time.monotonic()
        if remaining_after > 0:
            try:
                last_state = _read_state(
                    area, target, remaining_after, include_unmanaged=action == "register",
                )
                last_error = None
            except Exception as exc:
                last_error = exc
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def operate_cellview(
    owner: VirtuosoClient, action: str, lib: str, cell: str, view: str, *,
    message: str = "", dry_run: bool = False, timeout: float = 60,
    soscmd: str | None = None,
    include_unmanaged: bool = False,
) -> SOSCellViewResult:
    """Perform one GDM action at most once; report uncertain outcomes explicitly."""
    target = SOSCellViewTarget(*(_text(v, name) for v, name in ((lib, "lib"), (cell, "cell"), (view, "view"))))
    if action not in {"status", "co", "cancel_co", "ci", "register"}:
        raise ValueError(f"Unsupported SOS cellview action: {action}")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive.")
    if action in {"ci", "register"}:
        _text(message, "message")
    before = None
    after = None
    native = False

    def report(outcome: Outcome, *diagnostics: str) -> SOSCellViewResult:
        return SOSCellViewResult(action, outcome, target, before, after, tuple(diagnostics))

    if action in {"cancel_co", "ci", "register"} and _is_calibre_target(lib, cell, view):
        return report("blocked", "Calibre-related targets cannot be mutated through this API.")

    try:
        if sos_runner(owner) is None:
            return report("blocked", "SOS requires a GUI-host SSH runner or an explicitly local POSIX client; plain TCP/native Windows local is unsupported.")
        target = _resolve_ciw_target(owner, target, timeout, required_action=action)
        if action in {"cancel_co", "ci", "register"} and _is_calibre_target(
            target.directory, target.master,
        ):
            return report("blocked", "Resolved path belongs to a Calibre-related target; mutation is prohibited.")
        if action == "register":
            decision = _target_precondition(target)
            if decision:
                return report(*decision)
        executable = resolve_soscmd(owner, soscmd, timeout=timeout)
        probe = SOSWorkarea(owner, target.directory, soscmd=executable)
        found = probe._run("findwaroot", timeout=timeout)
        if found.returncode:
            raise RuntimeError(found.stderr.strip() or found.stdout.strip() or "SOS workarea not found.")
        workarea = _absolute_path(found.stdout.strip())
        if (target.directory == workarea
                or posixpath.commonpath([target.directory, workarea]) != workarea):
            raise RuntimeError("Resolved cellview is outside the returned SOS workarea.")
        target = replace(
            target, workarea=workarea,
            path=posixpath.relpath(target.directory, workarea),
        )
        area = SOSWorkarea(owner, workarea, soscmd=executable)
        if action in {"co", "cancel_co", "ci"}:
            decision = _target_precondition(target)
            if decision:
                return report(*decision)
        before = _read_state(area, target, timeout,
                             include_unmanaged=action == "register" or include_unmanaged)
        if action == "status":
            return report("success")
        decision = _precondition(action, before)
        if decision:
            return report(*decision)
        if dry_run:
            return report("dry_run", "Preconditions passed; no SOS mutation was executed.")
        if action == "cancel_co":
            # Re-resolve the cellview and refresh SOS immediately before the one-shot discard.
            refreshed_target = _decode(owner.execute_skill(
                _skill(target, required_action="cancel_co"), timeout=timeout,
            ))
            if refreshed_target != [
                "ok", target.directory, target.master, target.view_type, None,
            ]:
                return report(
                    "blocked",
                    "Target identity or connected-CIW save state changed before cancel checkout.",
                )
            refreshed_state = _read_state(area, target, timeout)
            if refreshed_state != before:
                return report(
                    "blocked", "SOS state changed before cancel checkout; inspect status first.",
                )
        if action == "register":
            # A previous dry-run is not authorization to treat a now-managed object
            # as new. Refresh once immediately before sending the one-shot GDM call.
            refreshed = _read_state(area, target, timeout, include_unmanaged=True)
            if refreshed != before:
                return report("blocked", "SOS state changed before initial registration; inspect status first.")
        if action in {"ci", "register"}:
            native = _native_available(owner, timeout)
            if not native and _maestro_active(owner, timeout):
                return report(
                    "blocked",
                    "SOS Design Manager automation is unavailable while Maestro is open; "
                    "direct GDM checkin can change an unrelated Maestro session mode.",
                )
    except (_ObjectUnavailable, SOSEnvironmentError) as exc:
        return report("blocked", str(exc))
    except Exception as exc:
        return report("failed", str(exc))

    if action == "cancel_co":
        diagnostics = []
        discard_reply = None
        try:
            discard_reply = area._run("discardco", "./" + target.path, timeout=timeout)
        except Exception as exc:
            diagnostics.append(f"SOS discardco result unavailable: {exc}")
        try:
            after = _read_state(area, target, timeout)
        except Exception as exc:
            diagnostics.append(f"Post-operation SOS status unavailable: {exc}")
        if discard_reply is None:
            if after is not None and _verified(action, before, after):
                diagnostics.append(
                    "SOS now shows the intended state, but the discardco result was not confirmed."
                )
            return report("unknown", *diagnostics,
                          "Do not retry automatically; inspect SOS status first.")
        detail = discard_reply.stderr.strip() or discard_reply.stdout.strip()
        if discard_reply.returncode:
            if after is not None and _verified(action, before, after):
                diagnostics.insert(0, detail or "SOS discardco returned a nonzero status.")
                return report("unknown", *diagnostics,
                              "Do not retry automatically; inspect SOS status first.")
            return report("failed", detail or "SOS discardco failed.", *diagnostics)
        if after is None:
            return report("unknown", *diagnostics,
                          "SOS discardco succeeded but post-operation status is unavailable.")
        if not _verified(action, before, after):
            return report("failed", "SOS discardco succeeded but its postcondition did not match.")
        return report("success")

    # From here, an incomplete response may mean a completed server-side write.
    # Only SSH status reads are allowed after ambiguity; never resend GDM.
    reply = None
    diagnostics = []
    provider = "SOS Design Manager" if native else "GDM"
    try:
        mutation = (_native_dispatch_skill(target, action, message) if native
                    else _skill(target, action, message))
        reply = _decode(owner.execute_skill(
            mutation, timeout=timeout, retry_connect=False,
        ))
        expected = ["scheduled", "sos_design_manager"] if native else ["ok"]
        if reply != expected and not (
            len(reply) == 2 and reply[0] in {"blocked", "failed"} and isinstance(reply[1], str)
        ):
            raise RuntimeError(f"Malformed {provider} response.")
    except Exception as exc:
        reply = None
        diagnostics.append(f"{provider} result unavailable: {exc}")
    if reply is None:
        try:
            after = _read_state(
                area, target, timeout, include_unmanaged=action == "register",
            )
        except Exception as exc:
            diagnostics.append(f"Post-operation SOS status unavailable: {exc}")
        if after is not None and _verified(action, before, after):
            diagnostics.append("SOS now shows the intended state, but the GDM result was not confirmed.")
        return report("unknown", *diagnostics, "Do not retry automatically; inspect SOS status first.")
    if reply[0] != "ok" and (not native or reply[0] != "scheduled"):
        try:
            after = _read_state(
                area, target, timeout, include_unmanaged=action == "register",
            )
        except Exception as exc:
            diagnostics.append(f"Post-operation SOS status unavailable: {exc}")
        return report(reply[0], reply[1], *diagnostics)
    try:
        after = _read_post_state(owner, area, target, action, before, timeout, native=native)
    except Exception as exc:
        diagnostics.append(f"Post-operation SOS status unavailable: {exc}")
    if native:
        try:
            native_reply = _native_result(owner, timeout)
        except Exception as exc:
            diagnostics.append(f"SOS Design Manager completion unavailable: {exc}")
            return report("unknown", *diagnostics,
                          "Do not retry automatically; inspect SOS status first.")
        if not all(native_reply[1:4]) or native_reply[4] is not None:
            return report("failed", native_reply[4] or
                          "SOS Design Manager did not confirm form submission and callback completion.",
                          *diagnostics)
    if after is None:
        return report("unknown", *diagnostics,
                      f"{provider} succeeded but SOS verification is unavailable.")
    if not _verified(action, before, after):
        return report("failed", f"{provider} reported success but the SOS postcondition did not match.")
    return report("success")
