# Use this repository as a template

Use **Use this template → Create a new repository** on GitHub to start an independent
project with this source, configuration, documentation and CI.

You can also use the GitHub CLI. Replace `OWNER` and `YOUR_PROJECT`:

```sh
gh repo create OWNER/YOUR_PROJECT --template ljang0/agentcompanyfactory --private --clone
cd YOUR_PROJECT
make setup
make doctor
```

Choose public visibility instead if your project should be public from the start.
Creating from a template gives your project its own Git history. Fork this repository
instead when you intend to contribute changes back through pull requests.

## Make it your project

1. Set the project name, description and repository links in `README.md`. Update
   the CI badge URL and any support links that should point to your repository.
2. Edit the ignored `configs/company.local.toml` with your model and runtime setup.
   Commit reusable, credential-free presets as separate example configurations.
3. Put new company outputs under `workspace/companies/` and research runs under
   `runs/`. Both are ignored. Add a curated example under `examples/` only when you
   are ready to share it.
4. Keep the SanMar example as a reference or replace the public example index with
   your own. Its grades belong to the published snapshot and should not be relabeled
   as results from a new project.
5. Run `make check`, update the project documentation and open a pull request.

Keep the upstream license and copyright notices. Add your own notices where
appropriate for your contributions. The Python package and CLI can keep their
existing names; renaming them is a separate code and packaging change.

## What is included

- A Python package with staged CLI commands and a locked dependency set.
- A small starter configuration and example tasks with provenance.
- Linux CI for lint, regression tests, browser checks and isolated verification.
- Issue forms, a pull request template and contribution instructions.
- Source maps and guides for changing apps, authoring, graders and trial policies.

The template does not contain model credentials, the external hub checkout, VM
images or large trial recordings. [Getting started](GETTING-STARTED.md) lists those
requirements; the upstream inspection archive remains available from its release.
