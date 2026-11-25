"""Tests for generating SCuBA OSCAL selection profiles."""

import copy
import importlib.util
import json
import re
from pathlib import Path
from urllib.parse import unquote

import pytest

from scubagoggles.scuba_constants import DEFAULT_OSCAL_VERSION


REPO_ROOT = Path(__file__).resolve().parents[4]
UTILITY_DIR = REPO_ROOT / "scubagoggles" / "utils"
SCUBA_NS = "https://cisa.gov/scuba"
REPRESENTATIVE_POLICY_ID = "gws.commoncontrols.1.1v1"
BADGE_PROPERTIES = (
    "bod-25-01-requirement", "automated-check", "manual-check",
    "configurable", "log-based-check",
)


def load_module(file_name):
    """Load an independent utility by path."""

    spec = importlib.util.spec_from_file_location(file_name, UTILITY_DIR / file_name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="generator")
def profile_generator_fixture():
    """Provide the profile generator."""

    return load_module("generate_oscal_profiles.py")


@pytest.fixture(name="catalog")
def catalog_fixture():
    """Provide four policies with different flags and requirement levels."""

    def control(control_id, level, flags):
        props = [
            {"name": name, "value": "true" if name in flags else "false", "ns": SCUBA_NS}
            for name in BADGE_PROPERTIES
        ]
        props.append({"name": "recommendation-level", "value": level, "ns": SCUBA_NS})
        return {"id": control_id, "title": control_id, "props": props}

    return {
        "uuid": "1a21eb2b-368d-5781-84c9-2ccadb555670",
        "metadata": {"version": "v1.0.0", "oscal-version": DEFAULT_OSCAL_VERSION},
        "groups": [{
            "id": "baseline",
            "groups": [{
                "id": "section",
                "controls": [
                    control("one", "SHALL", {
                        "bod-25-01-requirement", "automated-check", "configurable"
                    }),
                    control("two", "SHALL NOT", {
                        "bod-25-01-requirement", "manual-check"
                    }),
                    control("three", "SHOULD", {"automated-check", "log-based-check"}),
                    control("four", "SHOULD NOT", {"manual-check"}),
                ],
            }],
        }],
    }


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("bod-25-01", ["one", "two"]),
        ("automated", ["one", "three"]),
        ("manual", ["two", "four"]),
        ("configurable", ["one"]),
        ("log-based", ["three"]),
        ("required", ["one", "two"]),
        ("recommended", ["three", "four"]),
    ],
)
def test_profiles_select_only_matching_controls(generator, catalog, name, expected):
    """Each profile should include matches and exclude all other controls."""

    original = copy.deepcopy(catalog)
    profile = generator.build_profile(catalog, "../catalog.json", name)["profile"]
    assert profile["imports"] == [{
        "href": "../catalog.json", "include-controls": [{"with-ids": expected}]
    }]
    assert profile["merge"] == {"as-is": True}
    assert profile["metadata"]["version"] == "v1.0.0"
    assert profile["metadata"]["oscal-version"] == DEFAULT_OSCAL_VERSION
    assert "modify" not in profile
    assert "groups" not in profile
    assert catalog == original


def test_profile_identity_is_stable(generator, catalog):
    """Repeated generation should retain the profile's source-based identity."""

    first = generator.build_profile(catalog, "catalog.json", "required")["profile"]
    second = generator.build_profile(catalog, "catalog.json", "required")["profile"]
    other = generator.build_profile(catalog, "catalog.json", "recommended")["profile"]
    assert first["uuid"] == second["uuid"]
    assert first["uuid"] != other["uuid"]
    assert {p["value"] for p in first["metadata"]["props"]
            if p["name"] == "source-catalog-uuid"} == {catalog["uuid"]}


def test_manual_selection_is_not_the_inverse_of_automated(generator, catalog):
    """Manual selection must honor its own badge even when flags overlap."""

    controls = catalog["groups"][0]["groups"][0]["controls"]
    for control, value in ((controls[0], "true"), (controls[3], "false")):
        for prop in control["props"]:
            if prop["name"] == "manual-check":
                prop["value"] = value
    profile = generator.build_profile(catalog, "catalog.json", "manual")["profile"]
    assert profile["imports"][0]["include-controls"][0]["with-ids"] == ["one", "two"]


def test_nested_controls_are_selected(generator, catalog):
    """Nested controls should be selectable without changing their IDs."""

    controls = catalog["groups"][0]["groups"][0]["controls"]
    controls[0]["controls"] = [controls.pop(1)]
    profile = generator.build_profile(catalog, "catalog.json", "required")["profile"]
    assert profile["imports"][0]["include-controls"][0]["with-ids"] == ["one", "two"]


@pytest.mark.parametrize("level", ["MUST", "MUST NOT", "REQUIRED"])
def test_required_profile_includes_synonyms(generator, catalog, level):
    """Equivalent mandatory keywords should retain their requirement strength."""

    catalog["groups"][0]["groups"][0]["controls"][0]["props"][-1]["value"] = level
    profile = generator.build_profile(catalog, "catalog.json", "required")["profile"]
    assert profile["imports"][0]["include-controls"][0]["with-ids"] == ["one", "two"]


@pytest.mark.parametrize("case", ["missing", "duplicate", "namespace", "invalid"])
def test_invalid_badge_properties_are_rejected(generator, catalog, case):
    """Missing or ambiguous metadata must not silently produce incomplete profiles."""

    props = catalog["groups"][0]["groups"][0]["controls"][0]["props"]
    if case == "missing":
        props.pop(0)
    elif case == "duplicate":
        props.append(copy.deepcopy(props[0]))
    elif case == "namespace":
        props[0]["ns"] = "https://example.test/other"
    else:
        props[0]["value"] = "unknown"
    with pytest.raises(ValueError, match="bod-25-01-requirement"):
        generator.build_profile(catalog, "catalog.json", "bod-25-01")


def test_duplicate_control_ids_are_rejected(generator, catalog):
    """Ambiguous control identities should fail before files are written."""

    controls = catalog["groups"][0]["groups"][0]["controls"]
    controls[1]["id"] = controls[0]["id"]
    with pytest.raises(ValueError, match="Duplicate control ID"):
        generator.build_profile(catalog, "catalog.json", "required")


def test_source_version_must_match_configuration(generator, catalog):
    """Profiles must not relabel an older source catalog as the current model."""

    catalog["metadata"]["oscal-version"] = "0.0.0"
    with pytest.raises(ValueError, match="expected"):
        generator.build_profile(catalog, "catalog.json", "required")


def test_empty_selection_is_rejected(generator, catalog):
    """An empty selection should fail rather than emit invalid include-controls."""

    with pytest.raises(ValueError, match="No controls match"):
        generator.select_control_ids(catalog, "recommendation-level", ("MAY",))


@pytest.mark.parametrize("names", [None, ["required"], ["manual", "required"]])
def test_generate_profiles_keeps_imports_relative(generator, catalog, tmp_path, names):
    """Profiles should reference the exact source file using an escaped relative URI."""

    source = tmp_path / "catalog inputs" / "catalog.json"
    source.parent.mkdir()
    source.write_text(json.dumps({"catalog": catalog}), encoding="utf-8")
    output = tmp_path / "profiles"
    summary = generator.generate_profiles(source, output, names)
    assert [entry["name"] for entry in summary["profiles"]] == (
        list(generator.PROFILE_RULES) if names is None else names
    )
    assert (output / generator.SUMMARY_FILE).exists()
    assert "%20" in summary["source-catalog"]
    assert (output / unquote(summary["source-catalog"])).resolve() == source.resolve()
    for entry in summary["profiles"]:
        profile = json.loads((output / entry["output"]).read_text())["profile"]
        assert DEFAULT_OSCAL_VERSION in entry["output"]
        assert profile["imports"][0]["href"] == summary["source-catalog"]


@pytest.mark.parametrize("names", [[], ["required", "required"], ["unknown"]])
def test_invalid_profile_names_do_not_write_files(generator, catalog, tmp_path, names):
    """Invalid requests should not leave partially generated profile sets."""

    source = tmp_path / "catalog.json"
    source.write_text(json.dumps({"catalog": catalog}), encoding="utf-8")
    output = tmp_path / "profiles"
    with pytest.raises(ValueError):
        generator.generate_profiles(source, output, names)
    assert not output.exists()


def test_generation_covers_real_catalog(generator, tmp_path):
    """All seven profiles should select the correct IDs from every baseline."""

    catalog_generator = load_module("generate_oscal_baselines.py")
    source_dir = tmp_path / "catalog"
    source_summary = catalog_generator.generate_baselines(
        REPO_ROOT / "scubagoggles" / "baselines", source_dir
    )
    source = source_dir / source_summary["output"]
    catalog = json.loads(source.read_text())["catalog"]
    controls = list(generator.iter_controls(catalog))
    assert any(
        part["id"] == REPRESENTATIVE_POLICY_ID + "_statement"
        and part["name"] == "statement"
        for control in controls if control["id"] == REPRESENTATIVE_POLICY_ID
        for part in control["parts"]
    )
    output = tmp_path / "profiles"
    summary = generator.generate_profiles(source, output)
    for entry in summary["profiles"]:
        _, property_name, values = generator.PROFILE_RULES[entry["name"]]
        expected = [
            control["id"] for control in controls
            if any(prop["name"] == property_name and prop["value"] in values
                   and prop["ns"] == SCUBA_NS for prop in control["props"])
        ]
        profile = json.loads((output / entry["output"]).read_text())["profile"]
        assert profile["imports"][0]["include-controls"][0]["with-ids"] == expected
        assert (REPRESENTATIVE_POLICY_ID in expected) == (
            entry["name"] in {"bod-25-01", "automated", "required"}
        )


def test_documented_json_samples_preserve_policy_content(generator):
    """Documentation should contain parseable, internally consistent real examples."""

    document = (REPO_ROOT / "docs/misc/oscal-profiles.md").read_text(encoding="utf-8")
    examples = [json.loads(block) for block in re.findall(r"```json\s*\n(.*?)```", document, re.S)]
    assert len(examples) == 2
    profile = examples[0]["profile"]
    excerpt = examples[1]["catalog"]
    selected = profile["imports"][0]["include-controls"][0]["with-ids"]
    assert REPRESENTATIVE_POLICY_ID in selected
    assert next(prop["value"] for prop in profile["metadata"]["props"]
                if prop["name"] == "selected-policy-count") == str(len(selected))
    assert profile["metadata"]["oscal-version"] == DEFAULT_OSCAL_VERSION
    assert excerpt["metadata"]["version"] == profile["metadata"]["version"]
    assert profile["merge"] == {"as-is": True}
    catalog_generator = load_module("generate_oscal_baselines.py")
    source, _ = catalog_generator.build_catalog(REPO_ROOT / "scubagoggles/baselines")
    original = next(control for control in generator.iter_controls(source["catalog"])
                    if control["id"] == REPRESENTATIVE_POLICY_ID)
    shown = next(generator.iter_controls(excerpt))
    assert shown["id"] == original["id"]
    assert shown["title"] == original["title"]
    assert shown["parts"] == original["parts"]
    assert all(prop in original["props"] for prop in shown["props"])
