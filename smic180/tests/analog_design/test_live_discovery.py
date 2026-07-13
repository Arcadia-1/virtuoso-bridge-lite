import pytest

from analog_design.technology.discovery import DiscoveryError, DiscoveryRequest, discover_technology


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
