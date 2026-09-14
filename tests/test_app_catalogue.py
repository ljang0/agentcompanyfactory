"""What keeps the ten newly ported hub apps reachable, and the catalogue honest about them.

Ten apps served the hub contract, had or could be given a pinned schema document under
``catalogs/app_schemas``, and were invisible to every stage of the pipeline, because
``hub_app.hub_apps`` admits a catalogue row only when ``source == cua_gym_hub`` AND ``schema`` AND
``state_keys`` are all present. The gate read 88 of 98 before the port and 98 of 98 after:

* ``canva_mock``, ``canvas_mock``, ``confluence_mock``, ``openreview_mock`` had **no row at all**.
  The upstream importer derived its rows from each clone's own ``SCHEMA.md``, and those are the
  only 4 of 98 hub directories that ship none.
* ``google_analytics_mock`` and ``youtube_mock`` had a row and a schema and ``state_keys: []``.
  Those are the only 2 of 94 rows whose clone writes its state as a fenced code block instead of a
  markdown table, which is what the importer read.
* ``TradingView_mock``, ``tripadvisor_mock``, ``wandb_mock`` and ``westlaw_mock`` had a row and
  ``schema: ""``. The importer admits an ``apps/*.md`` document only with both a state-schema
  section and an "Observable State Changes" table, and none of the four upstream documents has the
  second one -- a documentation gap, not an app that cannot be seeded. Their documents are imported
  byte-for-byte (two from the mock_websites schema corpus, two from the clone's own ``SCHEMA.md``),
  the provenance the other 94 have.

**A fixture built from an app's own demo data cannot detect the app absorbing its demo data.**
The google_analytics deep merge is the worked example: ``initializeData`` unioned the seed into
freshly generated defaults, and that fixture's ``dailyMetrics`` dates are exactly the 90 days from
2024-09-17 the clone generates for itself, so the union was a no-op and no run against the fixture
could ever have shown it. It took a seed on other dates: 180 days back, 90 of them the clone's own
demo rows. Every fixture under ``experiments/hub-smoke/states`` is captured from the app's own
unseeded state -- that is the point, the field names are then the app's -- so every one of them
carries the same blind spot wherever an app merges rather than replaces. Renaming the identity
surface is what makes a render check able to tell a seeded world from the demo rows; it does
nothing for the records underneath.

Six apps, one missing vocabulary: "how a schema document may be written". The rows are now derived
from the pinned document by ``top_level_keys`` -- the same reader ``validate_state`` gates with --
so a row cannot disagree with the contract it admits. These tests exist so a rebuild of
``apps.json`` from the source repository, or an unfrozen edit to it, fails loudly instead of
silently removing 5 apps from the design surface again.
"""

import json
import re
import tomllib
from pathlib import Path

import pytest

from company_envs.catalogs import Catalogs
from company_envs.storage import digest
from company_envs.world.hub_app import hub_apps, permitted_keys, top_level_keys, validate_state

ROOT = Path(__file__).resolve().parent.parent
CATALOGS = ROOT / "catalogs"
SCHEMAS = CATALOGS / "app_schemas"
FIXTURES = ROOT / "experiments/hub-smoke/states"
# The ten ported here, in two halves on 2026-09-10. Their rows are ours, not the importer's, so
# they are named. Each is also on design.available_runtime_apps, and is there only because it passed
# `company-envs hub-smoke` against its own fixture with the patches recorded in hub_patches.json.
PORTED = (
    "TradingView_mock",
    "canva_mock",
    "canvas_mock",
    "confluence_mock",
    "google_analytics_mock",
    "openreview_mock",
    "tripadvisor_mock",
    "wandb_mock",
    "westlaw_mock",
    "youtube_mock",
)


def catalogue():
    return hub_apps(json.loads((CATALOGS / "apps.json").read_text())["apps"])


def test_catalogue_integrity_is_frozen():
    """An edited catalogue file that was not re-pinned is an integrity error in every run.

    ``manifest.json`` pins every catalogue file's sha256 and ``Catalogs.integrity_errors`` compares
    them; ``audit.audit_run`` and ``report.build_report`` both refuse a run that reports one. Editing
    the pinned schema documents without re-pinning broke 57 tests on 2026-09-09, and adding these
    five rows to ``apps.json`` is the same edit. Nothing in the tree re-pins a catalogue file --
    ``catalogs.bootstrap`` writes the manifest once and returns early ever after, and
    ``scripts/freeze_manifest.py`` freezes the pipeline into experiments/FREEZE-MANIFEST.json and
    never reads this manifest -- so this is the only thing that notices.
    """
    assert Catalogs(CATALOGS).integrity_errors() == []


@pytest.mark.parametrize("app_id", sorted(catalogue()))
def test_every_catalogue_app_has_a_pinned_schema_document_with_a_readable_table(app_id):
    """Seeding reads the document, not the row, so an unreadable table is an unseedable app.

    ``validate_state`` takes the top-level contract from the ``## State Schema`` table and refuses
    any key it does not list. A catalogue row whose document has no readable table therefore admits
    an app no state can be seeded into -- which is how 2 of 94 rows came to carry
    ``state_keys: []``.
    """
    relative = f"app_schemas/{app_id}.md"
    path = CATALOGS / relative
    assert path.is_file(), f"{app_id} is in the catalogue with no pinned schema document"
    pinned = json.loads((CATALOGS / "manifest.json").read_text())["files"].get(relative)
    assert pinned, f"{relative} is not pinned by manifest.json"
    assert digest(path.read_bytes()) == pinned["sha256"], f"{relative} differs from its pin"
    assert top_level_keys(path.read_text()), f"{app_id}: no readable ## State Schema table"


@pytest.mark.parametrize("app_id", PORTED)
def test_a_ported_rows_state_keys_are_exactly_its_documents_top_level_keys(app_id):
    """These five rows are derived, not hand-listed, so they cannot drift from the contract.

    The importer's own rows are a flattened list of top-level *and* nested names (TradingView's has
    195 entries for 13 keys), which is why ``validate_state`` prefers the document whenever it has
    one and ``probe_key`` has to skip names that are not top-level. A derived row is the narrower,
    checkable claim: the gate that admits the app and the gate that validates its seed read the same
    bytes.
    """
    row = catalogue().get(app_id)
    assert row, f"{app_id} is not admitted by hub_apps(); the pipeline cannot see it"
    assert row["state_keys"] == top_level_keys((SCHEMAS / f"{app_id}.md").read_text())
    assert row["schema"] == f"apps/{app_id}.md", "hub_apps reads the basename; the prefix is apps/"


def test_no_fixture_state_carries_a_flattened_documentation_path_as_a_top_level_key():
    """A documentation path used as a literal key is a fixture the app refuses.

    ``top_level_keys`` drops nested paths (``charts[].config``, ``iam.users[]``) because they
    document a record's shape, not a key the state has. Two of the 88 fixtures carried them as real
    top-level keys beside the correct nested data -- amplitude's ``charts[].config`` and
    ``charts[].data``, aws_console's ``iam.users[]`` -- so ``validate_state`` refused both and
    neither app had ever been smoked with a state it would accept. Their recorded pass predated the
    check.
    """
    offenders = {
        path.name: sorted(key for key in json.loads(path.read_text()) if re.search(r"[\[\].]", key))
        for path in sorted(FIXTURES.glob("*.json"))
    }
    assert {name: keys for name, keys in offenders.items() if keys} == {}


@pytest.mark.parametrize("app_id", sorted(catalogue()))
def test_every_catalogue_app_has_a_fixture_state_it_would_accept(app_id):
    """98 of 98 on 2026-09-10, the whole hub. A fixture the app refuses proves nothing about the app.

    The fixture is what ``hub-smoke`` seeds and what ``scripts/interact_apps.py --mode volume``
    grows, so an invalid one silently turns both into a run against the app's own demo records.
    """
    path = FIXTURES / f"{app_id}.json"
    assert path.is_file(), f"{app_id} has no fixture state in {FIXTURES.relative_to(ROOT)}"
    schema = (SCHEMAS / f"{app_id}.md").read_text()
    state = json.loads(path.read_text())
    validate_state(catalogue()[app_id], state, schema)  # raises with the reason
    allowed = set(permitted_keys(app_id, schema) or ())
    assert set(state) <= allowed


def hub_clones():
    """Every directory under ``design.hub_root`` that is a vite app, or () when it is not checked out."""
    hub_root = Path(tomllib.loads((ROOT / "config.toml").read_text())["design"]["hub_root"]).expanduser()
    if not hub_root.is_dir():
        return ()
    return tuple(
        sorted(
            path.name
            for path in hub_root.iterdir()
            if path.is_dir() and any((path / f"vite.config.{ext}").is_file() for ext in ("js", "ts"))
        )
    )


@pytest.mark.parametrize("app_id", hub_clones() or PORTED)
def test_every_hub_clone_that_serves_the_contract_is_admitted(app_id):
    """The test these ten would have failed all night: a working clone nothing could see.

    Each clone is a real directory under ``design.hub_root`` whose vite config serves ``/state``, and
    each has a pinned schema document -- the two facts that make an app seedable through the hub
    contract -- so the only thing that ever kept ten of them out was a row. This fails again if
    ``apps.json`` is rebuilt by the source repository's ``scripts/build_apps.py``: that importer reads
    each clone's own ``SCHEMA.md`` and admits it only with an "Observable State Changes" table, and of
    the ten, four ship no SCHEMA.md, two write their state as a fenced code block rather than the
    table the reader matches, and four have no such section in any upstream document.

    Now scoped to every clone, not just the ported ten. The earlier half-port left this narrow on
    purpose, so it would not go red for the other agent's five; both halves have landed.
    """
    hub_root = Path(tomllib.loads((ROOT / "config.toml").read_text())["design"]["hub_root"]).expanduser()
    source = hub_root / app_id
    if not source.is_dir():
        pytest.skip(f"hub source {source} is not checked out")
    config = next(
        (source / f"vite.config.{ext}" for ext in ("js", "ts") if (source / f"vite.config.{ext}").is_file()),
        None,
    )
    assert config is not None, f"{app_id} is not a hub vite app"
    # As built, not as shipped: 2 of the 98 clones (adp_mock, PACS-viewer_mock) serve no GET /state
    # at all and their recorded patch adds the route, so the shipped bytes are the wrong thing to read.
    edits = json.loads((ROOT / "src/company_envs/world/hub_patches.json").read_text()).get(app_id, [])
    served = config.read_text() + "".join(e["new"] for e in edits if e["file"] == config.name)
    assert "/state" in served, f"{app_id} does not serve the hub /state route, patched or not"
    assert app_id in catalogue(), (
        f"{app_id} serves the hub contract and has a pinned schema document but no catalogue row "
        f"hub_apps() admits, so no stage can see it"
    )


def test_the_whole_hub_is_admitted_and_on_the_runtime_surface():
    """98 of 98 on 2026-09-10. It was 88, and the 10 missing were not broken apps -- just unseen.

    ``hub_apps()`` is the gate every stage reads, and ``design.available_runtime_apps`` is what puts
    an admitted app in front of the design stage; an app in one and not the other is invisible work.
    The two were 93 and 88 mid-port, which is the state this pins against coming back.
    """
    clones = hub_clones()
    if not clones:
        pytest.skip("hub source is not checked out")
    admitted = set(catalogue())
    surface = set(tomllib.loads((ROOT / "config.toml").read_text())["design"]["available_runtime_apps"])
    assert len(clones) == 98, sorted(clones)
    assert admitted == set(clones), sorted(admitted.symmetric_difference(clones))
    assert surface == admitted, sorted(surface.symmetric_difference(admitted))


def test_every_app_on_the_runtime_surface_answers_the_hub_state_envelope():
    """An app whose ``/state`` returns the bare state crashes the golden stage, not the smoke test.

    ``HubClient.current`` reads ``{stored_state, has_custom_state, sid}``; ``golden.py:337`` reads
    ``has_custom_state`` directly and ``hub_app.smoke`` and ``proxy_roundtrip`` read both. 12 of the
    98 hub clones answer the bare state object instead; 10 carry a recorded fix in
    ``hub_patches.json``. The two that do not are ``google_analytics_mock`` and ``wandb_mock``,
    because neither was ever in the catalogue -- so nothing checked them, and nothing would have
    checked them on the day somebody added them to the surface either. Being in the catalogue is
    not the same as being safe to run: this is the check that separates the two.
    """
    design = tomllib.loads((ROOT / "config.toml").read_text())["design"]
    hub_root = Path(design["hub_root"]).expanduser()
    if not hub_root.is_dir():
        pytest.skip(f"hub source {hub_root} is not checked out")
    patches = json.loads((ROOT / "src/company_envs/world/hub_patches.json").read_text())
    bare = []
    for app_id in design["available_runtime_apps"]:
        config = next(
            (
                hub_root / app_id / f"vite.config.{ext}"
                for ext in ("js", "ts")
                if (hub_root / app_id / f"vite.config.{ext}").is_file()
            ),
            None,
        )
        if config is None:
            bare.append(f"{app_id} (no vite config)")
        elif "has_custom_state" not in config.read_text() and not any(
            "has_custom_state" in edit.get("new", "") for edit in patches.get(app_id, [])
        ):
            bare.append(f"{app_id} (bare /state, no patch)")
    assert not bare, (
        "these apps are on design.available_runtime_apps but their GET /state does not answer the "
        f"hub envelope, so golden authoring raises KeyError for any company holding them: {bare}"
    )


# Three of the ten strings render_check.PLACEHOLDER_TEXT fails an app for are names a clone
# hardcodes. A demo name inside src/utils/dataManager.js is the app's own demo *data* and a seed
# replaces it; a demo name inside a rendered component is not replaceable by anything, which is the
# whole defect.
RENDER_GATE_NAMES = re.compile(r"\b(?:John Doe|Jane Doe|Alex Johnson)\b")
RENDERED_DIRS = ("src/components/", "src/pages/")
# An input's placeholder attribute is not in the page's innerText, and the gate reads innerText.
PLACEHOLDER_ATTR = re.compile(r'placeholder\s*=\s*(?:"[^"]*"|\{[^}]*\})')


def patched_text(hub_root, app_id, relative, patches):
    """One shipped file with the app's recorded hub patches applied, in order."""
    text = (hub_root / app_id / relative).read_text(errors="replace")
    for edit in patches.get(app_id, []):
        if edit["file"] == relative and text.count(edit["old"]) == 1:
            text = text.replace(edit["old"], edit["new"])
    return text


def test_no_rendered_component_hardcodes_a_name_the_render_gate_fails_an_app_for():
    """A demo person baked into a component fails the company, and no seed can repair it.

    Measured 2026-09-10 on the 20 companies that have a ``runtime/RENDER.json``: 5 failed the render
    gate, and 2 of those 5 -- beacon-communities and lensrentals -- failed on one string.
    ``airtable_mock`` wrote 'John Doe' and the initials 'JD' into its sidebar row, its account panel
    and its share dialog's owner line, so both companies record ``template or leaked text in the
    page: 'John Doe'`` while 61 of 329 and 115 of 363 of their own seeded strings were visible on the
    same page. The world was there; the app was naming somebody else. ``postman_mock`` had the same
    string in its profile menu with no company mounted yet. Both now read the state.
    """
    design = tomllib.loads((ROOT / "config.toml").read_text())["design"]
    hub_root = Path(design["hub_root"]).expanduser()
    if not hub_root.is_dir():
        pytest.skip(f"hub source {hub_root} is not checked out")
    patches = json.loads((ROOT / "src/company_envs/world/hub_patches.json").read_text())
    offenders = []
    for app_id in design["available_runtime_apps"]:
        source = hub_root / app_id
        if not source.is_dir():
            continue
        for path in sorted(source.rglob("*")):
            if not path.is_file() or path.suffix not in (".js", ".jsx", ".ts", ".tsx"):
                continue
            relative = path.relative_to(source).as_posix()
            if not relative.startswith(RENDERED_DIRS):
                continue
            text = PLACEHOLDER_ATTR.sub("", patched_text(hub_root, app_id, relative, patches))
            for name in sorted(set(RENDER_GATE_NAMES.findall(text))):
                offenders.append(f"{app_id}:{relative} renders {name!r}")
    assert not offenders, (
        "render_check.PLACEHOLDER_TEXT fails an app whose page shows any of these names, and a "
        f"component is not seedable, so every company mounting one of these apps fails: {offenders}"
    )


def test_every_catalogued_entry_route_is_a_route_its_own_clone_declares():
    """A worker opens this path; a path the router does not have lands on the ``*`` fallback.

    ``catalogs/app_entry_routes.json`` went from 5 apps to 19 on 2026-09-10, measured by serving each
    app against its largest real world through ``WorkerAppProxy`` and rendering every static route
    the clone's router declares. The gains are large enough that a typo would be expensive and
    silent: Zendesk goes from 6 of 200 seeded strings on ``/`` to 197 on ``/customers``, hotjar from
    1 of 95 to 80 on ``/feedback``, and expedia's ``/`` shows 0 of 58 -- not one of the company's own
    strings on its landing page.
    """
    hub_root = Path(tomllib.loads((ROOT / "config.toml").read_text())["design"]["hub_root"]).expanduser()
    if not hub_root.is_dir():
        pytest.skip(f"hub source {hub_root} is not checked out")
    recorded = json.loads((ROOT / "catalogs/app_entry_routes.json").read_text())["apps"]
    missing = []
    for app_id, entry in sorted(recorded.items()):
        app = next(
            (
                hub_root / app_id / f"src/App.{ext}"
                for ext in ("jsx", "tsx", "js", "ts")
                if (hub_root / app_id / f"src/App.{ext}").is_file()
            ),
            None,
        )
        if app is None:
            missing.append(f"{app_id} has no src/App.* to read routes from")
            continue
        declared = {
            path if path.startswith("/") else f"/{path}"
            for path in re.findall(r'path="([^"]+)"', app.read_text(errors="replace"))
        }
        # A nested route is declared relative to its parent, so match on the last segment too.
        tails = {f"/{path.rstrip('/').rsplit('/', 1)[-1]}" for path in declared}
        wanted = entry["entry_path"]
        if wanted not in declared and f"/{wanted.rstrip('/').rsplit('/', 1)[-1]}" not in tails:
            missing.append(f"{app_id} entry_path {wanted} is not a route in {app.name}: {sorted(declared)}")
    assert not missing, missing
