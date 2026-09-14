"""Guest tar transfers with transport doubles; no SSH processes or VMs."""

import asyncio
import base64
import hashlib
import io
import tarfile

import pytest

from company_envs.storage import read, write
from company_envs.world.backends.mypcbench import MyPCBenchBackend, SSHTransport
from company_envs.world.documents import GRADED_FOLDERS


def archive_bytes(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in entries:
            member = tarfile.TarInfo(name)
            if content is None:
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
                archive.addfile(member)
            else:
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


class TarTransport(SSHTransport):
    def __init__(self, data, *, code=0, truncated=False):
        self.data, self.code, self.truncated = data, code, truncated
        self.calls = []

    async def run(self, command, *, deadline, limit):
        self.calls.append((command, deadline, limit))
        return {
            "stdout": base64.b64encode(self.data).decode(),
            "exit_code": self.code,
            "truncated": {"stdout": self.truncated},
        }


def backend(tmp_path, transport, worker="worker"):
    record = tmp_path / "runtime/vms" / worker / "vm.json"
    write(record, {"worker": worker, "dry_run": False, "status": "running"})
    return MyPCBenchBackend(record, transport=transport)


def export(vm, destination):
    async def run():
        return await vm.export_files(destination, deadline=asyncio.get_running_loop().time() + 5)

    return asyncio.run(run())


def test_tar_export_keeps_names_bytes_hashes_and_skips_links(tmp_path):
    entries = [
        ("Desktop/日本語 note.txt", b"ready\n"),
        ("Documents/budget.xlsx", b"\x00\xff"),
        ("Downloads/nested/order.pdf", b"pdf"),
        ("Desktop/link", None),
    ]
    transport = TarTransport(archive_bytes(entries))
    vm = backend(tmp_path, transport)
    destination = tmp_path / "exports/task/worker"
    result = export(vm, destination)
    manifest = read(destination / "manifest.json")
    assert result["status"] == "exported" and manifest["worker_id"] == "worker"
    assert manifest["skipped"] == ["Desktop/link"]
    for name, content in entries[:-1]:
        assert (destination / name).read_bytes() == content
        assert manifest["files"][name] == {
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    assert not (destination / "Desktop/link").exists()
    command, _, limit = transport.calls[0]
    assert "cd /home/ga" in command and "Desktop Documents Downloads" in command
    assert "tar -cf -" in command and limit == 64 * 1024 * 1024
    with pytest.raises(FileExistsError):
        export(vm, destination)


def test_absent_guest_directories_export_an_empty_manifest(tmp_path):
    destination = tmp_path / "exports/task/worker"
    assert export(backend(tmp_path, TarTransport(archive_bytes([]))), destination)["files"] == {}
    assert read(destination / "manifest.json")["files"] == {}


def test_every_downloaded_folder_and_directory_member_can_be_exported(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for folder in GRADED_FOLDERS:
            directory = tarfile.TarInfo(folder)
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)
            file = tarfile.TarInfo(f"{folder}/evidence.txt")
            file.size = 5
            archive.addfile(file, io.BytesIO(b"proof"))
    destination = tmp_path / "exports/worker"
    result = export(backend(tmp_path, TarTransport(buffer.getvalue())), destination)
    assert set(result["files"]) == {f"{folder}/evidence.txt" for folder in GRADED_FOLDERS}


@pytest.mark.parametrize("name", ["../escape", "/tmp/escape", "Documents/../../escape", "secret/data"])
def test_tar_paths_cannot_escape_export(tmp_path, name):
    destination = tmp_path / "exports/task/worker"
    vm = backend(tmp_path, TarTransport(archive_bytes([("Desktop/ok.txt", b"ok"), (name, b"bad")])))
    with pytest.raises(ValueError, match="unsafe"):
        export(vm, destination)
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


@pytest.mark.parametrize("data,code,truncated", [(b"bad tar", 0, False), (b"", 1, False), (b"", 0, True)])
def test_failed_or_incomplete_download_never_publishes(tmp_path, data, code, truncated):
    destination = tmp_path / "exports/task/worker"
    with pytest.raises((RuntimeError, tarfile.ReadError)):
        export(backend(tmp_path, TarTransport(data, code=code, truncated=truncated)), destination)
    assert not destination.exists()


def test_download_cancellation_and_deadline_propagate(tmp_path):
    class Cancelled:
        async def download_desktop(self, *, deadline):
            raise asyncio.CancelledError()

    destination = tmp_path / "exports/task/worker"
    with pytest.raises(asyncio.CancelledError):
        export(backend(tmp_path, Cancelled()), destination)
    assert not destination.exists()

    async def expired():
        vm = backend(tmp_path, TarTransport(archive_bytes([("Desktop/x", b"x")])))
        await vm.export_files(destination, deadline=asyncio.get_running_loop().time() - 1)

    with pytest.raises(TimeoutError):
        asyncio.run(expired())
    assert not destination.exists()


def test_action_only_transport_double_skips_files(tmp_path):
    destination = tmp_path / "exports/task/worker"
    assert export(backend(tmp_path, object()), destination)["status"] == "skipped"
    assert not destination.exists()


def test_archive_size_bound_and_duplicate_entries(tmp_path):
    member = tarfile.TarInfo("Documents/huge.bin")
    member.size = 65 * 1024 * 1024
    destination = tmp_path / "exports/task/worker"
    with pytest.raises(ValueError, match="exceed 64 MiB"):
        export(backend(tmp_path, TarTransport(member.tobuf())), destination)
    assert not destination.exists()
    data = archive_bytes([("Desktop/repeated", b"one"), ("Desktop/repeated", b"two")])
    with pytest.raises(FileExistsError):
        export(backend(tmp_path, TarTransport(data)), destination)
    assert not destination.exists()
