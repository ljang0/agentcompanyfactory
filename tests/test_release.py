"""Release integrity and portability with tiny files and a doubled git checkout."""

import io
import json
import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest

from company_envs.company_layout import export_company, verify_pins
from company_envs.storage import digest, read, write
from company_envs.world import release
from company_envs.world.hub_world import load_world

REVISION = "a" * 40
MODEL = "codex/gpt-6-astra"


@pytest.fixture
def exported(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "config.toml").write_text(
        f'[design]\nhub_revision = "{REVISION}"\nmodel_pin = "{MODEL}"\nhub_root = "hub/websites"\n'
    )

    def git(args, **kwargs):
        assert args == ["git", "-C", str(root / "hub/websites"), "rev-parse", "HEAD"]
        assert kwargs["timeout"] == 10
        return REVISION + "\n"

    monkeypatch.setattr("company_envs.company_layout.subprocess.check_output", git)
    schema = root / "catalogs/app_schemas/example_mock.md"
    schema.parent.mkdir(parents=True)
    schema.write_text("## State Schema\n\n| Key | Type |\n| --- | --- |\n| `records` | array |\n")
    write(
        root / "catalogs/manifest.json",
        {"files": {"app_schemas/example_mock.md": {"sha256": digest(schema.read_bytes())}}},
    )
    write(
        root / "catalogs/apps.json",
        {
            "apps": [
                {
                    "id": "example_mock",
                    "source": "cua_gym_hub",
                    "schema": str(schema),
                    "state_keys": ["records"],
                }
            ]
        },
    )
    write(
        root / "input/company.json",
        {
            "id": "example",
            "workers": [{"id": "boss"}],
            "software": [{"id": "records", "catalog_app_ids": ["example_mock"]}],
        },
    )
    write(
        root / "input/workflow.json",
        {
            "id": "task",
            "title": "Reconcile",
            "brief": "Reconcile records.",
            "software_requirement_ids": ["records"],
            "worker_ids": ["boss"],
        },
    )
    write(root / "input/review.json", {"verdict": "accept"})
    write(
        root / "runs/run/dataset.json",
        {
            "companies": {"example": "input/company.json"},
            "workflows": [
                {
                    "company_id": "example",
                    "workflow_id": "task",
                    "workflow_path": "input/workflow.json",
                    "review_path": "input/review.json",
                }
            ],
        },
    )
    patches = root / "patches.json"
    write(
        patches,
        {
            "example_mock": [{"file": "src/a.js", "old": "old", "new": "new", "reason": "fix"}],
            "unrelated_mock": [{"private": "must not be included"}],
        },
    )
    monkeypatch.setattr(release, "PATCHES_PATH", patches)
    folder, manifest = export_company(root, "run", "example")
    write(folder / "world/example_mock.state.json", {"records": []})
    for name in ("world", "identities", "worker_apps", "SEED", "CHECKS", "REVIEW"):
        write(folder / f"world/{name}.json", {})
    material = folder / "world/materials/boss/brief.txt"
    material.parent.mkdir(parents=True)
    material.write_text("Worker material")
    for name in ("grader", "golden", "reference"):
        write(folder / f"tasks/task/{name}.json", {"private": name})
    for relative in (
        "runtime/trace.json",
        "world/calls/request.json",
        "tasks/task/calls/prompt.json",
        "world/materials/boss/runtime/log.json",
        "world/traces/events.json",
    ):
        write(folder / relative, {"secret": "omit"})
    return root, folder, manifest


def contents(path):
    with tarfile.open(path, "r:gz") as archive:
        return {m.name: archive.extractfile(m).read() for m in archive}


def rewrite(path, members):
    with tarfile.open(path, "w:gz") as archive:
        for name, data in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


def test_export_bundle_verify_and_serve_inputs(exported, tmp_path):
    root, folder, manifest = exported
    pins = {
        "hub_revision": REVISION,
        "model_pin": MODEL,
        "catalog_manifest_sha256": digest((root / "catalogs/manifest.json").read_bytes()),
    }
    assert manifest["pins"] == pins
    assert verify_pins(root, folder)["ok"]
    original_manifest = (folder / "MANIFEST.json").read_bytes()
    target = release.bundle_company(root, folder, tmp_path / "release.tar.gz")
    report = release.verify_bundle(target)
    assert report["ok"], report
    assert report["pins"] == pins
    assert (folder / "MANIFEST.json").read_bytes() == original_manifest
    files = contents(target)
    assert not any(release.EXCLUDED.intersection(Path(name).parts) for name in files)
    for name in ("workflow", "grader", "golden", "reference"):
        assert files[f"tasks/task/private/{name}.json"] == (folder / f"tasks/task/{name}.json").read_bytes()
    assert files["tasks/task/public/assignment.json"] == (folder / "tasks/task/assignment.json").read_bytes()
    assert set(json.loads(files["hub_patches.json"])) == {"example_mock"}
    for name in ("world", "identities", "worker_apps", "SEED", "CHECKS", "REVIEW"):
        assert f"world/{name}.json" in files
    assert files["world/materials/boss/brief.txt"] == b"Worker material"
    # Move the archive's data away from the original repository: hub loading must
    # resolve the copied schema, state and identities without any source files.
    extracted = tmp_path / "extracted"
    for name, data in files.items():
        path = extracted / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assert load_world(extracted, root=extracted)[0]["state"] == {"records": []}
    assert tomllib.loads(files["config.toml"].decode())["design"]["hub_revision"] == REVISION
    second = release.bundle_company(root, folder, tmp_path / "again.tar.gz")
    assert second.read_bytes() == target.read_bytes()


@pytest.mark.parametrize("change", ["tamper", "missing", "extra", "pins", "catalog", "null_hash"])
def test_bundle_detects_corruption(exported, tmp_path, change):
    root, folder, _ = exported
    target = release.bundle_company(root, folder, tmp_path / "release.tar.gz")
    files = contents(target)
    if change == "tamper":
        files["world/example_mock.state.json"] = b'{"records": [1]}'
    elif change == "missing":
        del files["company.json"]
    elif change == "extra":
        files["extra.txt"] = b"unlisted"
    elif change == "pins":
        record = json.loads(files["RELEASE.json"])
        record["pins"]["hub_revision"] = "b" * 40
        files["RELEASE.json"] = json.dumps(record).encode()
    elif change == "null_hash":
        record = json.loads(files["RELEASE.json"])
        record["files"]["absent.txt"] = None
        files["RELEASE.json"] = json.dumps(record).encode()
    else:
        # Even if both hash maps are recomputed, the catalog must match its pin.
        files["catalogs/manifest.json"] = b'{"files": {}}'
        manifest = json.loads(files["MANIFEST.json"])
        manifest["hashes"]["catalogs/manifest.json"] = digest(files["catalogs/manifest.json"])
        files["MANIFEST.json"] = json.dumps(manifest).encode()
        record = json.loads(files["RELEASE.json"])
        record["files"] = {name: digest(data) for name, data in files.items() if name != "RELEASE.json"}
        files["RELEASE.json"] = json.dumps(record).encode()
    rewrite(target, files)
    report = release.verify_bundle(target)
    assert not report["ok"]
    assert report["errors"]


@pytest.mark.parametrize("drift", ["model_pin", "hub_revision", "catalog", "checkout", "no_git", "no_pins"])
def test_pin_drift_blocks_bundling(exported, tmp_path, monkeypatch, drift):
    root, folder, _ = exported
    if drift in {"model_pin", "hub_revision"}:
        config = root / "config.toml"
        old, new = (MODEL, "codex/other") if drift == "model_pin" else (REVISION, "b" * 40)
        config.write_text(config.read_text().replace(old, new))
    elif drift == "catalog":
        write(root / "catalogs/manifest.json", {"changed": True})
    elif drift == "checkout":
        monkeypatch.setattr("company_envs.company_layout.subprocess.check_output", lambda *a, **kw: "b" * 40)
    elif drift == "no_git":

        def missing(*args, **kwargs):
            raise subprocess.CalledProcessError(128, "git")

        monkeypatch.setattr("company_envs.company_layout.subprocess.check_output", missing)
    else:
        manifest = read(folder / "MANIFEST.json")
        del manifest["pins"]
        write(folder / "MANIFEST.json", manifest)
    report = verify_pins(root, folder)
    assert not report["ok"]
    assert report["drift"]
    target = tmp_path / "release.tar.gz"
    with pytest.raises(ValueError, match="pin drift"):
        release.bundle_company(root, folder, target)
    assert not target.exists()


@pytest.mark.parametrize(
    "name", [".", "../escape", "/absolute", "world/calls/request", "tasks/task/workflow.json"]
)
def test_verifier_rejects_unsafe_or_private_flat_members(exported, tmp_path, name):
    root, folder, _ = exported
    target = release.bundle_company(root, folder, tmp_path / "release.tar.gz")
    files = contents(target)
    files[name] = b"secret"
    rewrite(target, files)
    assert not release.verify_bundle(target)["ok"]


@pytest.mark.parametrize("kind", ["symlink", "duplicate"])
def test_verifier_rejects_links_and_duplicate_members(exported, tmp_path, kind):
    root, folder, _ = exported
    target = release.bundle_company(root, folder, tmp_path / "release.tar.gz")
    files = contents(target)
    with tarfile.open(target, "w:gz") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        member = tarfile.TarInfo("company.json")
        if kind == "symlink":
            member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
        archive.addfile(member)
    assert not release.verify_bundle(target)["ok"]


def test_missing_schema_or_world_fails_before_writing(exported, tmp_path):
    root, folder, _ = exported
    (folder / "world/example_mock.state.json").unlink()
    with pytest.raises(ValueError, match="state_file"):
        release.bundle_company(root, folder, tmp_path / "release.tar.gz")
    write(folder / "world/example_mock.state.json", {"records": []})
    (root / "catalogs/app_schemas/example_mock.md").unlink()
    with pytest.raises(ValueError, match="missing release file"):
        release.bundle_company(root, folder, tmp_path / "release.tar.gz")


def test_bundler_rejects_symlinks_and_clobbering(exported, tmp_path):
    root, folder, _ = exported
    target = release.bundle_company(root, folder, tmp_path / "release.tar.gz")
    original = target.read_bytes()
    with pytest.raises(FileExistsError):
        release.bundle_company(root, folder, target)
    assert target.read_bytes() == original
    material = folder / "world/materials/boss/link"
    material.symlink_to(root / "input/workflow.json")
    with pytest.raises(ValueError, match="symlink"):
        release.bundle_company(root, folder, tmp_path / "another.tar.gz")


def test_split_task_inputs_do_not_duplicate_or_publish_private_files(exported, tmp_path):
    root, folder, _ = exported
    task = folder / "tasks/task"
    for path in list(task.glob("*.json")):
        destination = task / ("public" if path.name == "assignment.json" else "private")
        destination.mkdir(exist_ok=True)
        path.rename(destination / path.name)
    target = release.bundle_company(root, folder, tmp_path / "release.tar.gz")
    assert release.verify_bundle(target)["ok"]
    write(task / "assignment.json", {"brief": "ambiguous"})
    with pytest.raises(ValueError, match="duplicate task destination"):
        release.bundle_company(root, folder, tmp_path / "duplicate.tar.gz")


@pytest.mark.parametrize("directory", ["grader_calls", "golden_calls"])
def test_bundle_excludes_named_model_call_directories(exported, tmp_path, directory):
    root, folder, _ = exported
    write(folder / f"tasks/task/{directory}/attempt-001/receipt.json", {"private_trace": "omit"})
    target = release.bundle_company(root, folder, tmp_path / "release.tar.gz")
    assert not any(directory in Path(name).parts for name in contents(target))


@pytest.mark.parametrize("name", ["workflow.json", "grader.json", "golden.json", "reference.json"])
def test_private_task_files_cannot_be_worker_materials(exported, tmp_path, name):
    root, folder, _ = exported
    write(folder / "world/materials/boss" / name, {"private": "solution"})
    with pytest.raises(ValueError, match="private.*material"):
        release.bundle_company(root, folder, tmp_path / "release.tar.gz")


@pytest.mark.parametrize("change", ["boolean_version", "unknown_field", "duplicate_key"])
def test_verify_rejects_malformed_release_manifest(exported, tmp_path, change):
    root, folder, _ = exported
    target = release.bundle_company(root, folder, tmp_path / "release.tar.gz")
    files = contents(target)
    record = json.loads(files["RELEASE.json"])
    if change == "boolean_version":
        record["version"] = True
    elif change == "unknown_field":
        record["approved"] = True
    files["RELEASE.json"] = json.dumps(record).encode()
    if change == "duplicate_key":
        files["RELEASE.json"] = b'{"version": 999,' + files["RELEASE.json"][1:]
    rewrite(target, files)
    assert not release.verify_bundle(target)["ok"]


def test_private_task_copy_cannot_be_disguised_as_worker_notes(exported, tmp_path):
    root, folder, _ = exported
    source = folder / "tasks/task/workflow.json"
    (folder / "world/materials/boss/notes.txt").write_bytes(source.read_bytes())
    with pytest.raises(ValueError, match="private.*material"):
        release.bundle_company(root, folder, tmp_path / "release.tar.gz")


@pytest.mark.parametrize("name", ["episode-result.json", "events.jsonl", "screen-000001.png"])
def test_bundle_omits_loose_runtime_evidence(exported, tmp_path, name):
    root, folder, _ = exported
    (folder / "world" / name).write_bytes(b"runtime evidence")
    target = release.bundle_company(root, folder, tmp_path / "release.tar.gz")
    assert f"world/{name}" not in contents(target)
