"""Build a new Linux desktop image from a checked Ubuntu cloud image.

Uses native QEMU and cloud-localds. Failed builds keep their work directory and
logs; the requested output is created only after guest provisioning is verified.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from company_envs.world.vm import WorkerVM

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build(cloud_image, expected_sha256, output, *, timeout=7200):
    cloud_image, output = Path(cloud_image).resolve(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError(f"Output already exists: {output}")
    if sha256(cloud_image) != expected_sha256:
        raise ValueError("Cloud image SHA-256 does not match; no VM was started")
    for executable in ("qemu-system-x86_64", "qemu-img", "cloud-localds", "ssh", "sshpass"):
        if not shutil.which(executable):
            raise ValueError(f"Required executable is missing: {executable}")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise ValueError("Read/write access to /dev/kvm is required")
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=output.stem + "-build-", dir=output.parent))
    print(f"Build records: {work}", flush=True)
    recipe = work / "user-data"
    shutil.copyfile(ROOT / "configs/desktop-cloud-init.yaml", recipe)
    (work / "meta-data").write_text(f"instance-id: {work.name}\nlocal-hostname: company-desktop\n")
    disk = work / "provision.qcow2"
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(cloud_image), str(disk), "32G"],
        check=True,
    )
    subprocess.run(
        ["cloud-localds", str(work / "seed.iso"), str(recipe), str(work / "meta-data")], check=True
    )
    command = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-cpu",
        "host",
        "-m",
        "8192",
        "-smp",
        "4",
        "-drive",
        f"file={disk},if=virtio,format=qcow2",
        "-cdrom",
        str(work / "seed.iso"),
        "-device",
        "virtio-vga",
        "-display",
        "none",
        "-serial",
        "stdio",
        "-netdev",
        "user,id=build",
        "-device",
        "virtio-net-pci,netdev=build",
    ]
    with (work / "provision.log").open("w") as log:
        subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=timeout,
        )
    # A cloud-init log line or QEMU exit alone cannot establish a usable image.
    guest = WorkerVM(work / "verify", "desktop", disk, app_port=1)
    try:
        guest.boot()
        result = guest.run(
            "test -f /home/ga/.desktop-provisioned && "
            "sudo cloud-init status --wait && "
            "test $(id -u) = 1000 && command -v xdotool && "
            "python3 -c \"import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk\" && "
            "cat /home/ga/desktop-packages.tsv",
            timeout=180,
        )
        (work / "guest-check.txt").write_text(result.stdout)
        guest.run("sudo poweroff", check=False)
        guest.process.wait(timeout=90)
    finally:
        guest.close()
    candidate = work / "desktop.qcow2"
    subprocess.run(["qemu-img", "convert", "-O", "qcow2", str(disk), str(candidate)], check=True)
    report = {
        "status": "provisioned",
        "cloud_sha256": expected_sha256,
        "recipe_sha256": sha256(recipe),
        "image_sha256": sha256(candidate),
        "image_bytes": candidate.stat().st_size,
        "checks": ["SSH", "cloud-init completed", "ga UID 1000", "xdotool", "Python GTK 3"],
        "unmeasured": ["company app access", "guest browser", "team trial"],
        "packages": "guest-check.txt",
    }
    (work / "BUILD.json").write_text(json.dumps(report, indent=2) + "\n")
    # Hard linking fails if another process has created the destination meanwhile.
    os.link(candidate, output)
    print(json.dumps({"image": str(output), "report": str(work / "BUILD.json"), **report}, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud-image", type=Path, required=True)
    parser.add_argument("--sha256", required=True, help="Expected SHA-256 from Ubuntu's published sums")
    parser.add_argument("--output", type=Path, required=True, help="New qcow2 path; never overwritten")
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args()
    build(args.cloud_image, args.sha256, args.output, timeout=args.timeout)
