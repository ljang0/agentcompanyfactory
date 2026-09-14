#!/usr/bin/env bash
# Weekly construction gate. This makes real model calls and starts real hub apps.
# Usage: scripts/weekly_integration.sh COMPANY_ID RUN_ID
# It proves construction and reference replay, not a worker VM episode.
set -euo pipefail

if [[ $# != 2 || ! $1 =~ ^[a-z][a-z0-9_-]*$ || ! $2 =~ ^[A-Za-z0-9_:+-]+$ ]]; then
    echo 'Usage: scripts/weekly_integration.sh COMPANY_ID RUN_ID' >&2
    exit 2
fi
integration_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$integration_root"
export PYTHONPATH="$integration_root/src"
integration_python="$integration_root/.venv/bin/python"
integration_parent="$integration_root/experiments/integration/$(date -u +%F)"
mkdir -p "$integration_parent"
integration_dir=$(mktemp -d "$integration_parent/$1-XXXXXX")
integration_company="$integration_dir/company"

# Keep a failed agreement too, including when construction stops before calibration.
finish() {
    integration_status=$?
    trap - EXIT
    "$integration_python" - "$integration_company" "$integration_dir" "$integration_status" <<'PY'
import sys
from pathlib import Path
from company_envs.storage import write
from company_envs.world.agreement import agreement

folder, output = map(Path, sys.argv[1:3])
status = int(sys.argv[3])
try:
    report = agreement(folder)
except Exception as exc:
    report = {"done": False, "error": f"{type(exc).__name__}: {exc}"}
report["command_exit_code"] = status
report["done"] = report["done"] and status == 0
write(output / "AGREEMENT.json", report)
print(output / "AGREEMENT.json")
raise SystemExit(0 if report["done"] else 1)
PY
}
trap finish EXIT
exec > >(tee "$integration_dir/run.log") 2>&1

"$integration_python" -m company_envs export-company "$2" "$1" --output "$integration_company"
"$integration_python" -m company_envs rewrite-briefs "$integration_company"
"$integration_python" -m company_envs assign-apps "$integration_company"
"$integration_python" -m company_envs seed-world "$integration_company" --review-rounds 2
"$integration_python" -m company_envs add-bulk "$integration_company"
"$integration_python" -m company_envs sync-worker-apps "$integration_company"
"$integration_python" -m company_envs readability "$integration_company"

# add-bulk ran world_check with its generated-record ids and wrote fresh CHECKS.json.
# Preserve that distinction between ordinary prose and intentionally repeated bulk rows.
# Keep the app processes alive until every task has replayed and calibrated.
"$integration_python" - "$integration_root" "$integration_company" "$integration_dir" <<'PY'
import sys
import tomllib
from pathlib import Path
from company_envs.storage import read
from company_envs.world.golden import author_golden
from company_envs.world.grader import author_grader, calibrate
from company_envs.world.hub_app import HubClient
from company_envs.world.hub_world import CompanyWorld

root, folder, output = map(Path, sys.argv[1:])
config = tomllib.loads((root / "config.toml").read_text())
bulk = read(folder / "world/BULK.json")
if any("skipped" in result for result in bulk["apps"].values()):
    raise SystemExit("Bulk generation failed for an app; see world/BULK.json")
checks = read(folder / "world/CHECKS.json")
if not checks["ok"]:
    raise SystemExit("World checks failed; see world/CHECKS.json")
world = CompanyWorld(folder, config["design"]["hub_root"], output / "hub-builds", root=root, host="127.0.0.1")
try:
    world.start()
    clients = {app: HubClient(entry["harness_url"]) for app, entry in world.endpoints.items()}
    tasks = [p.parent.name for p in sorted(folder.glob("tasks/*/workflow.json")) if not p.parent.name.startswith("_")]
    if not tasks:
        raise ValueError("The exported company has no tasks")
    for task_id in tasks:
        author_golden(root, folder, task_id)
        author_grader(root, folder, task_id)
        calibrate(folder, task_id, clients, golden=True)
finally:
    world.stop()
PY
