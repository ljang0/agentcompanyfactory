"""Atomic JSON storage and narrow, content-based dependencies."""

import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def now():
    return datetime.now(UTC).isoformat()


def digest(value):
    content = (
        value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    )
    return hashlib.sha256(content).hexdigest()


def decided_by(paths):
    """A digest of the files whose content decides a verdict, to be recorded in the marker holding it.

    The batch driver asks whether a marker is older than the code that decided it, and it asks with
    mtimes. Measured 2026-09-10: mtimes are not a record of anything. 6,207 of the 6,912 files under
    companies/ were *created* inside one three-minute window by an operation outside the pipeline --
    birth, mtime and ctime equal on every one, inside directories two days older, and the only files
    that kept their real dates are the ones no stage writes. 93 markers therefore read as newer than
    the last change to the module that decides them, with nothing on disk able to say whether the
    verdict was computed by that module or merely re-dated after it. The one mechanism that survived
    was bulk_layer.BULK_VERSION, because it is a value recorded *in* the marker.

    This is that mechanism without the hand-maintained number. It answers the same question from
    content, so a re-dating, a restore or an archive extraction cannot defeat it, and it fires on
    exactly the commits an mtime fires on -- a commit that changes a file changes both -- so it costs
    nothing in re-runs: 28, 5, 36 and 6 commits in the week to 2026-09-10 for the four lists that had
    one. It also drops the false half an mtime has, where a checkout restales a verdict that no code
    change touched. The marker records this value and the module that wrote it exposes ``current``;
    a marker with no value recorded reads stale, which is the migration.

    A path that does not exist digests as its absence rather than raising: the contract is that the
    value changes when the inputs change, and a deleted input is a change.
    """
    return digest(
        {str(p): digest(Path(p).read_bytes()) if Path(p).is_file() else None for p in sorted(map(str, paths))}
    )


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def run_lock(run_dir):
    path = Path(run_dir) / ".lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another coordinator owns this run") from exc
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def bound_review(
    company, workflows, evidence, review_prompt, authors, policy="different_model", receipts=None
):
    payload = {
        "company": company,
        "workflows": workflows,
        "evidence": evidence,
        "review_prompt": review_prompt,
        "authors": sorted(authors),
    }
    if policy != "different_model":
        payload.update(review_policy=policy, author_receipts=receipts)
    return digest(payload)
