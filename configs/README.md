# Configuration

Start from `company.example.toml`. `make setup` copies it to the ignored
`company.local.toml` without replacing an existing file. Use `pilot-sanmar.toml`
when inspecting the measured SanMar snapshot.

Configuration precedence is the command's `--config`, then `COMPANY_ENVS_CONFIG`,
then the repository's `config.toml`. Relative paths resolve beside the selected
TOML file. A missing explicitly selected file is an error. Resumed research runs
keep the configuration captured when they were created.

| Section | Controls |
| --- | --- |
| `generation` | Company/task/worker counts, reviewers, repair limits and authoring timeouts |
| `design` | App surface, runtime and model pins, native volume limits, VM assets and worker call budget |
| `models` | Models used by authoring and review, reasoning and account profiles |
| `diversity` | Embedding model and comparison settings |
| `worker_policy` | Per-worker actions, execution time and policy selection |

The starter config uses the example's measured runtime revision and model names.
Change them to match your environment before paid generation. An accepted world
binds these inputs; changing them can require new acceptance evidence.

Account files and machine paths belong in local configs. The tracked root
`config.toml` retains the broader development defaults used by existing tests;
select your local config explicitly for new company work.
