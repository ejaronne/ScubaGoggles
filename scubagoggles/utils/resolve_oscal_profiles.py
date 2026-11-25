"""Resolve generated SCuBA profiles into tailored catalogs using OSCAL CLI."""

# pylint: disable=wrong-import-position

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

UTILITY_DIR = Path(__file__).resolve().parent
if str(UTILITY_DIR) not in sys.path:
    sys.path.insert(0, str(UTILITY_DIR))

from generate_oscal_profiles import (
    DEFAULT_OSCAL_VERSION, PROFILE_RULES, SCUBA_NS,
    SUMMARY_FILE as PROFILE_SUMMARY_FILE,
    JsonObject, JsonValue, iter_controls, make_prop, relative_file_uri,
    require_object, require_string,
)


SUMMARY_FILE = "tailored-catalog-summary.json"


def read_object(path: Path, key: str | None = None) -> JsonObject:
    """Read a UTF-8 JSON document or one of its top-level objects."""

    document = require_object(json.loads(path.read_text(encoding="utf-8")), str(path))
    return document if key is None else require_object(document.get(key), key)


def source_catalog_path(profile: JsonObject, profile_path: Path) -> Path:
    """Require one local catalog import from a generated selection profile."""

    imports = profile.get("imports")
    if not isinstance(imports, list) or len(imports) != 1 or profile.get("modify"):
        raise ValueError("Expected a selection profile with one import and no modify block")
    imported = require_object(imports[0], "profile.imports[0]")
    href = require_string(imported.get("href"), "profile import href")
    uri = urlsplit(href)
    if uri.query or uri.fragment or uri.netloc or uri.scheme not in ("", "file"):
        raise ValueError("Only local source catalog imports are supported")
    path = Path(url2pathname(uri.path)) if uri.scheme else profile_path.parent / unquote(uri.path)
    return path.resolve(strict=True)


def selected_ids(profile: JsonObject) -> list[str]:
    """Read the explicit selection used by the generated profiles."""

    imported = profile["imports"][0]
    includes = imported.get("include-controls")
    if (not isinstance(includes, list) or len(includes) != 1
            or profile.get("merge") != {"as-is": True}
            or imported.get("exclude-controls")):
        raise ValueError("Expected an explicit with-ids selection and merge.as-is")
    values = require_object(includes[0], "include-controls").get("with-ids")
    if not isinstance(values, list) or not values:
        raise ValueError("Expected nonempty with-ids")
    ids = [require_string(value, "control ID") for value in values]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate selected control IDs")
    return ids


def restore_selected_tree(source: JsonObject, resolved: JsonObject) -> JsonObject:
    """Retain the CLI's selected hierarchy and the source's exact content."""

    restored = copy.deepcopy(source)
    for key in ("groups", "controls"):
        source_children = {child["id"]: child for child in source.get(key, [])}
        children = []
        for child in resolved.get(key, []):
            if child["id"] not in source_children:
                raise ValueError(f"Resolution moved or added {key}: {child['id']}")
            children.append(restore_selected_tree(source_children[child["id"]], child))
        if children:
            restored[key] = children
        else:
            restored.pop(key, None)
    return restored


def iter_objects(value: JsonValue) -> Iterator[JsonObject]:
    """Visit JSON objects for internal reference checks."""

    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_objects(child)


def internal_links(value: JsonValue) -> set[str]:
    """Collect fragment-only links without interpreting source prose."""

    return {
        node["href"][1:] for node in iter_objects(value)
        if isinstance(node.get("href"), str) and node["href"].startswith("#")
    }


def restore_resources(source: JsonObject, resolved: JsonObject) -> None:
    """Include exact source resources needed by retained controls and groups."""

    resources = {
        resource["uuid"]: resource
        for resource in source.get("back-matter", {}).get("resources", [])
    }
    selected = {
        resource["uuid"]
        for resource in resolved.get("back-matter", {}).get("resources", [])
    }
    selected.update(internal_links(resolved) & resources.keys())
    pending = set(selected)
    while pending:
        resource_id = pending.pop()
        if resource_id not in resources:
            raise ValueError(f"Resolved resource is not in the source: {resource_id}")
        added = (internal_links(resources[resource_id]) & resources.keys()) - selected
        selected.update(added)
        pending.update(added)
    if selected:
        resolved["back-matter"] = {
            "resources": [copy.deepcopy(resource) for key, resource in resources.items()
                          if key in selected]
        }
    else:
        resolved.pop("back-matter", None)


def preserve_content(
    source: JsonObject, profile: JsonObject, resolved: JsonObject
) -> JsonObject:
    """Verify selection and keep source text, resources, and traceability."""

    expected = selected_ids(profile)
    actual = [control["id"] for control in iter_controls(resolved)]
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise ValueError("Resolved controls do not match the profile selection")
    retained = restore_selected_tree(source, resolved)
    for key in ("groups", "controls", "parts", "params", "props"):
        if key in retained:
            resolved[key] = retained[key]
        else:
            resolved.pop(key, None)
    restore_resources(source, resolved)
    metadata = require_object(resolved.get("metadata"), "resolved metadata")
    source_metadata = require_object(source.get("metadata"), "source metadata")
    profile_metadata = require_object(profile.get("metadata"), "profile metadata")
    if (metadata.get("oscal-version") != DEFAULT_OSCAL_VERSION
            or source_metadata.get("oscal-version") != DEFAULT_OSCAL_VERSION
            or profile_metadata.get("oscal-version") != DEFAULT_OSCAL_VERSION
            or metadata.get("version") != source_metadata.get("version")):
        raise ValueError("Resolved catalog versions do not match the configured source")
    for name, expected_value in (
        ("source-catalog-uuid", source["uuid"]),
        ("source-catalog-version", source_metadata["version"]),
    ):
        values = [
            prop["value"] for prop in profile_metadata.get("props", [])
            if prop.get("name") == name and prop.get("ns") == SCUBA_NS
        ]
        if values != [expected_value]:
            raise ValueError(f"Profile does not match its source catalog: {name}")
    metadata["title"] = profile_metadata["title"].removesuffix(" Profile") + " Tailored Catalog"
    metadata["props"] = metadata.get("props", []) + copy.deepcopy(profile_metadata["props"])
    metadata["props"].append(make_prop("source-profile-uuid", profile["uuid"]))
    for key in ("roles", "parties", "responsible-parties", "remarks"):
        if key in source_metadata:
            metadata[key] = copy.deepcopy(source_metadata[key])
    return resolved


def check_local_references(catalog: JsonObject) -> None:
    """Reject dangling internal links after unused resources are removed."""

    targets = {
        node[key] for node in iter_objects(catalog) for key in ("id", "uuid") if key in node
    }
    missing = internal_links(catalog) - targets
    if missing:
        raise ValueError(f"Unresolved internal references: {sorted(missing)}")


def resolve_one(profile_path: Path, temporary_path: Path, destination: Path, cli: str) -> int:
    """Delegate selection to OSCAL CLI and validate the complete resulting catalog."""

    profile = read_object(profile_path, "profile")
    source = read_object(source_catalog_path(profile, profile_path), "catalog")
    selected_ids(profile)
    subprocess.run([cli, "validate", str(profile_path)], check=True)
    subprocess.run([
        cli, "resolve-profile", "--as=json", "--to=json", "--pretty-print",
        str(profile_path), str(temporary_path),
    ], check=True)
    resolved = preserve_content(source, profile, read_object(temporary_path, "catalog"))
    metadata = resolved["metadata"]
    metadata["links"] = [
        link for link in metadata.get("links", []) if link.get("rel") != "source-profile"
    ] + [{"href": relative_file_uri(profile_path, destination.parent), "rel": "source-profile"}]
    check_local_references(resolved)
    temporary_path.write_text(
        json.dumps({"catalog": resolved}, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    subprocess.run([cli, "validate", str(temporary_path)], check=True)
    return len(list(iter_controls(resolved)))


def resolve_profiles(profile_dir: Path, output_dir: Path, cli: str = "oscal-cli") -> JsonObject:
    """Resolve a generated profile set, publishing output only when all checks pass."""

    profile_dir = profile_dir.resolve(strict=True)
    output_dir = output_dir.resolve()
    summary = read_object(profile_dir / PROFILE_SUMMARY_FILE)
    entries = summary.get("profiles")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Profile summary must contain profiles")
    names = [entry["name"] for entry in entries]
    if len(names) != len(set(names)) or any(name not in PROFILE_RULES for name in names):
        raise ValueError("Profile summary contains duplicate or unknown names")
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="resolve-", dir=output_dir) as temporary_dir:
        for entry in entries:
            profile_path = (profile_dir / entry["output"]).resolve(strict=True)
            if profile_path.parent != profile_dir:
                raise ValueError("Profile files must be in the profile directory")
            destination = output_dir / (
                f"scubagoggles-oscal-catalog-{entry['name']}-"
                f"oscal-{DEFAULT_OSCAL_VERSION}.json"
            )
            source_path = source_catalog_path(read_object(profile_path, "profile"), profile_path)
            if destination in (source_path, profile_path):
                raise ValueError("Output must not overwrite a profile or source catalog")
            count = resolve_one(
                profile_path, Path(temporary_dir) / destination.name, destination, cli
            )
            results.append({"name": entry["name"], "output": destination.name, "policies": count})
            print(f"Resolved {entry['name']}: {count} policies", flush=True)
        for result in results:
            (Path(temporary_dir) / result["output"]).replace(output_dir / result["output"])
    result_summary: JsonObject = {
        "source-catalog-uuid": summary["source-catalog-uuid"],
        "source-catalog-version": summary["source-catalog-version"],
        "oscal-version": DEFAULT_OSCAL_VERSION,
        "catalogs": results,
    }
    (output_dir / SUMMARY_FILE).write_text(
        json.dumps(result_summary, indent=2) + "\n", encoding="utf-8"
    )
    return result_summary


def main() -> None:
    """Resolve generated profiles with the installed OSCAL CLI."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--oscal-cli", default="oscal-cli", dest="cli")
    args = parser.parse_args()
    resolve_profiles(args.profile_dir, args.output_dir, args.cli)


if __name__ == "__main__":
    main()
