# Scripts

Use `company-envs` for the company pipeline. Its entrypoints and checkpoints are
documented in [the pipeline guide](../docs/PIPELINE.md). Scripts here support
development, runtime setup and inspection.

- `check.sh`: lint/test workflow; passing a commit message also commits and pushes.
- `browser_path.py`: select the browser matching the installed Playwright version.
  `--guest` prints the full Linux browser directory; the default prints the host
  headless-shell executable.
- `build_desktop.py`: build a new desktop image from a checksum-verified Ubuntu
  cloud image. See [runtime setup](../docs/RUNTIME.md).
- App audit, probe, render and volume scripts inspect native app behavior and
  contracts. Read each script's arguments before starting live app sessions.
- [pilots/sanmar/](pilots/sanmar/README.md): historical company-specific population
  helpers retained for inspection. New companies use the staged CLI.

Keep reusable algorithms in `src/company_envs/` and company-specific inputs in
company folders. Avoid adding another orchestration script when an existing CLI
entrypoint can express the operation.
