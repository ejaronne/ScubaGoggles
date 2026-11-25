# OSCAL Profiles

The profile generator reads the existing [SCuBA catalog](oscal-catalog.md) and
creates seven OSCAL profiles. It does not parse Markdown again or copy policy
statements into another model. Each profile imports the source catalog and
selects its existing control IDs.

## Selections

| Profile | Source property | Selected values |
| --- | --- | --- |
| BOD 25-01 | `bod-25-01-requirement` | `true` |
| Automated Checks | `automated-check` | `true` |
| Manual Checks | `manual-check` | `true` |
| Configurable | `configurable` | `true` |
| Log-Based Checks | `log-based-check` | `true` |
| Required | `recommendation-level` | `SHALL`, `SHALL NOT`, `MUST`, `MUST NOT`, `REQUIRED` |
| Recommended | `recommendation-level` | `SHOULD`, `SHOULD NOT`, `RECOMMENDED`, `NOT RECOMMENDED` |

Badge-based selections can overlap. A BOD-designated policy can also be automated,
manual, configurable, or log-based. Manual policies are selected using their
explicit badge, not by assuming that every non-automated policy is manual.

Required and Recommended include negative forms and equivalent RFC 2119 terms.
The current catalog uses only SHALL, SHALL NOT, SHOULD, and SHOULD NOT. Optional
requirements, if introduced later, would not belong to either selection.

## Generate Profiles

Run these commands from the repository root:

```text
python scubagoggles/utils/generate_oscal_baselines.py --output-dir dist/oscal-baseline-catalog
python scubagoggles/utils/generate_oscal_profiles.py --catalog dist/oscal-baseline-catalog/scubagoggles-oscal-catalog-oscal-1.2.3.json --output-dir dist/oscal-profiles
```

Use `--profile required`, for example, to generate only that profile. Repeat the
option to request several profiles. Without this option, all seven are generated.
For a release catalog, pass its exact versioned filename to `--catalog`.

The output directory contains the selected profile JSON files and
`profile-generation-summary.json`. No files are uploaded to GitHub by this
script. Profiles with the same name in that directory are overwritten, so use
separate output directories for different source catalog versions.

## Source Traceability

Each profile preserves the source catalog's content version in
`metadata.version`, uses the shared configured OSCAL version, and records the
source catalog UUID, source version, selection rule, and selected-policy count
as SCuBA properties. The generated filename includes the profile name and OSCAL
version. The profile UUID is stable for that source catalog UUID and profile name.

Imports use relative, URI-escaped paths when the catalog and profiles are on the
same filesystem drive. Keep their relative directory layout when distributing
them together. If they are on different Windows drives, the import uses an
absolute file URI and is local to that machine.

Generation fails on missing or ambiguous selection properties, duplicate control
IDs, an empty selection, or a source OSCAL version different from the configured
version. It does not silently infer missing badge values.

## Profile Structure

The profile records selected IDs in `imports[].include-controls[].with-ids` and
uses `merge.as-is` to retain the source catalog's grouping when resolved. Policy
IDs, statement IDs, related NIST references, and baseline and section context
remain in the imported catalog. The profiles do not add or remove policy prose
with `modify` instructions.

A profile is a selection document, not a self-contained tailored catalog.
Resolving it into a tailored catalog is a separate operation.

## Resolve Tailored Catalogs

After generating profiles, resolve the complete set with the installed OSCAL CLI:

```text
python scubagoggles/utils/resolve_oscal_profiles.py --profile-dir dist/oscal-profiles --output-dir dist/oscal-tailored-catalogs
```

If `oscal-cli` is not available on PATH, pass `--oscal-cli` followed by the full
path to the installed executable or Windows `.bat` launcher.

The wrapper delegates selection and hierarchy resolution to OSCAL CLI. It
validates each profile, resolves it, and validates the resulting tailored catalog.
All catalogs must pass before they are moved into the output directory. The
directory also receives `tailored-catalog-summary.json` with policy counts and
source catalog identity and version. Existing output files of the same names are
replaced after successful checks. Nothing is uploaded to GitHub.
Published files inherit the output directory's normal permissions rather than
retaining the private staging folder's permissions, so Windows editors can read
the resulting catalogs.

### Content Preservation

The CLI can normalize Markdown whitespace and omit unused back-matter resources.
For these selection-only profiles, the wrapper retains the CLI-selected IDs and
hierarchy while restoring the exact source fields for retained groups, controls,
and resources. This preserves original policy prose, shared prerequisites,
assumptions, terminology, NIST references, and the catalog's readable field order.

Resources referenced by retained groups or controls are included even if the CLI
prunes them. References between resources are followed too. Unused resources may
be omitted. Dangling internal links, changed hierarchy, or extra or missing
policies cause the operation to fail rather than publish incomplete catalogs.

The tailored catalog records source profile identity and inherits source catalog
version and attribution. Its source-profile link is relative to the output
directory. These provenance links still require the corresponding source files
when followed, although the selected baseline text and required resources are
present in the tailored catalog itself.

This wrapper accepts only the generated selection profiles with a single local
catalog import and `merge.as-is`. Profiles containing `modify` instructions are
rejected; content-changing tailoring needs separate handling so original source
values are not incorrectly restored over intentional changes. Use only trusted
local profile and catalog files.

## Validation

The existing OSCAL CLI can validate a generated profile:

```text
oscal-cli validate dist/oscal-profiles/scubagoggles-oscal-profile-required-oscal-1.2.3.json
```

The pinned CLI remains version 3.2.0 with its bundled OSCAL 1.2.1 model. Its
success is not a complete validation against OSCAL 1.2.3. No additional schema
validator is introduced here.

Unit tests check all seven selections, excluded controls, negative requirement
forms, source traceability, portable imports, and invalid input. Workflow and
release-artifact integration are not changed by this utility.
