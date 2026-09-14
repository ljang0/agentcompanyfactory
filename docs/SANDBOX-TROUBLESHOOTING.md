# Host prerequisites and permissions

`doctor` checks configured files, executables and runtime prerequisites. `preflight`
measures TCP/loopback access and disposable writes to the configured model profile.
Neither calls a model or proves provider quota is available.

```sh
uv run company-envs --config configs/pilot-sanmar.local.toml doctor --profile desktop
uv run company-envs --config configs/pilot-sanmar.local.toml preflight
```

| Symptom | Check |
| --- | --- |
| Loopback socket denied | The execution environment must allow local app servers and worker proxies |
| Model profile is read-only | Configure a writable authenticated profile outside the repository |
| Bubblewrap namespace creation denied | Use a Linux host allowing the verifier's user, PID and network namespaces |
| KVM unavailable | Install QEMU/KVM and grant the trial user access to `/dev/kvm` |
| Browser missing | Set the browser location in the local config or `COMPANY_ENVS_BROWSER` |
| `model_unavailable` | Inspect the provider receipt; local auth-file presence does not establish available capacity |

`permissions-config` can generate settings from the selected config; it does not
apply host permissions. Use `company-envs permissions-config --help` to inspect its
options. Keep machine paths and account settings in ignored `*.local.toml` files.

A blocked prerequisite is an environment result. Preserve its receipt and fix the
host setup before retrying; do not report it as a task failure or a passing check.
