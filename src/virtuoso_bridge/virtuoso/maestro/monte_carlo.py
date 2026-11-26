"""Structured Monte Carlo configuration and execution for Maestro."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from virtuoso_bridge.virtuoso.maestro.history import (
    MaestroHistoryLockResult,
    lock_history,
)
from virtuoso_bridge.virtuoso.maestro.reader.state import (
    MaestroSessionState,
    list_session_states,
)
from virtuoso_bridge.virtuoso.maestro.writer import run_and_wait
from virtuoso_bridge.virtuoso.ops import escape_skill_string
from virtuoso_bridge.virtuoso.skill_output import parse_sexpr

if TYPE_CHECKING:
    from virtuoso_bridge import VirtuosoClient


Variation = Literal["global", "mismatch", "all"]
SamplingMode = Literal["random", "orthogonal", "lhs", "lds"]
ModuleFilterMode = Literal["include", "exclude"]
ModuleKind = Literal["Master", "Subcircuit", "Schematic"]

MC_RUN_MODE = "Monte Carlo Sampling"
_OPTIONS_TAG = "VB_MAESTRO_MC_OPTIONS_V2"
_MUTATION_TAG = "VB_MAESTRO_MC_CONFIG_V2"
_RUN_MODE_TAG = "VB_MAESTRO_MC_RUN_MODE_V2"
_EXPORT_TAG = "VB_MAESTRO_MC_EXPORT_V1"
_UNKNOWN_TRANSPORT_MARKERS = (
    "socket timeout",
    "socket error",
    "connection refused",
)
_DEFAULTS = {
    "mcmethod": "all",
    "mcnumpoints": "100",
    "samplingmode": "random",
    "montecarloseed": "12345",
    "mcstartingrunnumber": "1",
    "donominal": "1",
    "saveallplots": "0",
    "saveprocess": "1",
    "savemismatch": "0",
}
_KNOWN_OPTIONS = {
    *_DEFAULTS,
    "mcnumbins",
    "dutsummary",
    "ignoreflag",
}


class MaestroMonteCarloError(RuntimeError):
    """A Monte Carlo operation failed or could not be verified."""


class MaestroMonteCarloOutcomeUnknown(MaestroMonteCarloError):
    """A mutating request may have executed, but persistence is unconfirmed."""


class MonteCarloModule(BaseModel):
    """One hierarchical DUT entry used by Maestro mismatch filtering."""

    model_config = ConfigDict(frozen=True)

    test: str
    instance: str
    master: str
    kind: ModuleKind = "Subcircuit"

    @field_validator("test", "master")
    @classmethod
    def _validate_component(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("module fields must be non-empty")
        if any(char in normalized for char in "%#;\r\n"):
            raise ValueError("module fields cannot contain %, #, ;, or newlines")
        return normalized

    @field_validator("instance")
    @classmethod
    def _validate_instance(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("/"):
            raise ValueError("instance must be an absolute hierarchical path")
        if any(char in normalized for char in "%#;\r\n"):
            raise ValueError("instance cannot contain %, #, ;, or newlines")
        return normalized


class MonteCarloModuleFilter(BaseModel):
    """Apply mismatch to only these modules, or exclude these modules."""

    model_config = ConfigDict(frozen=True)

    mode: ModuleFilterMode
    modules: tuple[MonteCarloModule, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _require_selection(self) -> "MonteCarloModuleFilter":
        if not self.modules:
            raise ValueError("module filter requires at least one module")
        identities = [(item.test, item.instance, item.master, item.kind) for item in self.modules]
        if len(set(identities)) != len(identities):
            raise ValueError("module filter entries must be unique")
        return self


class MonteCarloConfig(BaseModel):
    """Supported Maestro Monte Carlo run options."""

    model_config = ConfigDict(frozen=True)

    variation: Variation = "all"
    points: int = Field(default=100, ge=1)
    sampling: SamplingMode = "random"
    seed: int | None = Field(default=12345, ge=1)
    starting_run: int = Field(default=1, ge=1)
    nominal: bool = True
    save_all_plots: bool = False
    save_process: bool = True
    save_mismatch: bool = False
    num_bins: int | None = Field(default=None, ge=1)
    module_filter: MonteCarloModuleFilter | None = None
    extra_options: dict[str, str] = Field(default_factory=dict)

    @field_validator("extra_options")
    @classmethod
    def _validate_extra_options(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        normalized_names: set[str] = set()
        for name, option_value in value.items():
            if not isinstance(name, str):
                raise ValueError("extra option names must be strings")
            option_name = name.strip()
            lowered = option_name.lower()
            if not option_name or any(
                ord(char) < 0x20 or ord(char) == 0x7F for char in option_name
            ):
                raise ValueError("extra option names must be non-empty")
            if lowered in _KNOWN_OPTIONS:
                raise ValueError(f"extra option {option_name!r} duplicates a known option")
            if lowered in normalized_names:
                raise ValueError("extra option names must be unique ignoring case")
            if not isinstance(option_value, str):
                raise ValueError("extra option values must be strings")
            _require_skill_text(option_value, f"extra option {option_name!r} value")
            normalized_names.add(lowered)
            normalized[option_name] = option_value
        return normalized

    @model_validator(mode="after")
    def _validate_semantics(self) -> "MonteCarloConfig":
        if self.module_filter is not None and self.variation == "global":
            raise ValueError("module mismatch filtering requires mismatch or all variation")
        return self


class MonteCarloConfigureResult(BaseModel):
    """Verified outcome of a Monte Carlo configuration request."""

    model_config = ConfigDict(frozen=True)

    session: str
    requested: MonteCarloConfig
    before: MonteCarloConfig | None
    after: MonteCarloConfig | None = None
    before_run_mode: str
    after_run_mode: str | None = None
    changed: bool
    saved: bool
    outcome: Literal[
        "dry_run", "already_satisfied", "applied", "confirmed_after_unknown",
    ]
    planned_skill: str


class MonteCarloRunResult(BaseModel):
    """Completed Monte Carlo history, optionally protected by a history lock."""

    model_config = ConfigDict(frozen=True)

    session: str
    history: str
    status: str
    lock: MaestroHistoryLockResult | None = None


class MonteCarloExportResult(BaseModel):
    """Result of exporting a Monte Carlo history to remote CSV files."""

    model_config = ConfigDict(frozen=True)

    session: str
    history: str
    output_path: str
    test: str | None = None
    corner: str | None = None


def _require_name(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a non-empty string.")
    normalized = value.strip()
    if not normalized or any(ord(char) < 0x20 or ord(char) == 0x7F for char in normalized):
        raise ValueError(f"{label} must be a non-empty string.")
    return normalized


def _require_skill_text(value: str, label: str) -> str:
    """Reject controls that cannot safely occur in a SKILL string literal."""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string.")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{label} cannot contain control characters")
    return value


def _skill_string(value: str) -> str:
    return f'"{escape_skill_string(value)}"'


def _module_summary(module_filter: MonteCarloModuleFilter | None) -> str:
    if module_filter is None:
        return ""
    return "#".join(
        "%".join((entry.test, entry.instance, entry.master, entry.kind))
        for entry in module_filter.modules
    )


def _run_option_values(config: MonteCarloConfig) -> dict[str, str]:
    module_filter = config.module_filter
    options = {
        "donominal": "1" if config.nominal else "0",
        "dutsummary": _module_summary(module_filter),
        "ignoreflag": (
            "1" if module_filter and module_filter.mode == "exclude" else "0"
        ),
        "mcmethod": config.variation,
        "mcnumpoints": str(config.points),
        "samplingmode": config.sampling,
        "montecarloseed": "" if config.seed is None else str(config.seed),
        "mcstartingrunnumber": str(config.starting_run),
        "saveallplots": "1" if config.save_all_plots else "0",
        "saveprocess": "1" if config.save_process else "0",
        "savemismatch": "1" if config.save_mismatch else "0",
        "mcnumbins": "" if config.num_bins is None else str(config.num_bins),
    }
    options.update(config.extra_options)
    return options


def _result_errors(result) -> str:
    return "; ".join(getattr(result, "errors", []) or [])


def _transport_outcome_unknown(result) -> bool:
    metadata = getattr(result, "metadata", None)
    if isinstance(metadata, dict):
        if (
            metadata.get("outcome") == "not_started"
            or metadata.get("request_sent") is False
        ):
            return False
        if metadata.get("outcome") == "unknown":
            return True
        if metadata.get("request_handle") and not getattr(result, "ok", False):
            return True
    detail = _result_errors(result).lower()
    return not getattr(result, "ok", False) and any(
        marker in detail for marker in _UNKNOWN_TRANSPORT_MARKERS
    )


def _exception_outcome_unknown(exc: Exception) -> bool:
    for attr in ("result", "virtuoso_result"):
        res = getattr(exc, attr, None)
        if res is not None:
            metadata = getattr(res, "metadata", None)
            if isinstance(metadata, dict) and (
                metadata.get("outcome") == "not_started"
                or metadata.get("request_sent") is False
            ):
                return False
            if _transport_outcome_unknown(res):
                return True
    if isinstance(exc, TimeoutError):
        return True
    detail = str(exc).lower()
    return any(marker in detail for marker in _UNKNOWN_TRANSPORT_MARKERS)


def _options_probe_skill(session: str) -> str:
    escaped_session = escape_skill_string(session)
    return f'''prog((vbProbe vbSdb vbOptions vbNames vbRows vbName vbHandle
                      vbValue vbMode)
  unless(isCallable('axlGetMainSetupDB)
    return(list("{_OPTIONS_TAG}" "unsupported" "axlGetMainSetupDB" nil)))
  unless(isCallable('axlGetRunOptions)
    return(list("{_OPTIONS_TAG}" "unsupported" "axlGetRunOptions" nil)))
  unless(isCallable('axlGetRunOption)
    return(list("{_OPTIONS_TAG}" "unsupported" "axlGetRunOption" nil)))
  unless(isCallable('axlGetRunOptionValue)
    return(list("{_OPTIONS_TAG}" "unsupported" "axlGetRunOptionValue" nil)))
  unless(isCallable('maeGetCurrentRunMode)
    return(list("{_OPTIONS_TAG}" "unsupported" "maeGetCurrentRunMode" nil)))
  vbProbe=errset(axlGetMainSetupDB("{escaped_session}"))
  unless(vbProbe && car(vbProbe)
    return(list("{_OPTIONS_TAG}" "session_not_found" "{escaped_session}" nil)))
  vbSdb=car(vbProbe)
  vbProbe=errset(maeGetCurrentRunMode(?session "{escaped_session}"))
  unless(vbProbe && car(vbProbe)
    return(list("{_OPTIONS_TAG}" "probe_error" "maeGetCurrentRunMode" nil)))
  vbMode=car(vbProbe)
  vbProbe=errset(axlGetRunOptions(vbSdb "{MC_RUN_MODE}"))
  unless(vbProbe
    return(list("{_OPTIONS_TAG}" "probe_error" "axlGetRunOptions" nil)))
  vbOptions=car(vbProbe)
  unless(vbOptions && listp(vbOptions) && length(vbOptions) >= 2
    return(list("{_OPTIONS_TAG}" "not_configured" vbMode nil)))
  vbNames=cadr(vbOptions)
  unless(listp(vbNames)
    return(list("{_OPTIONS_TAG}" "malformed" "run_option_names" nil)))
  vbRows=nil
  foreach(vbName vbNames
    vbProbe=errset(axlGetRunOption(vbSdb "{MC_RUN_MODE}" vbName))
    unless(vbProbe && car(vbProbe)
      return(list("{_OPTIONS_TAG}" "probe_error" vbName nil)))
    vbHandle=car(vbProbe)
    vbProbe=errset(axlGetRunOptionValue(vbHandle))
    unless(vbProbe
      return(list("{_OPTIONS_TAG}" "probe_error" vbName nil)))
    vbValue=car(vbProbe)
    vbRows=cons(list(vbName vbValue) vbRows))
  return(list("{_OPTIONS_TAG}" "ok" vbMode reverse(vbRows)))
)'''


def _decode_int(value: str, label: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise MaestroMonteCarloError(
            f"Monte Carlo option {label} must be an integer, got {value!r}."
        ) from exc


def _decode_bool(value: str, label: str) -> bool:
    if value == "1":
        return True
    if value == "0":
        return False
    raise MaestroMonteCarloError(
        f"Monte Carlo option {label} must be '0' or '1', got {value!r}."
    )


def _parse_modules(summary: str) -> tuple[MonteCarloModule, ...]:
    if not summary:
        return ()
    modules: list[MonteCarloModule] = []
    entries = summary.split("#")
    # IC6.1.8 canonicalizes dutsummary as a # terminated record list.
    if entries[-1] == "":
        entries.pop()
    if not entries or any(not entry for entry in entries):
        raise MaestroMonteCarloError("Malformed dutSummary run option.")
    for entry in entries:
        fields = entry.split("%")
        if len(fields) != 4:
            raise MaestroMonteCarloError("Malformed dutSummary run option.")
        try:
            modules.append(MonteCarloModule(
                test=fields[0], instance=fields[1], master=fields[2], kind=fields[3],
            ))
        except Exception as exc:
            raise MaestroMonteCarloError(f"Invalid dutSummary entry: {entry!r}.") from exc
    return tuple(modules)


def _config_from_rows(rows: list) -> MonteCarloConfig:
    options: dict[str, str] = {}
    original_names: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 2:
            raise MaestroMonteCarloError("Malformed Monte Carlo run-option row.")
        name, value = row
        if not isinstance(name, str) or not name or not isinstance(value, str):
            raise MaestroMonteCarloError("Malformed Monte Carlo run-option name/value.")
        normalized = name.lower()
        if normalized in options:
            raise MaestroMonteCarloError(f"Duplicate Monte Carlo run option {name!r}.")
        options[normalized] = value
        original_names[normalized] = name

    def option(name: str) -> str:
        return options.get(name, _DEFAULTS.get(name, ""))

    variation = option("mcmethod")
    module_filter = None
    if variation != "global":
        summary = option("dutsummary")
        modules = _parse_modules(summary)
        if modules:
            module_filter = MonteCarloModuleFilter(
                mode="exclude" if option("ignoreflag") == "1" else "include",
                modules=modules,
            )

    raw_seed = option("montecarloseed")
    try:
        return MonteCarloConfig(
            variation=variation,
            points=_decode_int(option("mcnumpoints"), "mcnumpoints"),
            sampling=option("samplingmode"),
            seed=(
                _decode_int(raw_seed, "montecarloseed")
                if raw_seed.strip() else None
            ),
            starting_run=_decode_int(
                option("mcstartingrunnumber"), "mcstartingrunnumber",
            ),
            nominal=_decode_bool(option("donominal"), "donominal"),
            save_all_plots=_decode_bool(option("saveallplots"), "saveallplots"),
            save_process=_decode_bool(option("saveprocess"), "saveprocess"),
            save_mismatch=_decode_bool(option("savemismatch"), "savemismatch"),
            num_bins=(
                _decode_int(option("mcnumbins"), "mcnumbins")
                if option("mcnumbins") else None
            ),
            module_filter=module_filter,
            extra_options={
                original_names[name]: value
                for name, value in options.items()
                if name not in _KNOWN_OPTIONS
            },
        )
    except MaestroMonteCarloError:
        raise
    except Exception as exc:
        raise MaestroMonteCarloError("Invalid Maestro Monte Carlo run options.") from exc


def _parse_options_payload(raw: str) -> tuple[str, MonteCarloConfig | None]:
    value = parse_sexpr(raw)
    if not isinstance(value, list) or len(value) != 4 or value[0] != _OPTIONS_TAG:
        raise MaestroMonteCarloError("Malformed Monte Carlo options response.")
    status, mode, rows = value[1:]
    if not isinstance(mode, str) or not mode:
        raise MaestroMonteCarloError("Malformed Monte Carlo run mode.")
    if status == "not_configured":
        return mode, None
    if status != "ok":
        raise MaestroMonteCarloError(
            f"Monte Carlo options probe failed: {status}: {mode}"
        )
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise MaestroMonteCarloError("Malformed Monte Carlo run-option list.")
    return mode, _config_from_rows(rows)


def get_monte_carlo_config(
    client: "VirtuosoClient", session: str, *, timeout: float = 30,
) -> MonteCarloConfig | None:
    """Read MC run options from one explicit Maestro setup database."""
    session = _require_name(session, "session")
    result = client.execute_skill(_options_probe_skill(session), timeout=timeout)
    if not getattr(result, "ok", False) or getattr(result, "errors", []):
        raise MaestroMonteCarloError(
            "Monte Carlo options probe execution failed"
            + (f": {_result_errors(result)}" if _result_errors(result) else ".")
        )
    _, config = _parse_options_payload((getattr(result, "output", "") or "").strip())
    return config


def _mutable_session_state(
    client: "VirtuosoClient", session: str, *, timeout: float,
) -> MaestroSessionState:
    states = list_session_states(client, timeout=timeout)
    matches = [state for state in states if state.session == session]
    if len(matches) != 1:
        raise MaestroMonteCarloError(
            f"Session {session!r} must map to exactly one Maestro window before MC changes."
        )
    state = matches[0]
    if state.context != "gui":
        raise MaestroMonteCarloError(f"Session {session!r} is not a Maestro GUI session.")
    if state.access != "editing":
        raise MaestroMonteCarloError(f"Session {session!r} is not open in editing mode.")
    if state.unsaved is not False:
        raise MaestroMonteCarloError(
            "The target Maestro session has unsaved or unknown setup changes; save or discard "
            "them before applying Monte Carlo configuration."
        )
    return state


def _configuration_matches(actual: MonteCarloConfig, requested: MonteCarloConfig) -> bool:
    fields = set(MonteCarloConfig.model_fields) - {"extra_options"}
    if not all(getattr(actual, field) == getattr(requested, field) for field in fields):
        return False
    actual_extra = {name.lower(): value for name, value in actual.extra_options.items()}
    return all(
        actual_extra.get(name.lower()) == value
        for name, value in requested.extra_options.items()
    )


def _mutation_skill(session: str, config: MonteCarloConfig, *, save: bool) -> str:
    sq = _skill_string(session)
    lines = [
        "prog((vbProbe vbSdb vbActive vbHandle)",
        "  unless(isCallable('maeSetCurrentRunMode)",
        f'    return(list("{_MUTATION_TAG}" "unsupported" "maeSetCurrentRunMode")))',
        "  unless(isCallable('axlGetMainSetupDB)",
        f'    return(list("{_MUTATION_TAG}" "unsupported" "axlGetMainSetupDB")))',
        "  unless(isCallable('axlGetActiveSetup)",
        f'    return(list("{_MUTATION_TAG}" "unsupported" "axlGetActiveSetup")))',
        "  unless(isCallable('axlPutRunOption)",
        f'    return(list("{_MUTATION_TAG}" "unsupported" "axlPutRunOption")))',
        "  unless(isCallable('axlSetRunOptionValue)",
        f'    return(list("{_MUTATION_TAG}" "unsupported" "axlSetRunOptionValue")))',
        f'  vbProbe=errset(maeSetCurrentRunMode(?runMode "{MC_RUN_MODE}" '
        f'?session {sq}))',
        "  unless(vbProbe && car(vbProbe)",
        f'    return(list("{_MUTATION_TAG}" "run_mode_error" '
        '"maeSetCurrentRunMode")))',
        f"  vbProbe=errset(axlGetMainSetupDB({sq}))",
        "  unless(vbProbe && car(vbProbe)",
        f'    return(list("{_MUTATION_TAG}" "setup_error" "axlGetMainSetupDB")))',
        "  vbSdb=car(vbProbe)",
        "  vbProbe=errset(axlGetActiveSetup(vbSdb))",
        "  unless(vbProbe && car(vbProbe)",
        f'    return(list("{_MUTATION_TAG}" "setup_error" "axlGetActiveSetup")))',
        "  vbActive=car(vbProbe)",
    ]
    for name, value in _run_option_values(config).items():
        option_name = _skill_string(name)
        lines.extend([
            f'  vbProbe=errset(axlPutRunOption(vbActive "{MC_RUN_MODE}" {option_name}))',
            "  unless(vbProbe && car(vbProbe)",
            f'    return(list("{_MUTATION_TAG}" "put_error" {option_name})))',
            "  vbHandle=car(vbProbe)",
            f'  vbProbe=errset(axlSetRunOptionValue(vbHandle {_skill_string(value)}))',
            "  unless(vbProbe && car(vbProbe)",
            f'    return(list("{_MUTATION_TAG}" "set_error" {option_name})))',
        ])
    if save:
        lines.extend([
            f"  vbProbe=errset(maeSaveSetup(?session {sq}))",
            "  unless(vbProbe && car(vbProbe)",
            f'    return(list("{_MUTATION_TAG}" "save_error" "maeSaveSetup")))',
        ])
    lines.extend([
        f'  return(list("{_MUTATION_TAG}" "ok" t))',
        ")",
    ])
    return "\n".join(lines)


def _parse_tagged_ok(raw: str, tag: str, operation: str) -> None:
    value = parse_sexpr(raw)
    if not isinstance(value, list) or len(value) != 3 or value[0] != tag:
        raise MaestroMonteCarloError(f"Malformed {operation} response.")
    if value[1] != "ok":
        raise MaestroMonteCarloError(
            f"{operation} failed: {value[1]}: {value[2]}"
        )


def configure_monte_carlo(
    client: "VirtuosoClient",
    config: MonteCarloConfig,
    *,
    session: str,
    dry_run: bool = False,
    save: bool = True,
    timeout: float = 30,
) -> MonteCarloConfigureResult:
    """Apply and read back MC run options for one explicit Maestro session."""
    session = _require_name(session, "session")
    if not isinstance(config, MonteCarloConfig):
        config = MonteCarloConfig.model_validate(config)
    before = get_monte_carlo_config(client, session, timeout=timeout)
    before_run_mode = _get_run_mode(client, session, timeout=timeout)
    if before is not None and _configuration_matches(before, config) \
            and before_run_mode == MC_RUN_MODE:
        return MonteCarloConfigureResult(
            session=session,
            requested=config,
            before=before,
            after=before,
            before_run_mode=before_run_mode,
            after_run_mode=before_run_mode,
            changed=False,
            saved=False,
            outcome="already_satisfied",
            planned_skill=_mutation_skill(session, config, save=save),
        )
    _mutable_session_state(client, session, timeout=timeout)
    mutation_skill = _mutation_skill(session, config, save=save)
    if dry_run:
        return MonteCarloConfigureResult(
            session=session,
            requested=config,
            before=before,
            before_run_mode=before_run_mode,
            changed=True,
            saved=False,
            outcome="dry_run",
            planned_skill=mutation_skill,
        )

    mutation = None
    mutation_exception: Exception | None = None
    try:
        mutation = client.execute_skill(
            mutation_skill, timeout=timeout, retry_connect=False,
        )
    except Exception as exc:
        mutation_exception = exc

    uncertain = mutation_exception is not None or (
        mutation is not None and _transport_outcome_unknown(mutation)
    )
    if mutation is not None and not uncertain:
        if not getattr(mutation, "ok", False) or getattr(mutation, "errors", []):
            raise MaestroMonteCarloError(
                "Monte Carlo configuration execution failed"
                + (f": {_result_errors(mutation)}" if _result_errors(mutation) else ".")
            )
        _parse_tagged_ok(
            (getattr(mutation, "output", "") or "").strip(),
            _MUTATION_TAG,
            "Monte Carlo configuration",
        )

    try:
        after = get_monte_carlo_config(client, session, timeout=timeout)
        after_run_mode = _get_run_mode(client, session, timeout=timeout)
    except Exception as exc:
        raise MaestroMonteCarloOutcomeUnknown(
            "Monte Carlo configuration request was sent, but post-state could not be verified."
        ) from exc

    matches = (
        after is not None
        and _configuration_matches(after, config)
        and after_run_mode == MC_RUN_MODE
    )
    if uncertain:
        detail = str(mutation_exception) if mutation_exception else _result_errors(mutation)
        if not matches:
            raise MaestroMonteCarloOutcomeUnknown(
                "Monte Carlo configuration outcome is unknown after transport failure; "
                "the observed live setup does not match the request"
                + (f": {detail}" if detail else ".")
            )
        if save:
            raise MaestroMonteCarloOutcomeUnknown(
                "Monte Carlo configuration is visible in the live session after a transport "
                f"failure, but disk persistence is unknown: {detail}"
            )
    if not matches:
        raise MaestroMonteCarloError(
            "Monte Carlo configuration post-state does not match the requested values."
        )
    return MonteCarloConfigureResult(
        session=session,
        requested=config,
        before=before,
        after=after,
        before_run_mode=before_run_mode,
        after_run_mode=after_run_mode,
        changed=True,
        saved=save,
        outcome="confirmed_after_unknown" if uncertain else "applied",
        planned_skill=mutation_skill,
    )


def _get_run_mode(client: "VirtuosoClient", session: str, *, timeout: float) -> str:
    expression = f'''prog((vbProbe)
  unless(isCallable('maeGetCurrentRunMode)
    return(list("{_RUN_MODE_TAG}" "unsupported" "maeGetCurrentRunMode")))
  vbProbe=errset(maeGetCurrentRunMode(?session {_skill_string(session)}))
  unless(vbProbe && car(vbProbe)
    return(list("{_RUN_MODE_TAG}" "probe_error" "maeGetCurrentRunMode")))
  return(list("{_RUN_MODE_TAG}" "ok" car(vbProbe)))
)'''
    result = client.execute_skill(expression, timeout=timeout)
    if not getattr(result, "ok", False) or getattr(result, "errors", []):
        raise MaestroMonteCarloError(
            "Could not read Maestro run mode"
            + (f": {_result_errors(result)}" if _result_errors(result) else ".")
        )
    value = parse_sexpr((getattr(result, "output", "") or "").strip())
    if (not isinstance(value, list) or len(value) != 3
            or value[0] != _RUN_MODE_TAG or value[1] != "ok"
            or not isinstance(value[2], str)):
        raise MaestroMonteCarloError("Malformed Maestro run-mode response.")
    return value[2]


def run_monte_carlo_and_wait(
    client: "VirtuosoClient",
    *,
    session: str,
    timeout: int = 600,
    lock_result: bool = False,
) -> MonteCarloRunResult:
    """Run the configured MC setup, wait for completion, and optionally lock it."""
    session = _require_name(session, "session")
    mode = _get_run_mode(client, session, timeout=min(timeout, 30))
    if mode != MC_RUN_MODE:
        raise MaestroMonteCarloError(
            f"Session {session!r} is in run mode {mode!r}, not {MC_RUN_MODE!r}."
        )
    try:
        raw_history, status = run_and_wait(
            client, session=session, run_mode=MC_RUN_MODE, timeout=timeout,
        )
    except Exception as exc:
        if _exception_outcome_unknown(exc):
            raise MaestroMonteCarloOutcomeUnknown(
                "The Monte Carlo start or completion acknowledgement was lost; the run "
                "may have started. Inspect histories before deciding whether to run again."
            ) from exc
        raise
    history = raw_history.strip().strip('"')
    if not history:
        raise MaestroMonteCarloError("Monte Carlo run returned an empty history name.")
    lock = None
    if lock_result:
        lock = lock_history(client, history, session=session, timeout=min(timeout, 30))
    return MonteCarloRunResult(
        session=session,
        history=history,
        status=status,
        lock=lock,
    )


def export_monte_carlo_results(
    client: "VirtuosoClient",
    history: str,
    output_path: str,
    *,
    session: str,
    test: str | None = None,
    corner: str | None = None,
    timeout: float = 30,
) -> MonteCarloExportResult:
    """Export scalar MC results to one CSV per corner on the Virtuoso host."""
    session = _require_name(session, "session")
    history = _require_name(history, "history")
    output_path = _require_name(output_path, "output_path")
    parts = [
        f"axlWriteMonteCarloResultsCSV({_skill_string(session)}",
        _skill_string(history),
    ]
    if test:
        parts.extend(("?testName", _skill_string(_require_name(test, "test"))))
    if corner:
        parts.extend(("?cornerName", _skill_string(_require_name(corner, "corner"))))
    # IC6.1.8's installed reference table says ``?outputName``, but the
    # executable API and the example on that same page require
    # ``?outputPath``.  The latter is also the semantically correct keyword.
    parts.extend(("?outputPath", _skill_string(output_path)))
    call = " ".join(parts) + ")"
    expression = f'''prog((vbProbe)
  unless(isCallable('axlWriteMonteCarloResultsCSV)
    return(list("{_EXPORT_TAG}" "unsupported" "axlWriteMonteCarloResultsCSV")))
  vbProbe=errset({call})
  unless(vbProbe
    return(list("{_EXPORT_TAG}" "export_error" "axlWriteMonteCarloResultsCSV")))
  unless(car(vbProbe)
    return(list("{_EXPORT_TAG}" "returned_nil" "axlWriteMonteCarloResultsCSV")))
  return(list("{_EXPORT_TAG}" "ok" t))
)'''
    result = client.execute_skill(expression, timeout=timeout)
    if not getattr(result, "ok", False) or getattr(result, "errors", []):
        raise MaestroMonteCarloError(
            "Monte Carlo CSV export failed"
            + (f": {_result_errors(result)}" if _result_errors(result) else ".")
        )
    _parse_tagged_ok(
        (getattr(result, "output", "") or "").strip(),
        _EXPORT_TAG,
        "Monte Carlo CSV export",
    )
    return MonteCarloExportResult(
        session=session,
        history=history,
        output_path=output_path,
        test=test,
        corner=corner,
    )


__all__ = [
    "MC_RUN_MODE",
    "MaestroMonteCarloError",
    "MaestroMonteCarloOutcomeUnknown",
    "MonteCarloConfig",
    "MonteCarloConfigureResult",
    "MonteCarloExportResult",
    "MonteCarloModule",
    "MonteCarloModuleFilter",
    "MonteCarloRunResult",
    "configure_monte_carlo",
    "export_monte_carlo_results",
    "get_monte_carlo_config",
    "run_monte_carlo_and_wait",
]
