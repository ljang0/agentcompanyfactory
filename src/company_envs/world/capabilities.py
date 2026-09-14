"""Known runtime adapter facts, not app or business-quality certification.

Two adapters are executable. ``hub``: each app has its own origin and shared state, seeded by
posting its full native state. ``desktop``: the work is files in the guest's own applications --
the world's materials are rendered into real documents by ``documents.py``, uploaded into the
guest home, and opened in the application their suffix names (``catalogs/desktop_apps.json``).
The ``native`` facts and defaults below only preserve validation of frozen design
snapshots. Native builders and services live in the external historical archive;
these compatibility facts do not provide an executable native runtime.
"""

import re

# Catalog identities are exact and case-sensitive. Other catalog entries are
# not assumed to have an implemented binding just because their IDs are valid.
APP_KINDS = {
    "contractbook_mock": "contracts",
    "google_docs_mock": "docs",
    "google_sheets_mock": "sheets",
    "Zendesk_mock": "zendesk",
}

ADAPTERS = ("native", "hub", "desktop")
HUB_IDENTITY = "shared_session"
# One guest, one account: the worker is the guest user and every file they touch is attributed
# to their VM, the same access model the hub adapter states, without a login inside an app.
DESKTOP_IDENTITY = "guest_user"


def app_mount(kind, mount=None):
    default = "/sheets" if kind == "sheets" else ""
    mount = default if mount is None else mount
    if not isinstance(mount, str) or (
        mount and not re.fullmatch(r"/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", mount)
    ):
        raise ValueError("App mount must be empty or a canonical local path without a trailing slash")
    if kind != "docs" and mount != default:
        raise ValueError("Configurable mounting is currently supported only for Docs")
    return mount


def runtime_adapter(config):
    adapter = config.get("design", {}).get("runtime_adapter", "native")
    if adapter not in ADAPTERS:
        raise ValueError(f"runtime_adapter must be one of {list(ADAPTERS)}")
    return adapter


def desktop_facts(available):
    """What an author needs to know to write work for the desktop adapter."""
    from .desktop_app import desktop_apps

    catalog = desktop_apps()
    return {
        "adapter": "desktop",
        "identity": DESKTOP_IDENTITY,
        "scope": (
            "Files in the guest's own desktop applications: each material is written as text, "
            "rendered into the document format its filename promises, and opened in the "
            "application that owns that format. There is no server and no app state to seed."
        ),
        "apps": {
            app_id: {
                "kind": "desktop",
                "name": catalog[app_id]["name"],
                "opens": list(catalog[app_id]["opens"]),
                "graded_from": catalog[app_id]["reader"],
            }
            for app_id in available
            if app_id in catalog
        },
        "unmapped_app_ids": sorted(set(available) - set(catalog)),
        "requirements": [
            (
                "Name every material with a suffix one of the listed applications opens; a name "
                "with any other suffix reaches the desktop as a file nothing will open."
            ),
            (
                "A worker's materials land under Desktop/, Documents/, Downloads/, Pictures/ or "
                "Music/ in their own home; anything else lands on the Desktop."
            ),
            (
                "Grading reads the file back, so a check must name a value that survives into the "
                "document: a cell, a paragraph, a slide's text, a line of the PDF."
            ),
            (
                "Access is by which VM receives which material; do not require per-user "
                "permissions inside an application."
            ),
            (
                "Do not silently add, drop or substitute required applications; choose grounded "
                "feasible work or report the gap."
            ),
        ],
    }


def runtime_mounting(config):
    available = config.get("design", {}).get("available_runtime_apps")
    if available is None:
        return None
    if runtime_adapter(config) == "desktop":
        return desktop_facts(available)
    if runtime_adapter(config) == "hub":
        return {
            "adapter": "hub",
            "identity": HUB_IDENTITY,
            "scope": (
                "CUA-Gym hub state contract: each app runs as its own origin and is seeded by posting "
                "its complete native state; documentation is not proof of every UI interaction."
            ),
            "apps": {
                app_id: {
                    "kind": "hub",
                    "origin": "separate",
                    "seed_contract": "full_state_all_documented_keys",
                }
                for app_id in available
            },
            "unmapped_app_ids": [],
            "requirements": [
                "Any combination of listed apps may be required together; each is a separate origin.",
                "Seed state must contain every documented state key of the app schema and no others.",
                (
                    "Data is shared and each worker is logged in as their own named user: the same records, "
                    "seen through that worker's account; mailboxes are per person. Access is by which VM "
                    "receives which app, and every action is attributed to the worker's VM."
                ),
                "Do not require server-enforced per-user permissions inside an app; use VM-level access instead.",
                "Do not silently add, drop or substitute required apps; choose grounded feasible work or report the gap.",
            ],
        }
    return {
        "adapter": "native",
        "scope": "Historical native routing facts only; builders and services are archived",
        "apps": {
            app_id: {
                "kind": APP_KINDS[app_id],
                "default_mount": app_mount(APP_KINDS[app_id]),
                "configurable_mount": APP_KINDS[app_id] == "docs",
            }
            for app_id in available
            if app_id in APP_KINDS
        },
        "unmapped_app_ids": sorted(set(available) - APP_KINDS.keys()),
        "requirements": [
            "Each selected workflow must have exactly one root app and distinct nonshadowing mounts.",
            "Contracts and Zendesk are root-only and cannot coexist in one current world.",
            "Docs can be root or use an available prefix; Sheets is fixed at /sheets and cannot stand alone.",
            "Do not silently add, drop or substitute required apps to satisfy routing; choose grounded feasible work or report the capability gap.",
        ],
    }


def validate_runtime_mounting(app_ids, adapter="native"):
    if adapter not in ADAPTERS:
        raise ValueError(f"runtime_adapter must be one of {list(ADAPTERS)}")
    required = set(app_ids)
    if adapter == "desktop":
        # One guest, one filesystem: nothing to mount and nothing to conflict. What can go
        # wrong is naming an application the image does not have, which is what this rejects.
        from .desktop_app import validate_desktop_apps

        validate_desktop_apps(required)
        return
    if adapter == "hub":
        # Separate origins: no mount conflicts. Membership in the declared
        # surface is checked by the caller against the frozen catalog.
        return
    if unknown := required - APP_KINDS.keys():
        raise ValueError(f"Required apps have no known native adapter mapping: {sorted(unknown)}")
    kinds = [APP_KINDS[app_id] for app_id in required]
    root_only = [kind for kind in kinds if kind != "docs" and app_mount(kind) == ""]
    if len(root_only) > 1:
        raise ValueError("Required apps conflict: Contracts and Zendesk both require the root mount")
    if not root_only and "docs" not in kinds:
        raise ValueError("Required apps have no root-capable app; Sheets cannot stand alone")
