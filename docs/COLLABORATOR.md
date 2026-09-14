# Inspect company snapshots

You can read the [apparel supply](../examples/sanmar/README.md) and
[software delivery](../examples/thoughtbot/evaluation/README.md) examples on GitHub without
installing anything. Full inspection packages add native states, worker files,
desktop traces and replay proof.

For any task inspection package, start with its `INSPECT.md` and use the company
path recorded in `COLLABORATOR.json`. The replay instructions below apply to these
packages. The earlier [software delivery world snapshot](../examples/thoughtbot/README.md)
covers Stage 4 and instead provides `WORLD-README.md` for its native world checks.

## Download the software delivery task snapshot

The [task inspection release](https://github.com/ljang0/agentcompanyfactory/releases/tag/thoughtbot-inspection-2026-09-14)
contains two reviewed tasks, calibrated verifiers and the recorded team results.
It preserves the earlier world snapshot and failed teacher attempts.

```sh
gh release download thoughtbot-inspection-2026-09-14 --repo ljang0/agentcompanyfactory \
  --pattern 'thoughtbot-inspection-20260914*' --dir thoughtbot-download
cd thoughtbot-download
sha256sum -c thoughtbot-inspection-20260914.tar.gz.sha256
sha256sum -c thoughtbot-inspection-20260914-proofs.tar.gz.sha256
tar -xzf thoughtbot-inspection-20260914.tar.gz
tar -xzf thoughtbot-inspection-20260914-proofs.tar.gz
cd thoughtbot-inspection-20260914
```

The companion proofs archive adds `COLLABORATOR-VERIFIED.json` and the fresh
reference-replay evidence to the extracted folder. The separate
`thoughtbot-inspection-20260914-VERIFIED.json` identifies the tested main archive
and proof archive by SHA256 and records the source commit. Start with `INSPECT.md`.

## Download the SanMar inspection snapshot

With the GitHub CLI:

```sh
gh release download sanmar-inspection-2026-09-14 --repo ljang0/agentcompanyfactory \
  --pattern 'company-envs-inspection-20260914-share.tar.gz*' --dir inspection-download
cd inspection-download
sha256sum -c company-envs-inspection-20260914-share.tar.gz.sha256
tar -xzf company-envs-inspection-20260914-share.tar.gz
cd company-envs-inspection
```

Open `INSPECT.md` in the extracted directory. It links assignments, grades, final
native states, delivered files and desktop traces. `COLLABORATOR.json` records
payload hashes; `COLLABORATOR-VERIFIED.json` records the measured clean replay.
Its historical `inspection_verified_not_published` status predates this GitHub
release and does not imply full benchmark acceptance.

The same status in the software delivery proof records verification before its
GitHub release. Publishing an inspection package does not change any task grade.

The archive contains the exact source used for that proof. The Git repository
adds public documentation, CI and a smaller example; use the archive's source
when reproducing its recorded hashes. Earlier attempt receipts are retained;
full traces cover the latest teacher for each task and the latest ordinary trial.

## Install the development environment

Python 3.12+ is required; verification used Python 3.13. On Linux, install Node/npm,
Git, bubblewrap and a Chromium browser, then:

```sh
uv sync --frozen --extra browser
uv run playwright install --with-deps chromium-headless-shell
export COMPANY_ENVS_BROWSER="$(uv run playwright install --dry-run chromium-headless-shell | sed -n 's/^  Install location: *//p' | head -1)/chrome-headless-shell-linux64/chrome-headless-shell"
uv run ruff check src scripts tests
uv run pytest -q
```

The test suite makes no live model calls. Browser tests require Chromium, Node
powers the small native hub fixture, and isolated verifier tests use bubblewrap.
These checks do not require the full external hub or a VM image.

## Replay in native apps

Replay additionally requires the CUA-Gym hub checkout at revision
`1e50b797200f8afd6f11ca8e3ee04412de97b0f2`, matching the shipped config. The hub and
VM image are external assets, not included in this repository or release. Use the
[public hub checkout instructions](RUNTIME.md#public-hub-checkout) to acquire the
pinned source. Native replay does not require a VM image. The runtime guide also
provides a desktop build recipe and identifies the historical pilot image.

Choose the preset matching the extracted task package: `configs/pilot-sanmar.toml`
for SanMar or `configs/thoughtbot.toml` for the software delivery company. Work in
an ignored local copy and set its machine paths. For example:

```sh
cp configs/pilot-sanmar.toml configs/inspection.local.toml
# Edit hub and other machine paths in the local config.
company_dir=$(python3 -c 'import json; print(json.load(open("COLLABORATOR.json"))["company"])')
uv run company-envs --config configs/inspection.local.toml doctor --folder "$company_dir"
uv run company-envs --config configs/inspection.local.toml preflight --scope runtime
uv run company-envs --config configs/inspection.local.toml pilot-status "$company_dir"
uv run company-envs --config configs/inspection.local.toml verify-collaborator \
  --work /tmp/agentcompanyfactory-replay
```

Use a fresh work directory. Verification starts app sessions, replays each reference
through worker proxies, runs the isolated Python verifier and checks reset. It does
not reseed the world or repeat model judgments. Model-profile checks in preflight
are relevant to future model work; they are not model calls or quota verification.

## New desktop trials

Desktop trials additionally need QEMU/KVM, sshpass, a configured Ubuntu GNOME image
and browser distribution. The guest uses Python GTK 3 bindings and xdotool for
clipboard text entry. Configure a model account outside the repository. Run
`doctor --profile desktop` and `preflight` before any paid work.

Follow [the stage guide](PIPELINE.md) for new attempts and their limits. Keep private
assessments, references and verifiers off worker desktops. Tasks from one company
share a world lineage and belong in the same benchmark split. Read the package's
actual trial results; publication does not change its acceptance status.
