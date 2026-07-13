import pytest

from analog_design.technology.discovery import (
    DiscoveryError,
    DiscoveryRequest,
    VirtuosoDiscoveryClient,
    discover_technology,
)


class FakeDiscoveryClient:
    def __init__(self, *, roots=None, devices=None):
        self.roots = roots or []
        self.devices = devices or {}
        self.calls = []

    def existing_paths(self, paths):
        self.calls.append(("paths", tuple(paths)))
        return [path for path in paths if path in self.roots]

    def probe_device(self, master_ref, candidates):
        self.calls.append(("device", master_ref, tuple(candidates)))
        return self.devices.get(master_ref)


def request():
    return DiscoveryRequest(
        pdk_roots=("/home/IC/Tech/smic18ee_2", "/home/IC/Tech/smic18ee_2P6M_20100810"),
        cds_lib_candidates=("/home/IC/Tech/smic18ee_2/cds.lib", "/home/IC/Tech/smic18ee_2P6M_20100810/cds.lib"),
        device_candidates={
            "smic180.core_nmos": (("smic18", "nmos", "symbol"),),
            "smic180.core_pmos": (("smic18", "pmos", "symbol"),),
        },
    )


def complete_probe(device_class):
    return {
        "device_class": device_class,
        "library": "smic18",
        "cell": "nmos" if device_class == "mos.nmos" else "pmos",
        "view": "symbol",
        "terminals": ["D", "G", "S", "B"],
        "parameter_map": {"width": "w", "length": "l", "fingers": "nf", "multiplier": "m"},
        "parameter_dimensions": {"width": "length", "length": "length", "fingers": "integer", "multiplier": "integer"},
        "evidence": {"master": "master.json", "terminals": "terminals.json", "cdf": "roundtrip.json"},
    }


def test_discovery_reports_pdk_root_conflict_instead_of_selecting_silently():
    client = FakeDiscoveryClient(roots=list(request().pdk_roots) + list(request().cds_lib_candidates))
    with pytest.raises(DiscoveryError, match="multiple PDK roots"):
        discover_technology(client, request())


def test_discovery_requires_one_resolved_root_and_cds_lib():
    client = FakeDiscoveryClient(roots=[])
    with pytest.raises(DiscoveryError, match="PDK root"):
        discover_technology(client, request())


def test_discovery_refuses_confirmation_when_device_evidence_is_incomplete():
    roots = [request().pdk_roots[0], request().cds_lib_candidates[0]]
    client = FakeDiscoveryClient(roots=roots, devices={"smic180.core_nmos": complete_probe("mos.nmos")})
    with pytest.raises(DiscoveryError, match="core_pmos"):
        discover_technology(client, request())


def test_discovery_builds_confirmed_profile_from_complete_live_evidence():
    roots = [request().pdk_roots[0], request().cds_lib_candidates[0]]
    devices = {"smic180.core_nmos": complete_probe("mos.nmos"), "smic180.core_pmos": complete_probe("mos.pmos")}
    profile = discover_technology(FakeDiscoveryClient(roots=roots, devices=devices), request())
    assert profile.state == "confirmed"
    assert profile.evidence["pdk_root"] == request().pdk_roots[0]
    assert profile.resolve("smic180.core_nmos").cdf_parameter("width") == "w"


def test_plan_only_returns_queries_without_touching_client():
    client = FakeDiscoveryClient()
    plan = discover_technology(client, request(), plan_only=True)
    assert plan["pdk_roots"] == list(request().pdk_roots)
    assert "smic180.core_nmos" in plan["device_candidates"]
    assert client.calls == []


class FakeResult:
    def __init__(self, output, errors=()):
        self.output = output
        self.errors = list(errors)


class FakeVirtuosoBridge:
    def __init__(self):
        self.calls = []

    def execute_skill(self, expression, timeout=30):
        self.calls.append((expression, timeout))
        if "n33e2r" in expression:
            return FakeResult(
                "MASTER|smic18ee|n33e2r|symbol\n"
                "TERMINALS|D,G,B,S\n"
                "SPECTRE_TERMINALS|D,G,S,B\n"
                "MODEL|n33e2r\n"
                "CDF|w|600n|smic18mm_mosCB( 'w )\n"
                "CDF|fw|600n|smic18mm_mosCB( 'fw )\n"
                "CDF|l|600n|smic18mm_mosCB( 'l )\n"
                "CDF|fingers|1|smic18mm_mosCB( 'fingers )\n"
                "CDF|m|1|smic18mm_mosCB( 'm )\n"
            )
        return FakeResult("")


def test_virtuoso_discovery_client_combines_live_metadata_with_roundtrip_evidence(tmp_path):
    roundtrip = {
        "smic180.core_nmos": {
            "evidence_file": "roundtrip_nmos.json",
            "cdf_readback": {"w": "2.4u", "fw": "600n", "l": "600n", "fingers": "4", "m": "1"},
            "netlist_model": "n33e2r",
            "netlist_terminals": ["D", "G", "S", "B"],
            "netlist_parameter_map": {"finger_width": "w", "length": "l", "effective_multiplier": "m"},
            "parameter_relations": {"width": "finger_width*fingers", "effective_multiplier": "multiplier*fingers"},
            "limits": {"minimum_length": 600e-9, "minimum_finger_width": 600e-9},
        }
    }
    client = VirtuosoDiscoveryClient(FakeVirtuosoBridge(), tmp_path, roundtrip)
    probe = client.probe_device("smic180.core_nmos", (("smic18ee", "n33e2r", "symbol"),))
    assert probe["library"] == "smic18ee"
    assert probe["terminals"] == ["D", "G", "B", "S"]
    assert probe["parameter_map"]["finger_width"] == "fw"
    assert probe["netlist_terminals"] == ["D", "G", "S", "B"]
    assert probe["parameter_relations"]["effective_multiplier"] == "multiplier*fingers"
    assert probe["limits"]["minimum_length"] == pytest.approx(600e-9)
    assert (tmp_path / "smic180.core_nmos.master.json").is_file()


def test_virtuoso_discovery_client_refuses_device_without_roundtrip_evidence(tmp_path):
    client = VirtuosoDiscoveryClient(FakeVirtuosoBridge(), tmp_path, {})
    assert client.probe_device("smic180.core_nmos", (("smic18ee", "n33e2r", "symbol"),)) is None

def test_virtuoso_discovery_uses_getq_for_spectre_property_list(tmp_path):
    bridge = FakeVirtuosoBridge()
    client = VirtuosoDiscoveryClient(bridge, tmp_path, {
        "smic180.core_nmos": {
            "evidence_file": "roundtrip.json",
            "netlist_model": "n33e2r",
            "netlist_terminals": ["D", "G", "S", "B"],
        }
    })
    client.probe_device("smic180.core_nmos", (("smic18ee", "n33e2r", "symbol"),))
    expression = bridge.calls[0][0]
    assert "getq(sim termOrder)" in expression
    assert "sim~>termOrder" not in expression
