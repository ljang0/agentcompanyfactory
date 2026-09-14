"""Select one configuration for a command and its subprocesses.

Relative filesystem settings resolve beside the TOML file. The default file is
still ROOT/config.toml; existing run snapshots remain frozen by the run loader.
"""

import os
import tomllib
from contextlib import contextmanager
from pathlib import Path

CONFIG_ENV = "COMPANY_ENVS_CONFIG"


def config_path(root, path=None):
    selected = path or os.environ.get(CONFIG_ENV)
    return Path(selected).expanduser().resolve() if selected else Path(root).resolve() / "config.toml"


def load_config(root, path=None, *, missing_ok=False):
    source = config_path(root, path)
    # Optional legacy defaults must never hide a misspelled explicit override.
    if missing_ok and not source.exists() and not path and not os.environ.get(CONFIG_ENV):
        return {}
    config = tomllib.loads(source.read_text())

    def resolve(value):
        if not isinstance(value, str) or not value:
            return value
        target = Path(value).expanduser()
        return str(target if target.is_absolute() else (source.parent / target).resolve())

    design = config.get("design", {})
    if isinstance(design, dict):
        for key in ("hub_root", "vm_base_image", "vm_browser_dir"):
            if key in design:
                design[key] = resolve(design[key])
    models = config.get("models", {})
    if isinstance(models, dict) and isinstance(models.get("codex_homes"), list):
        models["codex_homes"] = [resolve(home) for home in models["codex_homes"]]
    return config


@contextmanager
def selected_config(path):
    """Inherit the selection in child commands without leaking it across CLI calls."""
    previous = os.environ.get(CONFIG_ENV)
    if path is not None:
        os.environ[CONFIG_ENV] = str(Path(path).expanduser().resolve())
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(CONFIG_ENV, None)
        else:
            os.environ[CONFIG_ENV] = previous
