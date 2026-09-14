# Setup and inspect the SanMar run

You can read the [small example](../examples/sanmar/README.md) on GitHub without
installing anything. To inspect native states, files, screenshots and replay proof,
use the full [September 14 inspection release](https://github.com/ljang0/agentcompanyfactory/releases/tag/sanmar-inspection-2026-09-14).

## Download the measured snapshot

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
VM image are external assets, not included in this repository or release. Obtain
compatible copies from their distributor or the project maintainer. Access to
these assets is a remaining requirement for reproducing the native/desktop runs.

From the extracted snapshot:

```sh
cp configs/pilot-sanmar.toml configs/pilot-sanmar.local.toml
# Edit hub and other machine paths in the local config.
uv run company-envs --config configs/pilot-sanmar.local.toml doctor \
  --folder experiments/pilot-sanmar/company
uv run company-envs --config configs/pilot-sanmar.local.toml preflight
uv run company-envs --config configs/pilot-sanmar.local.toml pilot-status \
  experiments/pilot-sanmar/company
uv run company-envs --config configs/pilot-sanmar.local.toml verify-collaborator \
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

Follow [the stage guide](PILOT.md) for new attempts and their limits. Keep private
assessments, references and verifiers off worker desktops. Both example tasks share
one world lineage and belong in the same benchmark split. Ordinary-team and
ablation acceptance remains incomplete; publication does not change the grades.
