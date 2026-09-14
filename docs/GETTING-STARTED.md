# Getting started

Choose the level of execution you need. Reading the example requires no setup;
building a new company and running desktop teams requires external runtime assets.

| Goal | Requirements | First step |
| --- | --- | --- |
| Inspect the project and outputs | Web browser | [Example index](../examples/sanmar/README.md) |
| Develop and run checks | Python 3.12+, uv, Git; Node/npm, Linux bubblewrap and Chromium for runtime tests | `make setup`, then `make check` |
| Replay the published company | Development tools plus the pinned CUA-Gym hub | [Snapshot replay](COLLABORATOR.md) |
| Build a new company | Compatible hub and configured model account | Configure below, then use the stage commands |
| Run real desktop teams | Above plus QEMU/KVM, sshpass, a compatible desktop image and browser distribution | `doctor --profile desktop`, then `trial-task` |

## 1. Install and check the source

```sh
git clone https://github.com/ljang0/agentcompanyfactory.git
cd agentcompanyfactory
make setup
make doctor
```

`make setup` runs `uv sync --frozen --extra browser` and copies
`configs/company.example.toml` to `configs/company.local.toml` if it does not already
exist. `make doctor` checks configuration, catalogs and pins without model calls.
The repository includes its catalogs; `bootstrap` is only needed when deliberately
importing a different reference catalog.

Without Make, run the same steps directly:

```sh
uv sync --frozen --extra browser
cp configs/company.example.toml configs/company.local.toml
uv run company-envs --config configs/company.local.toml doctor
```

Do not overwrite an existing local configuration. Use Python 3.13 to match CI.

With Node.js on `PATH`, check the bundled native app without model access:

```sh
make smoke
```

Expected result: `2 passed`. The checks cover seed/read/write, worker proxy access,
session isolation, file upload and reset. They use a real local Node server and
bundled build fixture; no external hub, npm download or VM is required. Browser
rendering is checked when a compatible browser is available. This checks runtime
plumbing, not a generated company's acceptance.

## 2. Configure the runtime

Edit `configs/company.local.toml`. Paths are resolved relative to that file, and
`~` expands to your home directory. The file is ignored by Git.

| Setting | What to configure |
| --- | --- |
| `design.hub_root` | Path to the compatible CUA-Gym `hub/websites` checkout |
| `design.hub_revision` | Exact revision that provides the app contracts being used |
| `design.available_runtime_apps` | Apps the company can use; each needs a catalog contract |
| `design.model_pin`, `models.*` | Model identifiers available to your account |
| `models.codex_homes` | Authenticated model profile directories; an empty list uses the default profile |
| `design.vm_base_image`, `design.vm_browser_dir` | Desktop image and guest browser distribution for VM trials |
| `generation.*`, `design.*`, `worker_policy.*` | Company size, review counts, generation budget and trial limits |

The starter uses the eight-app surface and runtime revision measured in the
included example. These are starting values, not a promise that other apps or
model configurations have been validated. See [configuration](../configs/README.md).

Follow [runtime assets](RUNTIME.md) for the pinned public hub checkout, desktop
image build recipe, browser selection and resource requirements. These assets
are acquired separately from the Python package.

Check the capabilities needed for the next stage:

```sh
uv run company-envs --config configs/company.local.toml doctor --profile generate
uv run company-envs --config configs/company.local.toml preflight --scope models
uv run company-envs --config configs/company.local.toml preflight --scope runtime
```

These checks make no inference calls. A present account file does not establish
provider quota. [Host troubleshooting](SANDBOX-TROUBLESHOOTING.md) explains failures.

## 3. Start from accepted research

If you already have an accepted research run, export its dossier into a fresh
company directory:

```sh
uv run company-envs --config configs/company.local.toml export-company \
  RUN_ID COMPANY_ID --dossier-only --output workspace/companies/COMPANY_ID
uv run company-envs --config configs/company.local.toml doctor \
  --folder workspace/companies/COMPANY_ID
```

Replace `RUN_ID` and `COMPANY_ID` with identifiers from your run. Export refuses an
existing destination and checks captured evidence and provenance.

To obtain new research, the current entrypoint is:

```sh
uv run company-envs --config configs/company.local.toml generate \
  --companies 1 --tasks 2 --workers 4
```

This makes model calls and creates a run under `runs/`. The existing research
entrypoint also performs legacy workflow drafting. Export with `--dossier-only`
to use its accepted research as input to the shared-world pipeline; tasks for that
pipeline are created later, after the world is frozen. Review the run's report and
saved company IDs before exporting. Resume an interrupted research run with
`generate --resume RUN_ID` to reuse its checkpoints.

## 4. Build, accept and evaluate the world

Follow the [eight-stage pipeline](PIPELINE.md), beginning with `seed-core` on the
exported folder. Stop at each failed acceptance gate and inspect its receipt before
repairing or retrying. Do not paste the whole stage table into a shell: authoring
and review use model calls, and some population work still requires company-specific
adaptation.

For an already prepared company, inspect its status without inference:

```sh
make status COMPANY=workspace/companies/COMPANY_ID
```

To inspect the measured SanMar run instead, download the release and follow
[snapshot replay](COLLABORATOR.md). The small example stored in Git does not contain
the full world needed to run these commands.
