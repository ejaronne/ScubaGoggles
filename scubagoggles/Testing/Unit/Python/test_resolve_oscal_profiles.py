"""Tests for CLI-backed SCuBA profile resolution and content preservation."""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from scubagoggles.scuba_constants import DEFAULT_OSCAL_VERSION


UTILITY_DIR = Path(__file__).resolve().parents[4] / "scubagoggles" / "utils"
SCUBA_NS = "https://cisa.gov/scuba"


def load_module(name):
    """Load a standalone utility by path."""

    spec = importlib.util.spec_from_file_location(name, UTILITY_DIR / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="resolver")
def resolver_fixture():
    """Provide the CLI wrapper."""

    return load_module("resolve_oscal_profiles.py")


@pytest.fixture(name="scenario")
def scenario_fixture(tmp_path):
    """Create a source, profile, and simulated CLI-normalized selection."""

    controls = [{
        "id": name, "title": name,
        "props": [{"name": "recommendation-level", "value": level, "ns": SCUBA_NS}],
        "parts": [{"id": name + "_statement", "name": "statement",
                   "prose": "Original \u2013 text\n\n > a quoted requirement"}],
        "links": [{"href": "#control-resource", "rel": "reference"}],
    } for name, level in (("one", "SHALL"), ("two", "SHOULD"))]
    controls[0]["props"].append({
        "name": "nist-sp800-53-rev5-fedramp-high-mapping",
        "value": "IA-2(1), IA-5c", "ns": SCUBA_NS,
    })
    source = {
        "uuid": "1a21eb2b-368d-5781-84c9-2ccadb555670",
        "metadata": {
            "version": "v1.0.0", "oscal-version": DEFAULT_OSCAL_VERSION,
            "roles": [{"id": "creator", "title": "Creator"}],
            "remarks": "Original source attribution.",
        },
        "groups": [{
            "id": "baseline", "title": "Baseline",
            "parts": [{"name": "overview", "prose": "Shared prerequisites. " * 200}],
            "links": [{"href": "#group-resource", "rel": "source"}],
            "controls": controls,
        }],
        "back-matter": {"resources": [
            {"uuid": "control-resource", "title": "Control resource"},
            {"uuid": "group-resource", "links": [{"href": "#related-resource"}]},
            {"uuid": "related-resource", "title": "Referenced by another resource"},
            {"uuid": "unused-resource", "title": "Unused"},
        ]},
    }
    catalog_path = tmp_path / "source.json"
    catalog_path.write_text(json.dumps({"catalog": source}), encoding="utf-8")
    profile_dir = tmp_path / "profiles"
    generator = load_module("generate_oscal_profiles.py")
    summary = generator.generate_profiles(catalog_path, profile_dir, ["required"])
    profile_path = profile_dir / summary["profiles"][0]["output"]
    profile = json.loads(profile_path.read_text(encoding="utf-8"))["profile"]
    resolved = copy.deepcopy(source)
    resolved["groups"][0]["controls"] = [copy.deepcopy(controls[0])]
    resolved["groups"][0]["controls"][0]["parts"][0]["prose"] = "normalized"
    resolved["groups"][0]["parts"][0]["prose"] = "normalized"
    resolved["groups"][0].pop("links")
    resolved.pop("back-matter")
    resolved["metadata"] = {
        "title": profile["metadata"]["title"], "version": "v1.0.0",
        "oscal-version": DEFAULT_OSCAL_VERSION,
        "props": [{"name": "resolution-tool", "value": "OSCAL CLI"}],
    }
    return {
        "source": source, "catalog_path": catalog_path, "profile_dir": profile_dir,
        "profile_path": profile_path, "profile": profile, "resolved": resolved,
    }


def test_resolution_preserves_selected_content(resolver, scenario):
    """Keep exact policy prose, long shared context, and original property order."""

    result = resolver.preserve_content(
        scenario["source"], scenario["profile"], scenario["resolved"]
    )
    source_group = scenario["source"]["groups"][0]
    assert result["groups"][0]["controls"] == [source_group["controls"][0]]
    assert result["groups"][0]["parts"] == source_group["parts"]
    assert list(result["groups"][0]["controls"][0]["parts"][0]) == (
        list(source_group["controls"][0]["parts"][0])
    )
    assert result["metadata"]["roles"] == scenario["source"]["metadata"]["roles"]
    assert result["metadata"]["remarks"] == "Original source attribution."
    assert {item["uuid"] for item in result["back-matter"]["resources"]} == {
        "control-resource", "group-resource", "related-resource"
    }
    resolver.check_local_references(result)


@pytest.mark.parametrize("case", ["extra", "missing", "duplicate"])
def test_incorrect_cli_selections_fail(resolver, scenario, case):
    """Never publish a catalog with omitted, extra, or duplicate policies."""

    controls = scenario["resolved"]["groups"][0]["controls"]
    if case == "missing":
        controls.clear()
    elif case == "duplicate":
        controls.append(copy.deepcopy(controls[0]))
    else:
        controls.append(copy.deepcopy(scenario["source"]["groups"][0]["controls"][1]))
    with pytest.raises(ValueError, match="profile selection"):
        resolver.preserve_content(scenario["source"], scenario["profile"], scenario["resolved"])


def test_changed_hierarchy_fails(resolver, scenario):
    """Controls must remain under their original groups."""

    scenario["resolved"]["controls"] = scenario["resolved"].pop("groups")[0]["controls"]
    with pytest.raises(ValueError, match="moved or added"):
        resolver.preserve_content(scenario["source"], scenario["profile"], scenario["resolved"])


@pytest.mark.parametrize("case", ["uuid", "version", "oscal-version"])
def test_mismatched_source_fails(resolver, scenario, case):
    """A profile must still refer to the source identity and version it records."""

    if case == "oscal-version":
        scenario["resolved"]["metadata"]["oscal-version"] = "0.0.0"
    elif case == "version":
        scenario["source"]["metadata"]["version"] = "other"
    else:
        scenario["source"]["uuid"] = "other"
    with pytest.raises(ValueError):
        resolver.preserve_content(scenario["source"], scenario["profile"], scenario["resolved"])


@pytest.mark.parametrize("href", ["https://example.test/catalog.json", "//server/file", "#x"])
def test_nonlocal_imports_fail(resolver, scenario, href):
    """The wrapper is limited to the generated profiles' local source catalogs."""

    scenario["profile"]["imports"][0]["href"] = href
    with pytest.raises(ValueError, match="local source"):
        resolver.source_catalog_path(scenario["profile"], scenario["profile_path"])


def test_content_modifications_are_not_silently_discarded(resolver, scenario):
    """Content-changing tailoring is outside this selection-only wrapper."""

    scenario["profile"]["modify"] = {"alters": []}
    with pytest.raises(ValueError, match="no modify"):
        resolver.source_catalog_path(scenario["profile"], scenario["profile_path"])


def test_dangling_references_fail(resolver):
    """An unresolved internal reference must not pass the completeness check."""

    with pytest.raises(ValueError, match="Unresolved internal"):
        resolver.check_local_references({"links": [{"href": "#missing"}]})


def test_cli_pipeline_and_summary(resolver, scenario, tmp_path, monkeypatch):
    """Validate the profile, resolve it, validate the output, and publish a summary."""

    calls = []

    def run_cli(args, check):
        assert check is True
        calls.append(args)
        if args[1] == "resolve-profile":
            Path(args[-1]).write_text(
                json.dumps({"catalog": scenario["resolved"]}), encoding="utf-8"
            )

    monkeypatch.setattr(resolver.subprocess, "run", run_cli)
    output = tmp_path / "tailored"
    summary = resolver.resolve_profiles(scenario["profile_dir"], output, "test-cli")
    assert [args[1] for args in calls] == ["validate", "resolve-profile", "validate"]
    assert summary["catalogs"][0]["policies"] == 1
    result = json.loads((output / summary["catalogs"][0]["output"]).read_text())["catalog"]
    assert result["metadata"]["title"].endswith("Required Tailored Catalog")
    assert result["metadata"]["links"][0]["rel"] == "source-profile"
    assert result["metadata"]["links"][0]["href"].startswith("../profiles/")
    assert (output / resolver.SUMMARY_FILE).exists()


def test_cli_failure_does_not_publish_catalog(resolver, scenario, tmp_path, monkeypatch):
    """A failed validation must leave no apparently successful output catalog."""

    def run_cli(args, check):
        assert check is True
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(resolver.subprocess, "run", run_cli)
    output = tmp_path / "tailored"
    with pytest.raises(subprocess.CalledProcessError):
        resolver.resolve_profiles(scenario["profile_dir"], output, "test-cli")
    assert not list(output.glob("*.json"))


def test_later_failure_does_not_publish_a_partial_set(
    resolver, scenario, tmp_path, monkeypatch
):
    """One successful resolution must not be published when another one fails."""

    generator = load_module("generate_oscal_profiles.py")
    generator.generate_profiles(
        scenario["catalog_path"], scenario["profile_dir"], ["required", "recommended"]
    )
    calls = []

    def run_cli(args, check):
        assert check is True
        calls.append(args)
        if len(calls) == 4:
            raise subprocess.CalledProcessError(1, args)
        if args[1] == "resolve-profile":
            Path(args[-1]).write_text(
                json.dumps({"catalog": scenario["resolved"]}), encoding="utf-8"
            )

    monkeypatch.setattr(resolver.subprocess, "run", run_cli)
    output = tmp_path / "tailored"
    with pytest.raises(subprocess.CalledProcessError):
        resolver.resolve_profiles(scenario["profile_dir"], output, "test-cli")
    assert len(calls) == 4
    assert not list(output.glob("*.json"))


def test_profile_directory_traversal_is_rejected(resolver, scenario, tmp_path):
    """A summary cannot send the CLI to a file outside the profile directory."""

    summary_path = scenario["profile_dir"] / resolver.PROFILE_SUMMARY_FILE
    summary = json.loads(summary_path.read_text())
    summary["profiles"][0]["output"] = "../source.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ValueError, match="profile directory"):
        resolver.resolve_profiles(scenario["profile_dir"], tmp_path / "tailored", "test-cli")


def test_publish_catalog_replaces_content_without_leaving_staging_files(resolver, tmp_path):
    """Publishing should work for both new and existing output files."""

    staged = tmp_path / "staged.json"
    destination = tmp_path / "catalog.json"
    staged.write_bytes(b'{"catalog": {}}\n')
    destination.write_bytes(b"old")
    resolver.publish_catalog(staged, destination)
    assert destination.read_bytes() == staged.read_bytes()
    resolver.publish_catalog(destination, destination)
    assert destination.read_bytes() == staged.read_bytes()
    assert not list(tmp_path.glob(".publish-*.tmp"))


def test_failed_publication_keeps_existing_output(resolver, tmp_path, monkeypatch):
    """A failed replacement must not truncate the old catalog or leave a partial file."""

    staged = tmp_path / "staged.json"
    destination = tmp_path / "catalog.json"
    staged.write_bytes(b"new")
    destination.write_bytes(b"old")

    def fail_replace(_self, _target):
        raise OSError("replacement failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement failed"):
        resolver.publish_catalog(staged, destination)
    assert destination.read_bytes() == b"old"
    assert not list(tmp_path.glob(".publish-*.tmp"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL regression")
@pytest.mark.parametrize("repair_existing", [False, True])
def test_published_catalog_inherits_windows_permissions(resolver, tmp_path, repair_existing):
    """Private staging permissions must not prevent the user from reading output."""

    output = tmp_path / "output"
    output.mkdir()
    destination = output / "catalog.json"
    with tempfile.TemporaryDirectory(dir=tmp_path) as private_dir:
        staged = Path(private_dir) / "staged.json"
        staged.write_bytes(b'{"catalog": {}}\n')
        if repair_existing:
            staged.replace(destination)
            staged = destination
        resolver.publish_catalog(staged, destination)
    result = subprocess.run(
        ["icacls", str(destination)],
        check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert "(I)" in result.stdout
    assert destination.read_bytes() == b'{"catalog": {}}\n'
