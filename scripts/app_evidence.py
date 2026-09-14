#!/usr/bin/env python3
"""One row per catalogue app: what has been proved about it, and which file proves it.

Every cell points at a recorded artifact. A dimension with no artifact reads `-`, which means
no evidence either way -- never "works". The point is to make the gaps as visible as the passes.

Dimensions
  builds    the app built from hub source                      interact report
  seeds     seed, inspect, update, deny, reset round trip      experiments/hub-smoke/<app>.json
  accounts  per-worker proxy keeps seeded ids, refuses a       same record
            wholesale demo replacement, denies harness routes
  renders   a real company's records reach the first screen    fidelity report + screenshot
  clicks    the views a person can reach open without error    fidelity report
  writes    a typed write reached the state a grader reads      fidelity report (marker_in_state)
  volume    survives a state grown to a real company's size    volume report
  schema    the pinned schema matches the state the app keeps  drift report
"""

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BROKEN = ("first_screen_not_empty", "first_screen_no_error", "no_click_shows_an_error")
# Guest-side proof, and nothing else counts as it: a report carrying only host-side check names was
# taken on the host, whatever directory it was filed in. Matched by meaning rather than spelling,
# because each run named these differently -- proxy_denies_go_in_guest, vm_proxy_denies_go and
# vm_proxy_denies_go_route are one claim.
VM_REQUIRED = (
    re.compile(r"proxy_answers_200_in_guest"),
    re.compile(r"denies_go"),
    re.compile(r"seeded_records|state_.*returns_seeded"),
)


def guest_proved(report):
    """Whether a report shows the app served to a worker inside a guest, and the harness refused.

    Every matching check must pass, and each of the three claims must be present somewhere: a
    report that simply never made one of them has not proved it.
    """
    checks = report.get("checks") or {}
    for pattern in VM_REQUIRED:
        matched = [value for name, value in checks.items() if pattern.search(name)]
        if not matched or not all(matched):
            return False
    return True


# The per-worker proxy contract: a worker may save as itself and load its own view, and may
# not reach the harness routes that would show it the grader's baseline.
ACCOUNT_CHECKS = (
    "worker_proxy_denies_harness_routes",
    "worker_proxy_allows_app_saves",
    "worker_proxy_allows_app_state_load",
)
# Added to smoke after most records were written; absent means unproved, not failed.
IDENTITY_CHECK = "identity_proxy_preserves_seeded_record_ids"


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# The four outcomes this table can show, in the shared vocabulary of company_envs.receipt. There is
# no ``faulted`` column because none of the reports this reads records one: every cell here is
# either a verdict or the absence of evidence, and ``-`` is that absence. The mapping is written as
# the outcome names rather than as bare booleans so that the moment a report does carry a fault, the
# reader has somewhere to put it instead of folding it into "NO".
MARKS = {"passed": "yes", "refused": "NO", "faulted": "ERR", "unmeasured": "-"}


def mark(value):
    """One cell: its outcome's mark, read through the shared reader rather than a boolean table."""
    from company_envs.receipt import OUTCOME_OK

    outcomes = {ok: name for name, ok in OUTCOME_OK.items() if name != "faulted"}
    return MARKS[outcomes[value]]


def cell(taken, ok):
    """A dimension's cell: ``-`` unless the measurement was taken, then its verdict.

    Both questions have to be answered, and neither has a default, which is the point. Nine of the
    fourteen columns were hand-rolled ``None if not report else ...`` chains and each one was a place
    this defect could recur -- it did, in ``volume``: a run that never reached its target printed as
    a volume **pass**, because the chain asked whether a report existed and not whether it had
    measured anything. ``gate`` in company_envs.receipt is the same question asked once, for every
    stage, and a caller cannot reach a pass through it without first saying the run happened.
    """
    from company_envs.receipt import gate, ok_of

    return ok_of(gate(taken=bool(taken), ok=bool(ok), what="this dimension"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fidelity", type=Path, required=True)
    parser.add_argument("--volume", type=Path)
    parser.add_argument("--drift", type=Path)
    parser.add_argument("--smoke", type=Path, default=ROOT / "experiments/hub-smoke")
    parser.add_argument(
        "--vm", type=Path, help="counts of companies that served each app in a VM, and in an episode"
    )
    parser.add_argument("--vm-run", type=Path, help="a directory of live-VM reports, one per app")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    import sys

    sys.path.insert(0, str(ROOT / "src"))
    from company_envs.world.hub_app import hub_apps

    apps = sorted(hub_apps(json.loads((ROOT / "catalogs/apps.json").read_text())["apps"]))
    vm = load(args.vm) if args.vm else {}
    # What a seed may carry is the imported document plus the observed supplement, and the field
    # names each app's own records carry are a contract of their own.
    supplement = (load(ROOT / "catalogs/app_schema_supplement.json") or {}).get("apps", {})
    permitted = {app_id: set(entry.get("keys") or {}) for app_id, entry in supplement.items()}
    record_fields = (load(ROOT / "catalogs/app_record_fields.json") or {}).get("apps", {})
    vm_run = {}
    if args.vm_run:
        for path in sorted(Path(args.vm_run).glob("*.json")):
            report = load(path)
            if isinstance(report, dict) and report.get("app_id"):
                vm_run[report["app_id"]] = report
    episode = vm.get("episode", {})
    rows = []
    for app in apps:
        fidelity = load(args.fidelity / f"{app}.json") or {}
        volume = load(args.volume / f"{app}.json") if args.volume else None
        drift = load(args.drift / f"{app}.json") if args.drift else None
        smoke = load(args.smoke / f"{app}.json")
        checks = fidelity.get("checks") or {}
        failed = set(fidelity.get("failed_checks") or [])
        clicks = fidelity.get("click_summary") or {}
        seeded = fidelity.get("state_origin", "none").startswith("world")
        rows.append(
            {
                "app": app,
                "builds": cell(fidelity, not fidelity.get("harness_error")),
                "seeds": cell(smoke, (smoke or {}).get("passed")),
                "accounts": cell(
                    smoke and all(name in smoke.get("checks", {}) for name in ACCOUNT_CHECKS),
                    smoke
                    and all(smoke["checks"][name] for name in ACCOUNT_CHECKS if name in smoke["checks"]),
                ),
                "identity": (smoke or {}).get("checks", {}).get(IDENTITY_CHECK),
                "renders": cell(seeded and checks, checks.get("shows_seeded_content")) if seeded else None,
                "clicks": cell(checks, not (failed & set(BROKEN))),
                "writes": cell(fidelity, (fidelity.get("mutation") or {}).get("marker_in_state")),
                # Volume asks one thing: does the app survive a real company's size on screen.
                # A run that never reached the target tested nothing, so it is no evidence rather
                # than a pass -- canvas_mock filed a volume verdict at 1,764 bytes because its only
                # collection is nested and the amplifier could not grow it.
                "volume": cell(
                    volume and volume.get("volume_tested") is not False,
                    not (set((volume or {}).get("failed_checks") or []) & set(BROKEN)),
                ),
                # Whether it renders seeded records at all, for apps with no real world to seed.
                "seeded_view": (volume.get("checks", {}).get("shows_seeded_content") if volume else None)
                if not seeded
                else None,
                "needed_shim": bool(
                    (volume or {}).get("storage_overflow_after") or (volume or {}).get("storage_overflow")
                )
                if volume
                else None,
                "seedable": cell(
                    drift and drift.get("observed"),
                    not [
                        key
                        for key in (drift or {}).get("observed") or ()
                        if key not in (permitted.get(app, set()) | set(drift.get("documented") or []))
                    ],
                ),
                "dead_keys": cell(
                    drift and drift.get("observed"),
                    not (drift or {}).get("documented_but_app_keeps_none"),
                ),
                "fields": True if record_fields.get(app) else None,
                # Served to a worker inside a VM at all, and inside a VM episode that produced a
                # result. Absent evidence is the common case here, not a failure.
                # A live-VM report outranks the cohort counts: it says the app was served to a
                # worker in a guest and what the guest saw, not merely that an endpoint existed.
                # An endpoint having existed once is weaker than a guest having opened it, so a
                # report is the only thing that counts here.
                "vm": guest_proved(vm_run[app]) if app in vm_run else None,
                "episode": True if episode.get(app) else None,
                "world": fidelity.get("state_origin", "none"),
                "live": f"{clicks.get('changed_screen', 0)}/{clicks.get('tried', 0)}",
                "overflow": fidelity.get("storage_overflow_after") or 0,
                "unseedable": len((drift or {}).get("undocumented_so_unseedable") or []),
            }
        )

    columns = (
        "builds",
        "seeds",
        "accounts",
        "identity",
        "renders",
        "seeded_view",
        "clicks",
        "writes",
        "volume",
        "seedable",
        "dead_keys",
        "fields",
        "vm",
        "episode",
    )
    lines = [
        "| app | " + " | ".join(columns) + " | live clicks | seeded from |",
        "|" + "---|" * (len(columns) + 3),
    ]
    for row in rows:
        cells = " | ".join(mark(row[name]) for name in columns)
        lines.append(f"| {row['app']} | {cells} | {row['live']} | {row['world']} |")
    table = "\n".join(lines)
    print(table)
    print(f"\n{len(rows)} catalogue apps")
    # "no evidence" is its own count and not a rounding of the other two: the table's whole claim is
    # that a gap is as visible as a pass. The words are the shared outcomes -- proved is ``passed``,
    # failed is ``refused``, no evidence is ``unmeasured``.
    for name in columns:
        yes = sum(1 for row in rows if row[name] is True)
        no = sum(1 for row in rows if row[name] is False)
        print(f"  {name:<9} proved {yes:>3}   failed {no:>3}   no evidence {len(rows) - yes - no:>3}")
    shim = [row["app"] for row in rows if row["needed_shim"]]
    if shim:
        print(
            f"\nover the browser storage quota at real volume, kept working by the shim: {len(shim)}"
            f" of {len(rows)} apps"
        )
    overflow = [row["app"] for row in rows if row["overflow"]]
    if overflow:
        print(f"\nstorage quota overflowed (the shim kept them working): {', '.join(overflow)}")
    drifted = sorted((row for row in rows if row["unseedable"]), key=lambda r: -r["unseedable"])
    if drifted:
        print("\nkeys the app keeps that the schema omits, so they can never be seeded:")
        for row in drifted[:15]:
            print(f"  {row['app']:<26} {row['unseedable']}")
    if args.out:
        args.out.write_text(json.dumps(rows, indent=2))
        args.out.with_suffix(".md").write_text(table + "\n")
        print(f"\nwrote {args.out} and {args.out.with_suffix('.md')}")


if __name__ == "__main__":
    main()
