"""Portable company data releases; no runtime evidence or generation call logs.

RELEASE.json hashes every other member (a manifest cannot hash itself). Verification
establishes integrity and pin consistency, not authenticity or business readiness.
"""

import gzip
import io
import json
import re
import tarfile
import tomllib
from pathlib import Path, PurePosixPath

from company_envs.company_layout import pin_errors, verify_pins
from company_envs.storage import digest, read

from .hub_app import PATCHES_PATH

EXCLUDED = {
    "runtime",
    "calls",
    "grader_calls",
    "golden_calls",
    "trace",
    "traces",
    "episode.json",
    "episode-result.json",
    "events.jsonl",
    "messages.jsonl",
}
PRIVATE_TASK_FILES = {
    "workflow.json",
    "grader.json",
    "golden.json",
    "golden.author.json",
    "reference.json",
    "assessment.json",
    "WORLD.json",
}
REQUIRED = {
    "MANIFEST.json",
    "company.json",
    "review.json",
    "apps.json",
    "README.md",
    "config.toml",
    "catalogs/manifest.json",
    "hub_patches.json",
    "world/world.json",
    "world/identities.json",
}
RELEASE_README = """# Company release

This archive contains company data, schemas and app patches. Install company-envs
with Python 3.12+, Node/npm, and a CUA-Gym checkout at the hub_revision in
RELEASE.json. Hub source, dependencies, VMs and model access are separate prerequisites.
The model_pin records the configured provider/model name, not provider-side weights.

Verify the archive with `company-envs verify-release company.tar.gz` before extracting
it into an empty directory. From that directory, serve the company with:

```sh
company-envs --root . hub-serve . --hub-root /path/to/CUA-Gym/hub/websites
```

The release-aware hub-serve CLI uses this folder's hub_patches.json. The bundled
config contains pins only; --hub-root explicitly selects your local checkout.
Schema paths in apps.json resolve against this directory. Use --check for a
seed/readback/proxy check followed by shutdown. Runtime evidence is created afresh.

tasks/<id>/public/assignment.json is the public boss brief. Everything under
tasks/<id>/private/ is controller-only, including workflows, graders and golden
references when supplied. Current episode/grader commands expect flat task files:
copy a task's public and private files into a separate controller working folder
before using those commands. Never mount the release or private task data on VMs;
deliver only each worker's own materials and app URLs.

RELEASE.json hashes all other files; MANIFEST.json hashes the packaged payload
excluding the two manifests. Hashes detect corruption, not a maliciously replaced
manifest. Local config/catalog/checkout drift can additionally be checked with
verify_pins(repository_root, extracted_folder). Recorded review files do not by
themselves certify that the world or a real worker episode succeeds.
"""


def _json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _excluded(path):
    return bool(EXCLUDED.intersection(path.parts)) or any(
        re.fullmatch(r"screen-[0-9]+\.png", part) for part in path.parts
    )


def _name(name):
    path = PurePosixPath(name)
    if (
        not name
        or not path.parts
        or "\x00" in name
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or path.as_posix() != name
        or _excluded(path)
    ):
        raise ValueError(f"unsafe or excluded release path: {name!r}")
    return path


def _file(base, name):
    relative = _name(name)
    path = base
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"symlinks are not release inputs: {path}")
    if not path.is_file():
        raise ValueError(f"missing release file: {path}")
    return path.read_bytes()


def _tree(base, relative):
    directory = base / relative
    if directory.is_symlink():
        raise ValueError(f"symlinks are not release inputs: {directory}")
    for path in sorted(directory.rglob("*")):
        name = path.relative_to(base).as_posix()
        if _excluded(PurePosixPath(name)):
            continue
        if path.is_symlink():
            raise ValueError(f"symlinks are not release inputs: {path}")
        if not path.is_dir():
            yield name, _file(base, name)


def _contents_errors(files):
    """Check structural requirements, referenced assets and internal pins."""
    errors = [f"missing file: {name}" for name in sorted(REQUIRED - files.keys())]
    if errors:
        return errors
    manifest = json.loads(files["MANIFEST.json"])
    pins = manifest.get("pins")
    errors.extend(pin_errors(pins))
    if errors:
        return errors
    if digest(files["catalogs/manifest.json"]) != pins["catalog_manifest_sha256"]:
        errors.append("catalog manifest does not match catalog_manifest_sha256 pin")
    design = tomllib.loads(files["config.toml"].decode())["design"]
    for key in ("hub_revision", "model_pin"):
        if design.get(key) != pins[key]:
            errors.append(f"config pin mismatch: {key}")
    catalog = json.loads(files["catalogs/manifest.json"])
    apps = json.loads(files["apps.json"])["apps"]
    app_ids = {app["app_id"] for app in apps}
    if set(json.loads(files["hub_patches.json"])) - app_ids:
        errors.append("hub patches include apps outside apps.json")
    for app in apps:
        if not app.get("hub_seedable"):
            errors.append(f"app is not hub seedable: {app['app_id']}")
        for key, prefix in (("schema", "catalogs/app_schemas/"), ("state_file", "world/")):
            name = app[key]
            _name(name)
            if not name.startswith(prefix) or name not in files:
                errors.append(f"missing or misplaced {key}: {name}")
        schema = app["schema"]
        entry = catalog.get("files", {}).get(schema.removeprefix("catalogs/"), {})
        if schema in files and entry.get("sha256") != digest(files[schema]):
            errors.append(f"schema differs from pinned catalog: {schema}")
    tasks = manifest["tasks"]
    if not tasks or len(tasks) != len(set(tasks)):
        errors.append("manifest must declare unique tasks")
    for task in tasks:
        _name(task)
        for relative in ("public/assignment.json", "private/workflow.json"):
            name = f"tasks/{task}/{relative}"
            if name not in files:
                errors.append(f"missing file: {name}")
    private_hashes = {
        digest(content)
        for name, content in files.items()
        if PurePosixPath(name).parts[0] == "tasks" and PurePosixPath(name).name in PRIVATE_TASK_FILES
    }
    for name in files:
        parts = PurePosixPath(name).parts
        if parts[:2] == ("world", "materials") and (
            PRIVATE_TASK_FILES.intersection(parts) or digest(files[name]) in private_hashes
        ):
            errors.append(f"private task file in worker materials: {name}")
        if parts[0] == "tasks" and (
            len(parts) < 4
            or parts[1] not in tasks
            or (parts[2] != "private" and parts[2:] != ("public", "assignment.json"))
        ):
            errors.append(f"task file outside declared public/private boundary: {name}")
    expected = {
        name: digest(content)
        for name, content in files.items()
        if name not in {"MANIFEST.json", "RELEASE.json"}
    }
    if manifest.get("hashes") != expected:
        errors.append("MANIFEST.json payload hashes do not match")
    return errors


def bundle_company(root, folder, output_tar):
    """Bundle a pinned export and its seeded world without changing the source.

    Refuse drift, missing required assets, symlinks and existing outputs. Retain
    optional world reviews/checks and task graders/goldens if authored; never
    fabricate them. Return the resulting archive path.
    """
    root, folder, output_tar = Path(root).resolve(), Path(folder).resolve(), Path(output_tar).absolute()
    if output_tar.resolve().is_relative_to(folder):
        raise ValueError("release output must be outside the company folder")
    statuses = list((folder / "tasks").glob("*/STATUS.json"))
    # The controller's working copies: one per task runtime, plus the single-checkpoint layout.
    statuses.extend(folder.glob("runtime/tasks/*/controller/company/tasks/*/STATUS.json"))
    statuses.extend((folder / "runtime/controller/company/tasks").glob("*/STATUS.json"))
    for status_path in statuses:
        status = read(status_path)
        if status.get("status") == "discarded":
            raise ValueError(f"discarded task {status_path.parent.name}: {status.get('reason')}")
    report = verify_pins(root, folder)
    if not report["ok"]:
        raise ValueError(f"pin drift: {report['drift']}")
    files = {name: _file(folder, name) for name in ("company.json", "review.json", "apps.json")}
    manifest = json.loads(_file(folder, "MANIFEST.json"))
    for name, content in _tree(folder, "tasks"):
        parts = PurePosixPath(name).parts
        if len(parts) < 3:
            raise ValueError(f"task file has no task id: {name}")
        relative = parts[2:]
        if relative == ("assignment.json",):
            relative = ("public", *relative)
        elif relative[0] not in {"public", "private"}:
            relative = ("private", *relative)
        target = "/".join((*parts[:2], *relative))
        if target in files:
            raise ValueError(f"duplicate task destination: {target}")
        files[target] = content
    files.update(_tree(folder, "world"))
    apps = json.loads(files["apps.json"])["apps"]
    for app in apps:
        schema = app["schema"]
        if not isinstance(schema, str) or not schema.startswith("catalogs/app_schemas/"):
            raise ValueError(f"app schema must be in catalogs/app_schemas: {schema!r}")
        files[schema] = _file(root, schema)
    files["catalogs/manifest.json"] = _file(root, "catalogs/manifest.json")
    patches = read(PATCHES_PATH)
    files["hub_patches.json"] = _json_bytes(
        {app["app_id"]: patches[app["app_id"]] for app in apps if app["app_id"] in patches}
    )
    pins = manifest["pins"]
    files["config.toml"] = (
        "[design]\n" + "".join(f"{key} = {json.dumps(pins[key])}\n" for key in ("hub_revision", "model_pin"))
    ).encode()
    files["README.md"] = RELEASE_README.encode()
    manifest["hashes"] = {name: digest(content) for name, content in sorted(files.items())}
    files["MANIFEST.json"] = _json_bytes(manifest)
    errors = _contents_errors(files)
    if errors:
        raise ValueError(f"incomplete release: {errors}")
    files["RELEASE.json"] = _json_bytes(
        {
            "version": 1,
            "pins": pins,
            "files": {name: digest(content) for name, content in sorted(files.items())},
        }
    )
    output_tar.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids clobbering another job's release. Stable metadata
    # makes repeated bundles of the same input byte-for-byte identical.
    with output_tar.open("xb") as raw:
        try:
            with (
                gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
                tarfile.open(fileobj=compressed, mode="w") as archive,
            ):
                for name, content in sorted(files.items()):
                    member = tarfile.TarInfo(name)
                    member.size, member.mode = len(content), 0o644
                    archive.addfile(member, io.BytesIO(content))
        except BaseException:
            output_tar.unlink()
            raise
    return output_tar


def verify_bundle(tar):
    """Rehash an archive without extraction and return {ok, errors, pins, files}.

    Pin checks are self-contained: RELEASE, MANIFEST, config and copied catalog
    must agree. Use verify_pins separately to compare a local repository/checkout.
    """
    errors, files, pins = [], {}, None
    try:
        with tarfile.open(tar, "r:gz") as archive:
            for member in archive:
                _name(member.name)
                if not member.isfile() or member.name in files:
                    raise ValueError(f"non-regular or duplicate archive member: {member.name}")
                files[member.name] = archive.extractfile(member).read()
        release = json.loads(files["RELEASE.json"], object_pairs_hook=_unique_object)
        if not isinstance(release, dict) or set(release) != {"version", "pins", "files"}:
            raise ValueError("RELEASE.json requires exactly version, pins and files")
        pins = release.get("pins")
        errors.extend(pin_errors(pins))
        if type(release.get("version")) is not int or release["version"] != 1:
            errors.append("unsupported release version")
        hashes = release["files"]
        actual = {name: digest(content) for name, content in files.items() if name != "RELEASE.json"}
        for name in sorted(actual.keys() | hashes.keys()):
            if name not in actual or name not in hashes or actual[name] != hashes[name]:
                errors.append(f"file missing, unlisted or sha256 mismatch: {name}")
        if pins != json.loads(files["MANIFEST.json"]).get("pins"):
            errors.append("RELEASE.json pins differ from MANIFEST.json")
        errors.extend(_contents_errors(files))
    except (OSError, EOFError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError) as exc:
        errors.append(f"invalid release: {exc}")
    return {"ok": not errors, "errors": errors, "pins": pins, "files": len(files)}
