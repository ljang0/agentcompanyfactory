#!/usr/bin/env python3
"""Open one desktop application on a real company's material in a live guest, and look.

The desktop half of ``scripts/interact_apps.py``. That script answers "does a seeded web app
render this company's records and does a click reach the grader's state"; this one answers the
same two questions for a file: does the application open on the company's data, is anything
covering it, and does the file read back into the shape a grader asserts on.

Materials come from ``companies/*/world/materials`` -- the real thing, at the width and length
the seeding model actually wrote, because the defect this is built to catch is a presentation
defect. A fixture would have had a five-character account name and shown nothing.

The one check that has no desktop equivalent is ``ui_action_reaches_state``: nothing here types.
What replaces it is reading the artifact back through ``desktop_app.read_material`` -- the same
call a grader makes -- so the report says whether the values on screen are the values a check
could name.
"""

import argparse
import base64
import json
import re
import shlex
import subprocess
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from company_envs.storage import write
from company_envs.world.desktop_app import (
    DESKTOP,
    DESKTOP_CHROME,
    GUEST_HOME,
    check_desktop_catalog,
    check_materials,
    close_command,
    desktop_apps,
    document_window,
    grading_of,
    guest_target,
    kill_command,
    material_values,
    open_material,
    read_material,
    type_commands,
    windows_command,
)
from company_envs.world.documents import guest_path, render_material
from company_envs.world.vm import WorkerVM

# A screendump of a guest whose session has not painted is one flat colour. Measured against the
# working Calc frame from the first probe, which has 12,657 distinct colours.
MIN_SCREEN_COLOURS = 24


# Formats no seeded world ships, and how to make one that is still the company's own content.
# Labelled `generated` in every report: a proof on a synthetic file says the application works,
# not that a company's material does, and the two must not read as the same evidence.
def _company_svg(companies, *, prefer_stub=False):
    """A real designer's asset; by default one that is not a remote stub.

    5 of the 6 .svg materials on disk are stubs whose only <image> points at a URL, so a guest
    with restrict=on rasterises them to an empty canvas. An empty canvas is not a proof, so the
    asset's own title and credit are drawn instead, and the report says the file was generated.
    """
    found = sorted(companies.glob("*/world/materials/*/**/*.svg"))
    stubs = [p for p in found if 'href="http' in p.read_text(errors="replace")]
    local = [p for p in found if p not in stubs]
    wanted = (stubs or local) if prefer_stub else (local or stubs)
    return (wanted or [None])[0]


def _make_png(target, companies):
    """A raster through the production renderer, from a real company asset's own words.

    ``documents.render_material`` is the thing under test: the day a world names a material
    ``Pictures/harbor.png``, this is exactly the file the launcher will write.
    """
    svg = _company_svg(companies)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = svg.read_text(errors="replace") if svg else "# Company asset"
    target.write_bytes(render_material(target.name, text.encode()))


def _make_mp4(target, companies):
    png = target.parent / "_frame.png"
    _make_png(png, companies)
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-i",
            str(png),
            "-t",
            "8",
            "-r",
            "24",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            "scale=1280:720",
            "-c:v",
            "libx264",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    png.unlink()


def _make_osp(target, companies):
    """A minimal OpenShot project naming one clip, which is what a content check would read.

    The clip carries a real company asset's own title, so "does the company's data survive into
    the artifact" means something here rather than being asserted against a skeleton.
    """
    svg = _company_svg(companies)
    markup = svg.read_text(errors="replace") if svg else ""
    title = (re.search(r"<title>(.*?)</title>", markup, re.DOTALL) or [None, "Company asset"])[1].strip()
    # The clip has to point at a file that exists in the guest, or OpenShot opens a "Missing File"
    # dialog over the project and titles the window "Untitled Project" -- which is exactly what
    # the covering-window check caught on the first run of this asset.
    footage = target.parent / "company footage.mp4"
    _make_mp4(footage, companies)
    guest_footage = guest_target(f"Documents/{footage.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "id": "COMPANYPROOF",
                "clips": [
                    {
                        "id": "CLIP0001",
                        "title": title,
                        "position": 0.0,
                        "start": 0.0,
                        "end": 8.0,
                        "layer": 0,
                        "reader": {"path": guest_footage},
                    }
                ],
                "files": [{"id": "FILE0001", "media_type": "video", "path": guest_footage, "title": title}],
                "fps": {"num": 24, "den": 1},
                "width": 1280,
                "height": 720,
                "duration": 300,
                "scale": 15,
                "channels": 2,
                "channel_layout": 3,
                "sample_rate": 44100,
                "settings": {},
                "effects": [],
                "export_path": "",
                "version": {"openshot-qt": "2.5.1", "libopenshot": "0.2.5"},
            },
            indent=2,
        )
    )


GENERATED = {".png": _make_png, ".mp4": _make_mp4, ".osp": _make_osp}
# Formats only the application itself can author. Run in the guest before the launch.
GUEST_PREPARE = {
    ".blend": (
        "blender --background --factory-startup --python-expr "
        "'import bpy; bpy.ops.wm.save_as_mainfile(filepath=\"{path}\")' >/dev/null 2>&1"
    ),
}


def widest_material(app, companies, suffix=None):
    """The most demanding real material this application opens: the largest one any world has.

    Largest by source bytes, which for these formats is the number and width of the values -- the
    same reasoning as ``interact_apps.world_state`` picking the biggest seeded state. ``suffix``
    narrows it to one format, because an application's formats do not all open the same way: Calc
    takes a workbook straight to the grid and answers a bare CSV with the Text Import dialog, and
    the largest material was always the workbook, so the CSV path went untested.
    """
    wanted = (suffix.lower(),) if suffix else tuple(app.get("opens", ()))
    found = [
        path
        for path in companies.glob("*/world/materials/*/**/*")
        if path.is_file() and path.suffix.lower() in wanted
    ]
    return max(found, key=lambda path: path.stat().st_size, default=None)


def stage(material, work, name=None):
    """Render one material into a guest tree and tar it, exactly as ``hub_vm`` does at launch.

    Returns the material's guest-relative name and the company it came from. The company is read
    from the worker directory rather than by counting parents: a material nested two folders deep
    put "world" and then "materials" in the report where the company's name belonged. A generated
    asset has no worker directory and reports its company as ``generated``.
    """
    worker_root = next((parent for parent in material.parents if parent.parent.name == "materials"), None)
    guest = work / "guest"
    if worker_root is None:
        # A generated asset, and everything beside it: an OpenShot project without the footage it
        # names opens a Missing File dialog over the work, so the whole directory is staged.
        company, name = "generated", name or f"Documents/{material.name}"
        for sibling in sorted(material.parent.iterdir()):
            if sibling.is_file():
                beside = guest / guest_path(f"Documents/{sibling.name}")
                beside.parent.mkdir(parents=True, exist_ok=True)
                beside.write_bytes(sibling.read_bytes())
    else:
        company, name = worker_root.parents[2].name, str(material.relative_to(worker_root))
    target = guest / guest_path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(render_material(name, material.read_bytes()))
    archive = work / "guest.tar"
    with tarfile.open(archive, "w") as tar:
        for path in sorted(guest.rglob("*")):
            tar.add(path, arcname=str(path.relative_to(guest)))
    return name, company, target, archive


def screen_colours(path):
    from PIL import Image

    with Image.open(path) as image:
        return len(image.convert("RGB").getcolors(maxcolors=1 << 20) or [])


def pick_material(app, app_id, companies, work, suffix=None):
    """The file this application is proved on, and where it came from.

    A real company material wherever one exists, because the defect this catches is a presentation
    defect and a fixture would have none of a company's width. Six of the twelve applications open
    formats no seeded world ships at all -- there is not one .png, .mp4, .blend or .osp among the
    2,026 materials -- so those are generated from a real company asset and labelled as generated.
    """
    real = widest_material(app, companies, suffix)
    if real is not None:
        return real, "world"
    if not app["opens"]:
        # An application that opens a folder: the worker's own Documents, seeded or not.
        return None, "folder"
    for candidate in [suffix] if suffix else app["opens"]:
        if candidate in GENERATED:
            target = work / "generated" / app_id / f"company proof{candidate}"
            GENERATED[candidate](target, companies)
            return target, "generated"
        if candidate in GUEST_PREPARE:
            return None, "guest"
    return None, "none"


def check_app(vm, app_id, app, material, work, out, *, origin="world"):
    """Seed, launch, look, read back. Never raises for the application's sake."""
    started = time.monotonic()
    report = {
        "app_id": app_id,
        "name": app["name"],
        "checks": {},
        "material_origin": origin,
        # Shaped like a record number on purpose. Typed as "desktop probe 1789075589" into
        # Writer, LibreOffice AutoCorrect capitalised the sentence and the document held
        # "Desktop probe ..." -- so the check failed over a change the application made, not one
        # the worker missed. A grader's text operators are case-sensitive by contract, which is
        # why this matters beyond the probe; see experiments/DESKTOP-ADAPTER.md.
        "marker": f"PROBE-{int(time.time())}",
    }
    staged = None
    if material is not None:
        name, company, staged, archive = stage(material, work / app_id)
        report["material"] = {
            "name": name,
            "company": company,
            "source_bytes": material.stat().st_size,
            "rendered_bytes": staged.stat().st_size,
            "guest_path": guest_target(name),
        }
    elif origin == "folder":
        # An application that opens a folder rather than a file: the worker's own Documents.
        name, archive = "Documents", None
        report["material"] = {"name": name, "company": "", "guest_path": guest_target(name)}
    else:
        # A format only the application itself can author, built in the guest before the launch.
        suffix = next(s for s in app["opens"] if s in GUEST_PREPARE)
        name, archive = f"Documents/company proof{suffix}", None
        report["material"] = {"name": name, "company": "generated", "guest_path": guest_target(name)}
    binary = shlex.split(app["launch"])[0]
    which = vm.run(f"command -v {shlex.quote(binary)}", check=False)
    report["binary"] = which.stdout.strip()
    report["checks"]["app_installed"] = bool(which.stdout.strip())
    report["checks"]["packages_present"] = all(
        vm.run(f"dpkg-query -W -f='${{Status}}' {shlex.quote(package)}", check=False).stdout.startswith(
            "install ok installed"
        )
        for package in app["packages"]
    )
    if archive is not None:
        vm.upload(archive, f"{GUEST_HOME}/desktop-check.tar")
        vm.run(f"tar -xf {GUEST_HOME}/desktop-check.tar -C {GUEST_HOME} && rm {GUEST_HOME}/desktop-check.tar")
    elif origin == "guest":
        vm.run(f"mkdir -p {shlex.quote(str(Path(guest_target(name)).parent))}")
        prepared = vm.run(
            DESKTOP + GUEST_PREPARE[Path(name).suffix.lower()].replace("{path}", guest_target(name)),
            check=False,
            timeout=180,
        )
        report["guest_prepare"] = {"rc": prepared.returncode, "stderr": prepared.stderr.strip()[:200]}
    report["checks"]["file_present"] = origin == "folder" or (
        vm.run(f"test -f {shlex.quote(guest_target(name))}", check=False).returncode == 0
    )
    # Observations about this world, never failures: the rendered artifact is what a grader will
    # read, and a material it cannot parse is a fact about the episode, not a broken catalog.
    if staged is not None:
        report["material_findings"] = check_materials([staged])
    if not report["checks"]["app_installed"]:
        report["seconds"] = round(time.monotonic() - started, 1)
        return finish(report)

    transcript = []

    def run(command):
        # check=False on purpose: a launch that cannot find the session is a result about this
        # image, not a harness crash, and the window list below is where it shows up.
        done = vm.run(command, timeout=app["ready_seconds"] + 60, check=False)
        transcript.append({"rc": done.returncode, "stderr": done.stderr.strip()[:200]})
        return done.stdout

    # The application by name, not by suffix: a check must test the application it says it is
    # testing, and .png routes to the Image Viewer even when the subject is GIMP.
    opened = open_material(run, name, app_id=app_id)
    report["guest"] = transcript
    report.update({key: opened[key] for key in ("path", "title", "launch_seconds", "xdotool")})
    report["command"] = opened["command"].strip().splitlines()[-1]
    report["windows"] = [window.get("name", "") for window in opened["windows"]]
    report["covering"] = [window.get("name", "") for window in opened["covering"]]
    report["window_errors"] = opened["window_errors"]
    report["matched_on"] = opened["matched_on"]
    report["window_geometry"] = (
        {key: opened["document_window"].get(key) for key in ("x", "y", "width", "height")}
        if opened["document_window"]
        else None
    )
    report["checks"]["xdotool_present"] = opened["xdotool"]
    report["checks"]["window_open_on_the_document"] = opened["document_window"] is not None
    report["checks"]["nothing_covers_the_document"] = not opened["covering"]
    report["checks"]["no_window_announces_a_failure"] = not opened["window_errors"]

    settle = app["settle_seconds"]
    vm.run(f"sleep {settle:g}", timeout=settle + 30)
    shot = out / "shots" / f"{app_id}.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    vm.screenshot(shot)
    report["screenshot"] = str(shot)
    report["screen_colours"] = screen_colours(shot)
    report["checks"]["screen_is_painted"] = report["screen_colours"] >= MIN_SCREEN_COLOURS

    # The grader's own call, on the file the guest is showing. An application whose reader is
    # "none" is openable and not gradeable; that is a fact about it, not a failure of this run,
    # so the two checks are absent rather than false -- an empty run must not write a verdict.
    reopened = typed_write(vm, report, app, app_id, name, opened, work, run)
    report["grading"] = grading_of(app, name) if staged is not None else "none"
    if report["grading"] == "none":
        report["not_gradeable_reason"] = (
            f"{app['name']} leaves nothing a check can read" if staged is not None else "no staged file"
        )
    else:
        read_back(report, app, staged, material)
    # The window the close is sent to is the one now on screen. The kill above destroys the
    # original, so closing its id afterwards asks the server about a window that no longer
    # exists -- which reported LibreOffice, GIMP and Blender as refusing to close when all three
    # had already been killed and reopened.
    # One kill-and-reopen per run. A typed application has already had its, and it was the more
    # telling one: it proved the edit came back, not only the window.
    if reopened is None:
        reopened = survives_a_kill(vm, report, app, app_id, name, opened["titles"], run)
    current = reopened or opened
    if current["document_window"]:
        vm.run(close_command(current["document_window"]["id"]), check=False, timeout=60)
    time.sleep(2)
    left = json.loads(vm.run(windows_command()).stdout or "{}").get("windows", [])
    report["windows_after_close"] = [window.get("name", "") for window in left]
    # Checked, not merely recorded: the first run closed by name and the Image Viewer stayed open
    # behind the next four applications, which is how one application's screen became another's.
    report["checks"]["closes_on_request"] = document_window(left, opened["titles"]) is None
    report["forced_close"] = not report["checks"]["closes_on_request"]
    # Always, not only after a failed close. Each application is measured on a clean screen or
    # the measurement is about the one before it: a window left behind becomes the next app's
    # "something is covering the document", which is how GIMP's refusal to close was reported
    # against four other applications, and OpenShot's Missing File dialog against two more.
    vm.run(kill_command(app), check=False, timeout=60)
    time.sleep(3)
    remaining = json.loads(vm.run(windows_command()).stdout or "{}").get("windows", [])
    report["windows_left_behind"] = [
        window.get("name", "") for window in remaining if not DESKTOP_CHROME.match(window.get("name", ""))
    ]
    report["checks"]["leaves_a_clean_screen"] = not report["windows_left_behind"]
    report["seconds"] = round(time.monotonic() - started, 1)
    return finish(report)


def typed_write(vm, report, app, app_id, name, opened, work, run):
    """Type what a worker would type, save it, reopen, and read the file back off the guest.

    The one check the web half has that this did not: a typed action reaching state a grader
    reads. It is not enough that the marker is on screen -- the screen is not what a grader sees.
    The file is pulled back out of the guest and read through ``read_material``, which is the
    grader's own call, and then the application is reopened so a save that only lived in memory
    fails here rather than in an episode.

    An application without a ``typing`` entry does not get a made-up one. VLC has no typed work a
    worker would do, and a probe that typed into it and reported success would be measuring the
    absence of a crash.
    """
    commands = type_commands(app, report["marker"])
    if not commands or opened["document_window"] is None:
        report["typed"] = {"skipped": "no typed work in this application"}
        return None
    vm.run(DESKTOP + f"xdotool windowactivate --sync {opened['document_window']['id']}", check=False)
    time.sleep(1)
    for command in commands:
        run(command)
    time.sleep(3)
    guest = guest_target(name)
    pulled = work / "typed" / Path(name).name
    pulled.parent.mkdir(parents=True, exist_ok=True)
    fetched = vm.run(f"base64 -w0 {shlex.quote(guest)}", check=False, timeout=120)
    if fetched.returncode == 0 and fetched.stdout.strip():
        pulled.write_bytes(base64.b64decode(fetched.stdout))
    report["typed"] = {"marker": report["marker"], "bytes": pulled.stat().st_size if pulled.exists() else 0}
    try:
        read = read_material(pulled)
        report["typed"]["reads_back"] = read["kind"]
        report["checks"]["a_typed_edit_reaches_the_grader"] = report["marker"] in read["text"]
    except Exception as exc:  # noqa: BLE001 -- an unreadable saved file is the result
        report["typed"]["read_error"] = f"{type(exc).__name__}: {exc}"[:200]
        report["checks"]["a_typed_edit_reaches_the_grader"] = False
    # And it has to survive the application being killed and opened again, which is what an
    # episode does to every window it ever showed. This is also *the* kill-and-reopen cycle for a
    # typed application: running a second one afterwards asked soffice to die and restart twice
    # within a few seconds, and the second relaunch answered in 0.0s with no window -- a failure
    # invented by the harness's ordering, not by LibreOffice.
    vm.run(kill_command(app), check=False, timeout=60)
    time.sleep(6)
    gone = json.loads(vm.run(windows_command()).stdout or "{}").get("windows", [])
    report["checks"]["kill_actually_ends_it"] = document_window(gone, opened["titles"]) is None
    again = open_material(run, name, app_id=app_id)
    report["typed"]["reopened"] = again["document_window"] is not None
    report["checks"]["reopens_after_an_unclean_kill"] = again["document_window"] is not None
    report["checks"]["no_recovery_modal_after_a_kill"] = not (again["covering"] or again["window_errors"])
    report["checks"]["the_edit_survives_a_reopen"] = bool(
        again["document_window"] and not again["covering"] and not again["window_errors"]
    )
    return again


def survives_a_kill(vm, report, app, app_id, name, titles, run):
    """Kill it the way an episode does, launch it again, and see what comes up.

    The controller stops episode VMs rather than closing their applications, and a worker can end
    one any way they like. LibreOffice answers an unclean death with a Document Recovery modal
    over the work on the next launch; this is the check that says whether the image's suppression
    and the launch line's --norestore actually hold, rather than being asserted in a comment.
    """
    killed = vm.run(kill_command(app), check=False, timeout=60)
    report["killed"] = killed.stdout.strip()
    time.sleep(4)
    gone = json.loads(vm.run(windows_command()).stdout or "{}").get("windows", [])
    report["windows_after_kill"] = [window.get("name", "") for window in gone]
    # The kill has to have worked, or every check after it is measuring the application that was
    # already on screen. Reported rather than assumed: the first run's pkill matched its own
    # command line, killed its shell, and left the relaunch check passing against a window that
    # had never closed.
    report["checks"]["kill_actually_ends_it"] = document_window(gone, titles) is None
    if not report["checks"]["kill_actually_ends_it"]:
        return
    again = open_material(run, name, app_id=app_id)
    report["relaunch"] = {
        "seconds": again["launch_seconds"],
        "windows": [window.get("name", "") for window in again["windows"]],
        "covering": [window.get("name", "") for window in again["covering"]],
        "window_errors": again["window_errors"],
    }
    report["checks"]["reopens_after_an_unclean_kill"] = again["document_window"] is not None
    report["checks"]["no_recovery_modal_after_a_kill"] = not (again["covering"] or again["window_errors"])
    return again


def read_back(report, app, staged, material):
    """The grader's own call, on the file the guest is showing.

    What counts as read back depends on what the reader can prove. A text-bearing artifact has to
    come back with text; a picture has no text and never will, so its reader answers with size and
    format instead. Judging the picture by the text rule reported GIMP as unable to read back a
    file it had read perfectly -- the same "empty run writes a real failure's receipt" shape as
    the SVG value check earlier in this sweep, one reader further along.
    """
    try:
        artifact = read_material(staged)
        report["reads_back"] = {"kind": artifact["kind"], "characters": len(artifact["text"])}
        if report["grading"] == "properties":
            report["reads_back"] |= {
                key: artifact.get(key) for key in ("format", "width", "height") if key in artifact
            }
            report["checks"]["reads_back_for_grading"] = bool(artifact.get("width"))
            report["company_values"] = {"not_applicable": "a picture carries no values to check"}
            return
        report["checks"]["reads_back_for_grading"] = bool(artifact["text"].strip())
        wanted = material_values(material.read_text(errors="replace"))
        shown = [value for value in wanted if value in artifact["text"]]
        report["company_values"] = {"looked_for": len(wanted), "found": len(shown), "sample": shown[:5]}
        # A material with no readable values has nothing for this check to be right or wrong
        # about, and answering False would be the sweep's first defect class: an empty run
        # writing the receipt a real failure writes.
        if wanted:
            report["checks"]["company_values_survive"] = len(shown) >= max(1, len(wanted) // 2)
        else:
            report["company_values"]["not_applicable"] = "no readable values in the material's text"
    except Exception as exc:  # noqa: BLE001 -- an unreadable artifact is the result
        report["read_error"] = f"{type(exc).__name__}: {exc}"[:300]
        report["checks"]["reads_back_for_grading"] = False


def finish(report):
    report["failed_checks"] = sorted(name for name, ok in report["checks"].items() if not ok)
    report["passed"] = not report["failed_checks"]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apps", nargs="*", help="desktop app ids; default every app that opens a suffix")
    parser.add_argument("--image", type=Path, required=True, help="base qcow2 to overlay")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--work", type=Path, default=Path("/mnt/storage/desktop-check"))
    parser.add_argument("--companies", type=Path, default=ROOT / "companies")
    parser.add_argument("--suffix", help="test one format rather than the app's largest material")
    parser.add_argument(
        "--material", type=Path, help="an exact material to open, for apps whose format it is"
    )
    parser.add_argument("--keep", action="store_true", help="leave the guest running")
    args = parser.parse_args(argv)

    catalog = desktop_apps()
    # An authoring error stops the run: a catalog that cannot launch or grade has nothing to say
    # about the image, and running anyway would produce failures that blame the guest for it.
    if errors := check_desktop_catalog(catalog):
        raise SystemExit("\n".join(f"{f['severity']} {f['path']}: {f['message']}" for f in errors))
    chosen = args.apps or sorted(catalog)
    if unknown := sorted(set(chosen) - set(catalog)):
        raise SystemExit(f"not in catalogs/desktop_apps.json: {unknown}")
    args.out.mkdir(parents=True, exist_ok=True)
    work = args.work / datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    (work / "companies").mkdir(parents=True)

    materials = {}
    for app_id in chosen:
        chosen_material = args.material
        if chosen_material and chosen_material.suffix.lower() in catalog[app_id]["opens"]:
            material, origin = chosen_material, "world"
        else:
            material, origin = pick_material(catalog[app_id], app_id, args.companies, work, args.suffix)
        if origin == "none":
            print(f"SKIP {app_id}: no company material and nothing to generate for it", flush=True)
            continue
        materials[app_id] = (material, origin)

    vm = WorkerVM(work, "guest", args.image, app_port=1, memory_mb=6144)
    try:
        vm.boot()
        print(f"guest up on ssh port {vm.ssh_port}", flush=True)
        # SSH answers before the graphical session exists, and a launch into no session fails
        # with nothing on screen to say so -- the same silence as launching against :0.
        deadline = time.monotonic() + 300
        while not (sockets := vm.run("ls /tmp/.X11-unix", check=False).stdout.split()):
            if time.monotonic() > deadline:
                raise SystemExit("no X session in the guest after five minutes")
            time.sleep(5)
        print(f"X sockets: {sockets}", flush=True)
        for app_id, (material, origin) in materials.items():
            try:
                report = check_app(vm, app_id, catalog[app_id], material, work, args.out, origin=origin)
            except Exception as exc:  # noqa: BLE001 -- a harness failure is a result about the app
                report = finish(
                    {
                        "app_id": app_id,
                        "checks": {"harness": False},
                        "harness_error": f"{type(exc).__name__}: {exc}"[:600],
                    }
                )
            write(args.out / f"{app_id}.json", report)
            print(
                f"{'PASS' if report['passed'] else 'FAIL'} {app_id:<20}"
                f" {report.get('material_origin', ''):<10}"
                f" {report.get('grading', 'none'):<11}"
                f" {report.get('material', {}).get('name', '')[:38]:<40}"
                f" {report.get('seconds', 0):>6}s  {','.join(report['failed_checks'])[:70]}",
                flush=True,
            )
    finally:
        if not args.keep:
            vm.close()
            subprocess.run(["rm", "-rf", str(work)], check=False)


if __name__ == "__main__":
    main()
