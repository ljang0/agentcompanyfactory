# World acceptance and freeze

Run these commands on a complete populated company after configuring the external
hub and browser. They are for a new or deliberately repaired world. The published
SanMar world already has accepted evidence; inspect and reuse its checkpoint.

```sh
uv run company-envs --config configs/pilot-sanmar.local.toml preflight
uv run company-envs --config configs/pilot-sanmar.local.toml review-world COMPANY_FOLDER --round 1
uv run company-envs --config configs/pilot-sanmar.local.toml verify-world COMPANY_FOLDER --work WORK_DIRECTORY
uv run company-envs --config configs/pilot-sanmar.local.toml freeze-world COMPANY_FOLDER
```

Review uses fresh model sessions. Runtime verification starts native apps, checks
worker views, performs browser writes, verifies persistence and resets the apps.
It makes no model calls. Freeze requires current passing review and runtime evidence.

Inspect `world/acceptance/` and `world/FROZEN.json` after completion. A source or
state change can invalidate their fingerprints. Do not update hashes by hand to
make an old receipt look current.

For source changes, run the development checks before recording new runtime proof:

```sh
scripts/check.sh
```

See [stage commands](PILOT.md) for bounded population repairs and checkpoint reuse.
