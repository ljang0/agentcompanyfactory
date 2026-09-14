#!/usr/bin/env python3
"""Read a directory of interaction reports and say what is broken, in severity order.

The checks are not equally strong evidence. A blank first screen or a failure phrase is proof
the app is broken. "No state diff" is weaker: the probe types into the first text box it finds,
which on many apps is a search field, so a missing diff can mean the probe never performed a
write rather than that the app cannot record one. They are reported separately for that reason.
"""

import argparse
import json
from pathlib import Path

BROKEN = ("first_screen_not_empty", "first_screen_no_error", "no_click_shows_an_error")
SEEDING = ("shows_seeded_content", "seed_readback", "no_diff_after_seed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path)
    args = parser.parse_args()
    rows = []
    for path in sorted(args.reports.glob("*.json")):
        report = json.loads(path.read_text())
        if "app_id" not in report:
            continue
        failed = set(report.get("failed_checks", []))
        mutation = report.get("mutation") or {}
        rows.append(
            {
                "app": report["app_id"],
                "origin": report.get("state_origin", "?"),
                "broken": sorted(failed & set(BROKEN)),
                "seeding": sorted(failed & set(SEEDING)),
                "clicks": report.get("click_summary") or {},
                "faults": (report.get("faults") or [])[:1],
                "overflow": report.get("storage_overflow_after") or report.get("storage_overflow") or 0,
                "submitted": mutation.get("submitted"),
                "typed_into": mutation.get("typed_into"),
                "marker_in_state": mutation.get("marker_in_state"),
                "diff": "ui_action_reaches_state" not in failed,
                "harness_error": report.get("harness_error"),
                "not_volume": report.get("volume_tested") is False,
                "amplified": report.get("amplification") or {},
                "passed": report.get("passed"),
            }
        )

    reported = set()

    def group(name, chosen):
        """Each app is reported once, under the strongest thing that is true of it."""
        picked = [row for row in rows if row["app"] not in reported and chosen(row)]
        reported.update(row["app"] for row in picked)
        print(f"\n## {name} ({len(picked)})")
        for row in picked:
            detail = []
            if row["broken"]:
                detail.append("+".join(row["broken"]))
            if row["seeding"]:
                detail.append("+".join(row["seeding"]))
            if row["faults"]:
                detail.append(row["faults"][0][:90])
            if row["not_volume"]:
                grown = row["amplified"]
                detail.append(
                    f"state {grown.get('start_bytes')} -> {grown.get('bytes')} bytes"
                    f" of a {grown.get('target_bytes')} target"
                )
            clicks = row["clicks"]
            if clicks:
                detail.append(
                    f"clicks {clicks.get('changed_screen', 0)}/{clicks.get('tried', 0)} live"
                    f", {clicks.get('failed_to_click', 0)} unclickable"
                    + (f", {clicks['restored']} screens restored" if clicks.get("restored") else "")
                )
            if row["overflow"]:
                detail.append(f"storage overflow x{row['overflow']}")
            print(f"  {row['app']:<26} {row['origin']:<34} {' | '.join(detail)[:150]}")

    print(f"# {len(rows)} apps")
    group("Harness could not run the app", lambda r: r["harness_error"])
    # Not an app finding at all: the run never reached the size it set out to test, so whatever
    # else it says, it is not a volume verdict.
    group("Volume never tested: the state could not be grown to the target", lambda r: r["not_volume"])
    group("Broken on screen: blank, crashed, or showing a failure phrase", lambda r: r["broken"])
    group("Seeded records do not reach the screen", lambda r: r["seeding"] and not r["broken"])
    group("Working, and a typed write reached the state a grader reads", lambda r: r["marker_in_state"])
    group(
        "Working on screen, but no write was demonstrated",
        lambda r: (
            not r["broken"] and not r["seeding"] and not r["marker_in_state"] and not r["harness_error"]
        ),
    )
    writes = [r for r in rows if r["marker_in_state"]]
    submitted = [r for r in rows if r["submitted"]]
    print(
        f"\npassed every check: {sum(1 for r in rows if r['passed'])}"
        f" | volume never reached: {sum(1 for r in rows if r['not_volume'])}"
        f" | marker reached state: {len(writes)}"
        f" | probe found something to submit: {len(submitted)}"
        f" | storage overflow seen: {sum(1 for r in rows if r['overflow'])}"
    )


if __name__ == "__main__":
    main()
