"""Generate OSCAL selection profiles from an existing SCuBA catalog."""

# pylint: disable=wrong-import-position

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scubagoggles.scuba_constants import DEFAULT_OSCAL_VERSION


JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonObject: TypeAlias = dict[str, JsonValue]

SCUBA_NS = "https://cisa.gov/scuba"
SUMMARY_FILE = "profile-generation-summary.json"
PROFILE_RULES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "bod-25-01": ("BOD 25-01", "bod-25-01-requirement", ("true",)),
    "automated": ("Automated Checks", "automated-check", ("true",)),
    "manual": ("Manual Checks", "manual-check", ("true",)),
    "configurable": ("Configurable", "configurable", ("true",)),
    "log-based": ("Log-Based Checks", "log-based-check", ("true",)),
    "required": (
        "Required",
        "recommendation-level",
        ("SHALL", "SHALL NOT", "MUST", "MUST NOT", "REQUIRED"),
    ),
    "recommended": (
        "Recommended",
        "recommendation-level",
        ("SHOULD", "SHOULD NOT", "RECOMMENDED", "NOT RECOMMENDED"),
    ),
}


def require_object(value: JsonValue, label: str) -> JsonObject:
    """Require a JSON object."""

    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def require_string(value: JsonValue, label: str) -> str:
    """Require a nonempty JSON string."""

    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string")
    return value


def iter_controls(node: JsonObject) -> Iterator[JsonObject]:
    """Yield controls in source order, including nested groups and controls."""

    for key in ("controls", "groups"):
        children = node.get(key, [])
        if not isinstance(children, list):
            raise ValueError(f"{key} must be an array")
        for child in children:
            item = require_object(child, key)
            if key == "controls":
                yield item
            yield from iter_controls(item)


def select_control_ids(
    catalog: JsonObject, property_name: str, accepted_values: tuple[str, ...]
) -> list[str]:
    """Select controls by an explicit SCuBA property without changing content."""

    selected = []
    seen = set()
    for control in iter_controls(catalog):
        control_id = require_string(control.get("id"), "control.id")
        if control_id in seen:
            raise ValueError(f"Duplicate control ID: {control_id}")
        seen.add(control_id)
        props = control.get("props", [])
        if not isinstance(props, list):
            raise ValueError(f"{control_id}.props must be an array")
        values = [
            prop.get("value")
            for prop in props
            if isinstance(prop, dict)
            and prop.get("name") == property_name
            and prop.get("ns") == SCUBA_NS
        ]
        if len(values) != 1:
            raise ValueError(f"{control_id} must have one SCuBA {property_name} property")
        value = require_string(values[0], f"{control_id}.{property_name}")
        if accepted_values == ("true",) and value not in ("true", "false"):
            raise ValueError(f"{control_id}.{property_name} must be true or false")
        if value in accepted_values:
            selected.append(control_id)
    if not selected:
        raise ValueError(f"No controls match {property_name}: {accepted_values}")
    return selected


def make_prop(name: str, value: str) -> dict[str, str]:
    """Create a SCuBA property in the catalog's readable field order."""

    return {"name": name, "value": value, "ns": SCUBA_NS}


def relative_file_uri(path: Path, base_dir: Path) -> str:
    """Reference a local file relative to a directory when the drive permits."""

    try:
        return quote(Path(os.path.relpath(path, base_dir)).as_posix())
    except ValueError:
        return path.as_uri()


def build_profile(catalog: JsonObject, source_href: str, name: str) -> JsonObject:
    """Build one native OSCAL profile referencing the source catalog."""

    if name not in PROFILE_RULES:
        raise ValueError(f"Unknown profile: {name}")
    metadata = require_object(catalog.get("metadata"), "catalog.metadata")
    oscal_version = require_string(metadata.get("oscal-version"), "oscal-version")
    if oscal_version != DEFAULT_OSCAL_VERSION:
        raise ValueError(
            f"Catalog uses OSCAL {oscal_version}; expected {DEFAULT_OSCAL_VERSION}"
        )
    source_version = require_string(metadata.get("version"), "catalog.metadata.version")
    source_uuid = require_string(catalog.get("uuid"), "catalog.uuid")
    title, property_name, accepted_values = PROFILE_RULES[name]
    selected = select_control_ids(catalog, property_name, accepted_values)
    props = [
        make_prop("source-catalog-uuid", source_uuid),
        make_prop("source-catalog-version", source_version),
        make_prop("selection-property", property_name),
        *[make_prop("selection-value", value) for value in accepted_values],
        make_prop("selected-policy-count", str(len(selected))),
    ]
    return {
        "profile": {
            "uuid": str(uuid.uuid5(uuid.UUID(source_uuid), f"scuba-profile:{name}")),
            "metadata": {
                "title": f"Google Workspace SCuBA {title} Profile",
                "last-modified": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "version": source_version,
                "oscal-version": oscal_version,
                "props": props,
                "links": [
                    {"href": source_href, "rel": "source", "text": "Source SCuBA catalog"}
                ],
            },
            "imports": [
                {"href": source_href, "include-controls": [{"with-ids": selected}]}
            ],
            "merge": {"as-is": True},
        }
    }


def generate_profiles(
    catalog_path: Path, output_dir: Path, profile_names: list[str] | None = None
) -> JsonObject:
    """Generate selected profiles and a summary without parsing Markdown."""

    names = list(PROFILE_RULES) if profile_names is None else profile_names
    if not names or len(names) != len(set(names)):
        raise ValueError("Select at least one profile, without duplicate names")
    catalog_path = catalog_path.resolve(strict=True)
    output_dir = output_dir.resolve()
    document = require_object(
        json.loads(catalog_path.read_text(encoding="utf-8")), "document"
    )
    catalog = require_object(document.get("catalog"), "catalog")
    source_href = relative_file_uri(catalog_path, output_dir)
    profiles = [(name, build_profile(catalog, source_href, name)) for name in names]
    entries = []
    for name, profile in profiles:
        file_name = f"scubagoggles-oscal-profile-{name}-oscal-{DEFAULT_OSCAL_VERSION}.json"
        if (output_dir / file_name).resolve() == catalog_path:
            raise ValueError("Profile output must not overwrite the source catalog")
        entries.append({"name": name, "output": file_name})
    output_dir.mkdir(parents=True, exist_ok=True)
    for (_, profile), entry in zip(profiles, entries):
        (output_dir / entry["output"]).write_text(
            json.dumps(profile, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
        )
    metadata = require_object(catalog.get("metadata"), "catalog.metadata")
    summary: JsonObject = {
        "source-catalog": source_href,
        "source-catalog-uuid": catalog["uuid"],
        "source-catalog-version": metadata["version"],
        "oscal-version": DEFAULT_OSCAL_VERSION,
        "profiles": entries,
    }
    (output_dir / SUMMARY_FILE).write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True, help="Source OSCAL catalog.")
    parser.add_argument(
        "--output-dir", type=Path, default=REPO_ROOT / "dist" / "oscal-profiles"
    )
    parser.add_argument(
        "--profile", action="append", choices=list(PROFILE_RULES), dest="profiles",
        help="Select a profile; repeat for several. The default generates all seven.",
    )
    return parser.parse_args()


def main() -> None:
    """Generate profiles from an existing catalog."""

    args = parse_args()
    summary = generate_profiles(args.catalog, args.output_dir, args.profiles)
    for entry in summary["profiles"]:
        print(f"Wrote {entry['output']}")
    print(f"Wrote profile summary to {args.output_dir / SUMMARY_FILE}")


if __name__ == "__main__":
    main()
