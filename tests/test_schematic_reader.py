from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

from virtuoso_bridge.virtuoso.schematic.reader import (
    _parse_schematic,
    read_connectivity,
    read_instance_params,
    read_placement,
    read_schematic,
)
from virtuoso_bridge.virtuoso.schematic import SchematicOps


def test_read_schematic_raises_on_skill_error() -> None:
    class Client:
        def execute_skill(self, skill: str, timeout: int = 300):
            return SimpleNamespace(output="", errors=["*Error* sprintf: argument #3 should be a number"])

    with pytest.raises(RuntimeError, match="read_schematic SKILL error"):
        read_schematic(Client(), "LIB", "CELL", param_filters=None)


def test_read_schematic_forwards_timeout() -> None:
    class Client:
        timeout: int | None = None

        def execute_skill(self, skill: str, timeout: int = 300):
            self.timeout = timeout
            return SimpleNamespace(output="INSTANCES\nNETS\nPINS\nEND\n", errors=[])

    client = Client()

    read_schematic(client, "LIB", "CELL", param_filters=None, timeout=123)

    assert client.timeout == 123


def test_named_read_closes_its_cellview_in_unwind_cleanup() -> None:
    class Client:
        skill: str | None = None

        def execute_skill(self, skill: str, timeout: int = 300):
            self.skill = skill
            return SimpleNamespace(output="INSTANCES\nNETS\nPINS\nEND\n", errors=[])

    client = Client()

    read_schematic(client, "LIB", "CELL", param_filters=None)

    assert client.skill is not None
    assert 'dbOpenCellViewByType("LIB" "CELL" "schematic" "schematic" "r")' in client.skill
    assert "unwindProtect(" in client.skill
    assert "when(cv" in client.skill
    assert "dbClose(cv)" in client.skill
    assert client.skill.index("unwindProtect(") < client.skill.index(
        "dbOpenCellViewByType("
    )
    assert client.skill.index('result = strcat(result "END\\n")') < client.skill.index(
        "dbClose(cv)"
    )


def test_current_cellview_read_does_not_close_caller_owned_cellview() -> None:
    class Client:
        skill: str | None = None

        def execute_skill(self, skill: str, timeout: int = 300):
            self.skill = skill
            return SimpleNamespace(output="INSTANCES\nNETS\nPINS\nEND\n", errors=[])

    client = Client()

    read_schematic(client, param_filters=None)

    assert client.skill is not None
    assert "cv = geGetEditCellView()" in client.skill
    assert "unwindProtect(" not in client.skill
    assert "dbClose(cv)" not in client.skill


@pytest.mark.parametrize(
    ("reader", "output"),
    [
        (read_placement, "INSTANCES\nPINS\nLABELS\nWIRES\nEND\n"),
        (read_connectivity, "INSTANCES\nNETS\nPINS\nEND\n"),
        (read_instance_params, ""),
    ],
)
def test_legacy_named_readers_close_their_cellview_in_unwind_cleanup(
    reader: Callable[..., object],
    output: str,
) -> None:
    class Client:
        skill: str | None = None

        def execute_skill(self, skill: str, timeout: int = 30):
            self.skill = skill
            return SimpleNamespace(output=output, errors=[])

    client = Client()

    reader(client, "LIB", "CELL")

    assert client.skill is not None
    assert 'dbOpenCellViewByType("LIB" "CELL" "schematic" "schematic" "r")' in client.skill
    assert "unwindProtect(" in client.skill
    assert "when(cv" in client.skill
    assert "dbClose(cv)" in client.skill
    assert client.skill.index("unwindProtect(") < client.skill.index(
        "dbOpenCellViewByType("
    )
    assert client.skill.index("dbOpenCellViewByType(") < client.skill.index("dbClose(cv)")


@pytest.mark.parametrize(
    ("reader", "output"),
    [
        (read_placement, "INSTANCES\nPINS\nLABELS\nWIRES\nEND\n"),
        (read_connectivity, "INSTANCES\nNETS\nPINS\nEND\n"),
        (read_instance_params, ""),
    ],
)
def test_legacy_current_cellview_readers_do_not_close_caller_owned_cellview(
    reader: Callable[..., object],
    output: str,
) -> None:
    class Client:
        skill: str | None = None

        def execute_skill(self, skill: str, timeout: int = 30):
            self.skill = skill
            return SimpleNamespace(output=output, errors=[])

    client = Client()

    reader(client)

    assert client.skill is not None
    assert "cv = geGetEditCellView()" in client.skill
    assert "unwindProtect(" not in client.skill
    assert "dbClose(cv)" not in client.skill


@pytest.mark.parametrize(
    ("reader", "operation"),
    [
        (read_placement, "read_placement"),
        (read_connectivity, "read_connectivity"),
        (read_instance_params, "read_instance_params"),
    ],
)
def test_legacy_readers_surface_skill_cleanup_errors(
    reader: Callable[..., object],
    operation: str,
) -> None:
    class Client:
        def execute_skill(self, skill: str, timeout: int = 30):
            return SimpleNamespace(output="", errors=["schematic reader close failed"])

    with pytest.raises(RuntimeError, match=rf"{operation} SKILL error"):
        reader(Client(), "LIB", "CELL")


@pytest.mark.parametrize(
    ("reader", "operation"),
    [
        (read_placement, "read_placement"),
        (read_connectivity, "read_connectivity"),
        (read_instance_params, "read_instance_params"),
    ],
)
def test_legacy_readers_reject_open_failure_sentinel(
    reader: Callable[..., object],
    operation: str,
) -> None:
    class Client:
        def execute_skill(self, skill: str, timeout: int = 30):
            return SimpleNamespace(output='"ERROR"', errors=[])

    with pytest.raises(RuntimeError, match=rf"{operation} could not open schematic"):
        reader(Client(), "LIB", "MISSING")


@pytest.mark.parametrize(
    "reader",
    [read_schematic, read_placement, read_connectivity, read_instance_params],
)
@pytest.mark.parametrize(
    ("lib", "cell"),
    [
        ("LIB_ONLY", None),
        (None, "CELL_ONLY"),
        ("", "CELL_ONLY"),
        ("LIB_ONLY", ""),
        ("", ""),
    ],
)
def test_readers_reject_incomplete_named_target(
    reader: Callable[..., object],
    lib: str | None,
    cell: str | None,
) -> None:
    class Client:
        def execute_skill(self, skill: str, timeout: int = 300):
            raise AssertionError("partial targets must fail before SKILL execution")

    with pytest.raises(
        ValueError,
        match="lib and cell must both be non-empty when provided",
    ):
        reader(Client(), lib, cell)


def test_client_bound_read_delegates_to_unified_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    owner = object()

    def fake_read(client: object, lib: str, cell: str, **kwargs: object) -> dict[str, object]:
        captured["client"] = client
        captured["lib"] = lib
        captured["cell"] = cell
        captured["kwargs"] = kwargs
        return {"instances": []}

    monkeypatch.setattr(
        "virtuoso_bridge.virtuoso.schematic.reader.read_schematic",
        fake_read,
    )

    result = SchematicOps(owner).read("LIB", "CELL", include_positions=True, timeout=123)

    assert result == {"instances": []}
    assert captured == {
        "client": owner,
        "lib": "LIB",
        "cell": "CELL",
        "kwargs": {"include_positions": True, "timeout": 123},
    }


def test_read_schematic_raises_on_empty_output() -> None:
    class Client:
        def execute_skill(self, skill: str, timeout: int = 300):
            return SimpleNamespace(output="", errors=[])

    with pytest.raises(RuntimeError, match="returned empty output"):
        read_schematic(Client(), "LIB", "CELL", param_filters=None)


def test_parse_schematic_defaults_non_numeric_widths() -> None:
    raw = """
INSTANCES
INST|I222<1:14>|FIRAS|LB_FCT_cunit
TERM|CINP|<*14>CINP
NETS
NET|FCT_NTUNE_D<2:0>|nil|signal|nil|I222<1:14>.CINP
PINS
PIN|FCT_NTUNE_D<2:0>|inputOutput|nil
END
"""

    data = _parse_schematic(raw, include_positions=False, filter_config=None)

    assert data["instances"][0]["name"] == "I222<1:14>"
    assert data["instances"][0]["terms"] == {"CINP": "<*14>CINP"}
    assert data["nets"]["FCT_NTUNE_D<2:0>"]["numBits"] == 1
    assert data["pins"]["FCT_NTUNE_D<2:0>"]["numBits"] == 1


def _skill_string(value: str) -> str:
    """Model %L string printing; these payloads are synthetic, not PDK captures."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'


class _ReadClient:
    def __init__(self, payload: str, *, quoted: bool = True) -> None:
        self.output = _skill_string(payload) if quoted else payload
        self.calls = 0

    def execute_skill(self, *_args, **_kwargs):
        self.calls += 1
        return SimpleNamespace(output=self.output, errors=[])


@pytest.mark.parametrize("payload", [
    "nil",
    "INSTANCES\nINST|M2|demoPdk|pmos\n",
    "INSTANCES\nNETS\nEND\n",
    "NETS\nINSTANCES\nPINS\nEND\n",
    "INSTANCES\nNETS\nNETS\nPINS\nEND\n",
    "INSTANCES\nNETS\nPINS\nEND\nunexpected trailing data\n",
    "INSTANCES\nINST|M2\nNETS\nPINS\nEND\n",
    "INSTANCES\nPARAM|w|\"2u\"\nNETS\nPINS\nEND\n",
    "INSTANCES\nNETS\nTERM|D|out\nPINS\nEND\n",
    "INSTANCES\nINST|M2|demoPdk|pmos\nPARAM|w|\"unterminated\nNETS\nPINS\nEND\n",
])
def test_read_rejects_incomplete_or_malformed_payload_without_retry(payload: str) -> None:
    client = _ReadClient(payload)

    with pytest.raises(RuntimeError, match="malformed or incomplete"):
        SchematicOps(client).read(param_filters=None)

    assert client.calls == 1


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("value", [
    'iPar("simM")',
    'pPar("w") * 2',
    '"quoted text"',
    r'C:\models\new\device.scs',
    'line one\nline two\twith a | separator',
    '偏置参数',
])
def test_read_preserves_cdf_string_values(value: str, quoted: bool) -> None:
    payload = (
        "INSTANCES\nINST|M2|demoPdk|pmos\n"
        f"PARAM|expression|{_skill_string(value)}\n"
        "NETS\nPINS\nEND\n"
    )
    client = _ReadClient(payload, quoted=quoted)

    data = SchematicOps(client).read(param_filters=None)

    assert data["instances"][0]["params"]["expression"] == value
    assert client.calls == 1


def test_read_accepts_a_complete_empty_schematic() -> None:
    data = SchematicOps(_ReadClient("INSTANCES\nNETS\nPINS\nNOTES\nEND\n")).read()

    assert data == {"instances": [], "nets": {}, "pins": {}, "notes": []}


def test_read_preserves_quoted_notes_and_their_geometry() -> None:
    note = 'Input | output\n"quoted" annotation'
    payload = (
        "INSTANCES\nNETS\nPINS\nNOTES\n"
        f"NOTE|{_skill_string(note)}|(1.0 2.0)|stick|0.1|R0|lowerCenter\nEND\n"
    )

    data = SchematicOps(_ReadClient(payload)).read(include_positions=True)

    assert data["notes"] == [{
        "text": note, "xy": [1.0, 2.0], "font": "stick", "height": 0.1,
        "orient": "R0", "justify": "lowerCenter",
    }]


def _evidence_payload(evidence: str) -> str:
    return (
        "INSTANCES\nINST|M2|demoPdk|pmos\n"
        f"PARAM_EVIDENCE|{evidence}\nNETS\nPINS\nEND\n"
    )


def test_read_distinguishes_unstored_cdf_values_from_explicit_properties() -> None:
    expression = 'iPar("simM")'
    payload = (
        "INSTANCES\nINST|M2|demoPdk|pmos\n"
        f'PARAM_EVIDENCE|(nil t (("w" "200n") ("m" {_skill_string(expression)})))\n'
        'PARAM|w|"200n"\n'
        "INST|M3|demoPdk|pmos\n"
        'PARAM_EVIDENCE|((("w" "2u")) t (("w" "2u")))\n'
        'PARAM|w|"2u"\nNETS\nPINS\nEND\n'
    )
    client = _ReadClient(payload)

    data = SchematicOps(client).read(include_param_evidence=True)

    first, second = data["instances"]
    assert first["params"] == {"w": "200n"}
    assert first["param_evidence"] == {"stored": {}, "cdf": {"w": "200n", "m": expression}}
    assert second["param_evidence"] == {"stored": {"w": "2u"}, "cdf": {"w": "2u"}}
    assert client.calls == 1


@pytest.mark.parametrize(("evidence", "expected"), [
    ("(nil nil nil)", {"stored": {}, "cdf": None}),
    ("(nil t nil)", {"stored": {}, "cdf": {}}),
    ('((("w" nil)) t (("w" "") ("enabled" t)))',
     {"stored": {"w": None}, "cdf": {"w": "", "enabled": True}}),
    ('((("custom" "present")) nil nil)', {"stored": {"custom": "present"}, "cdf": None}),
])
def test_read_distinguishes_missing_cdf_empty_cdf_and_explicit_nil(evidence, expected) -> None:
    data = SchematicOps(_ReadClient(_evidence_payload(evidence))).read(include_param_evidence=True)

    assert data["instances"][0]["param_evidence"] == expected


def test_parameter_evidence_preserves_long_values_independent_of_param_filters(tmp_path) -> None:
    filters = tmp_path / "filters.yaml"
    filters.write_text('filters: []\nfallback: [w]\n')
    long_value = 'pPar("bias") | ' + "x" * 160
    payload = _evidence_payload(
        f'((("note" {_skill_string(long_value)})) t (("note" {_skill_string(long_value)}) ("w" "2u")))'
    ).replace("NETS\n", 'PARAM|w|"2u"\nPARAM|hidden|"present"\nNETS\n')

    data = SchematicOps(_ReadClient(payload)).read(
        include_param_evidence=True, param_filters=filters,
    )

    inst = data["instances"][0]
    assert inst["params"] == {"w": "2u"}
    assert inst["param_evidence"]["stored"]["note"] == long_value
    assert inst["param_evidence"]["cdf"] == {"note": long_value, "w": "2u"}


@pytest.mark.parametrize("evidence", [
    "nil", '(nil t (("w" "2u"))', '(nil "yes" nil)',
    '(nil nil (("w" "2u")))', '(nil t (("w")))',
    '(nil t (("w" "1u") ("w" "2u")))', '(nil t ("not-a-pair"))',
])
def test_requested_parameter_evidence_rejects_incomplete_or_ambiguous_data(evidence) -> None:
    with pytest.raises(RuntimeError, match="malformed or incomplete"):
        SchematicOps(_ReadClient(_evidence_payload(evidence))).read(include_param_evidence=True)


def test_requested_parameter_evidence_must_be_present_for_every_instance() -> None:
    payload = _evidence_payload("(nil t nil)").replace("NETS\n", "INST|M3|demoPdk|pmos\nNETS\n")

    with pytest.raises(RuntimeError, match="missing requested parameter evidence"):
        SchematicOps(_ReadClient(payload)).read(include_param_evidence=True)


def test_parameter_evidence_is_opt_in_and_read_only() -> None:
    from virtuoso_bridge.virtuoso.skill_output import is_single_complete_skill_list

    class Client(_ReadClient):
        skill = ""

        def execute_skill(self, skill, **kwargs):
            self.skill = skill
            return super().execute_skill(skill, **kwargs)

    client = Client(_evidence_payload("(nil t nil)"))
    default = SchematicOps(client).read()
    assert "param_evidence" not in default["instances"][0]
    assert "PARAM_EVIDENCE|" not in client.skill

    SchematicOps(client).read(include_param_evidence=True)
    assert "PARAM_EVIDENCE|" in client.skill
    assert "{param_evidence}" not in client.skill
    assert is_single_complete_skill_list("(" + client.skill + ")")
    for mutation in ("cdfUpdateInstParam", "evalstring", "dbSave", "dbReplaceProp", "schCheck"):
        assert mutation not in client.skill
