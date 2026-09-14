"""Failed image preparation must preserve inputs and never publish an output."""

import hashlib
import importlib.util
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "build_desktop", Path(__file__).resolve().parents[1] / "scripts/build_desktop.py"
)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.mark.parametrize("existing", [False, True])
def test_rejected_inputs_never_start_a_guest_or_overwrite_an_image(tmp_path, monkeypatch, existing):
    source, output = tmp_path / "cloud.img", tmp_path / "desktop.qcow2"
    source.write_bytes(b"cloud image")
    if existing:
        output.write_bytes(b"accepted image")

    def forbidden(*args, **kwargs):
        pytest.fail("No subprocess may run for rejected inputs")

    monkeypatch.setattr(builder.subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="already exists" if existing else "SHA-256"):
        builder.build(source, "wrong digest", output)
    assert source.read_bytes() == b"cloud image"
    assert output.read_bytes() == b"accepted image" if existing else not output.exists()


def test_failed_provisioning_retains_log_and_does_not_publish(tmp_path, monkeypatch):
    source, output = tmp_path / "cloud.img", tmp_path / "desktop.qcow2"
    source.write_bytes(b"cloud image")
    monkeypatch.setattr(builder.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(builder.os, "access", lambda *args: True)

    def run(command, **kwargs):
        if command[0] == "qemu-system-x86_64":
            kwargs["stdout"].write("provisioning failed\n")
            raise subprocess.CalledProcessError(1, command)
        assert command[0] in {"qemu-img", "cloud-localds"}

    monkeypatch.setattr(builder.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        builder.build(source, hashlib.sha256(source.read_bytes()).hexdigest(), output)
    assert not output.exists()
    (log,) = tmp_path.glob("desktop-build-*/provision.log")
    assert log.read_text() == "provisioning failed\n"
    assert not list(tmp_path.glob("desktop-build-*/BUILD.json"))
