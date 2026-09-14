import json
import shutil

from company_envs.world.agreement import CONDITIONS, agreement, difficulty, merge_policy_runs

GRADED = {"score": 1.0, "passed": True, "environment_fault": False, "reason": "graded"}
FAULT = {"score": 0.2, "passed": False, "environment_fault": True, "reason": "wiped_collections"}


def make(
    tmp_path,
    *,
    reviewed=True,
    solo=False,
    relative=False,
    calibrated=True,
    vm="teacher_passed",
    policy_runs=None,
    tasks=("acme_o1",),
    legacy=False,
):
    """A company folder with every artifact the agreement reads, one controller runtime per task.

    ``vm`` is the teacher class for every task, or a dict keyed by task id. ``policy_runs`` is a
    policy -> run mapping for every task, or a dict keyed by task id. ``legacy`` puts the first
    task's checkpoint and working copy at the old company-level location, runtime/CONTROLLER.json
    and runtime/controller/.
    """
    folder = tmp_path / "acme"
    (folder / "world").mkdir(parents=True)
    per_task = isinstance(policy_runs, dict) and set(policy_runs) <= set(tasks)
    for index, task in enumerate(tasks):
        runtime = folder / "runtime" if legacy and index == 0 else folder / "runtime" / "tasks" / task
        teacher_dir = runtime / "controller" / "company" / "runtime" / "teacher" / task
        teacher_dir.mkdir(parents=True)
        controller = {
            "task_id": task,
            "status": "done",
            "options": {"backend": "mypcbench", "dry_run": False},
        }
        runs = policy_runs.get(task) if per_task else policy_runs
        if runs is not None:
            controller["policy_runs"] = runs
        (runtime / "CONTROLLER.json").write_text(json.dumps(controller))
        outcome = vm.get(task) if isinstance(vm, dict) else vm
        if outcome:
            # A real teacher report always carries contributions; the roles condition reads them.
            (teacher_dir / "TEACHER.json").write_text(
                json.dumps(
                    {
                        "class": outcome,
                        "score": 1.0,
                        "contributions": {
                            "workers": [
                                {"worker_id": "boss", "changes": [{"check_ids": ["c1"]}]},
                                {"worker_id": "clerk", "changes": [{"check_ids": ["c2"]}]},
                            ]
                        },
                    }
                )
            )
            (teacher_dir / "messages.jsonl").write_text(
                "\n".join(
                    json.dumps(m)
                    for m in (
                        {"event": "send", "sender": "boss", "recipient": "clerk"},
                        {"event": "send", "sender": "clerk", "recipient": "boss"},
                    )
                )
            )
        (folder / "runtime" / "grades" / task).mkdir(parents=True)
        (folder / "runtime" / "grades" / task / "calibration.json").write_text(
            json.dumps({"accepted": calibrated, "scope": "all_checks"})
        )
        (folder / "tasks" / task).mkdir(parents=True)
        (folder / "tasks" / task / "workflow.json").write_text(
            json.dumps(
                {
                    "id": task,
                    "manager_id": "boss",
                    "brief": "Fix the nightly build by Friday."
                    if relative
                    else "Fix the nightly build by September 12, 2026.",
                    "feature_cell": {"collections": ["jira_mock.issues", "google_docs_mock.documents"]},
                    "contributions": [
                        {"worker_id": "boss", "apps": ["jira_mock"] if solo else []},
                        {"worker_id": "dev", "apps": ["jira_mock"]},
                    ],
                }
            )
        )
    (folder / "world" / "SEED.json").write_text(
        json.dumps(
            {
                "status": "seeded_reviewed" if reviewed else "seeded_review_failed",
                "review": "accept",
                "reference_date": "2026-09-08",
            }
        )
    )
    (folder / "world" / "CHECKS.json").write_text(
        json.dumps(
            {
                "ok": True,
                "errors": 0,
                "warnings": 2,
                "apps_checked": ["gmail_mock", "google_docs_mock", "jira_mock"],
            }
        )
    )
    (folder / "world" / "worker_apps.json").write_text(
        json.dumps(
            {
                "boss": ["gmail_mock", "google_docs_mock"],
                "dev": ["gmail_mock", "google_docs_mock", "jira_mock"],
            }
        )
    )
    (folder / "apps.json").write_text(
        json.dumps({"apps": [{"app_id": a} for a in ("gmail_mock", "google_docs_mock", "jira_mock")]})
    )
    (folder / "REPORT-readability.json").write_text(
        json.dumps({"categories": {"brief": {"verdict": "plain"}, "messages": {"verdict": "dense"}}})
    )
    return folder


def test_agreement_holds_when_every_condition_holds(tmp_path):
    report = agreement(make(tmp_path, policy_runs={"own": GRADED}))
    assert report["done"] and set(report["conditions"]) == set(CONDITIONS)
    assert json.loads((tmp_path / "acme" / "AGREEMENT.json").read_text())["done"]
    assert "difficulty" not in CONDITIONS and report["difficulty"]["label"] == "easy"
    assert report["difficulty"]["by_task"] == {"acme_o1": "easy"}


def test_agreement_names_the_failing_condition(tmp_path):
    assert agreement(make(tmp_path, reviewed=False))["conditions"]["reviewed"]["ok"] is False
    solo = agreement(make(tmp_path / "b", solo=True))
    assert solo["done"] is False and solo["conditions"]["barrier"]["evidence"][
        "manager_could_finish_alone"
    ] == ["acme_o1"]
    rel = agreement(make(tmp_path / "c", relative=True))
    assert rel["conditions"]["evergreen"]["ok"] is False and "by friday" in str(
        rel["conditions"]["evergreen"]["evidence"]
    )
    assert agreement(make(tmp_path / "d", calibrated=False))["conditions"]["calibrated"]["ok"] is False


def test_agreement_requires_a_real_vm_episode_per_task(tmp_path):
    assert agreement(make(tmp_path))["conditions"]["vm_verified"]["ok"] is True
    missing = agreement(make(tmp_path / "b", vm=None))
    assert missing["done"] is False and missing["conditions"]["vm_verified"]["ok"] is False
    discarded = agreement(make(tmp_path / "c", vm="environment_error"))
    assert discarded["conditions"]["vm_verified"]["ok"] is False
    assert (
        discarded["conditions"]["vm_verified"]["evidence"]["episodes"]["acme_o1"]["class"]
        == "environment_error"
    )


def test_a_retired_task_is_not_held_against_its_company(tmp_path):
    """The driver stops retiring a whole company for one ungradeable task; so must the agreement.

    Without this the company survives at the driver, runs the whole way to the agreement, and then
    fails because the retired task has no teacher pass and never will.
    """
    two = ("acme_o1", "acme_o2")
    folder = make(tmp_path, tasks=two, vm={"acme_o1": "teacher_passed", "acme_o2": "teacher_failed"})
    assert agreement(folder)["conditions"]["vm_verified"]["ok"] is False

    (folder / "tasks" / "acme_o2" / "GRADER_FAILED.json").write_text(
        json.dumps({"reason": "selects a whole collection"})
    )
    survived = agreement(folder)
    vm = survived["conditions"]["vm_verified"]
    assert vm["ok"] is True, "the surviving task carries the company"
    assert set(vm["evidence"]["episodes"]) == {"acme_o1"}

    # A grader that was written later revives it, and the company is judged on both again.
    (folder / "tasks" / "acme_o2" / "grader.json").write_text(json.dumps({"checks": []}))
    assert agreement(folder)["conditions"]["vm_verified"]["ok"] is False


def test_vm_verified_needs_every_task_to_have_run_and_passed(tmp_path):
    two = ("acme_o1", "acme_o2")
    both = agreement(make(tmp_path, tasks=two, policy_runs={"own": GRADED}))
    vm = both["conditions"]["vm_verified"]
    assert both["done"] and vm["ok"] is True
    assert set(vm["evidence"]["episodes"]) == set(two) and vm["evidence"]["backends"] == dict.fromkeys(
        two, "mypcbench"
    )
    # Task 2's teacher failed: the company is not VM-verified, whatever task 1 did.
    one_failed = agreement(
        make(tmp_path / "b", tasks=two, vm={"acme_o1": "teacher_passed", "acme_o2": "teacher_failed"})
    )
    vm = one_failed["conditions"]["vm_verified"]
    assert vm["ok"] is False and vm["evidence"]["episodes"]["acme_o2"]["class"] == "teacher_failed"
    assert vm["evidence"]["episodes"]["acme_o1"]["class"] == "teacher_passed"
    # Task 2 never ran (no checkpoint, no teacher report): also not verified.
    folder = make(tmp_path / "c", tasks=two)
    shutil.rmtree(folder / "runtime" / "tasks" / "acme_o2")
    never_ran = agreement(folder)["conditions"]["vm_verified"]
    assert never_ran["ok"] is False
    assert never_ran["evidence"]["episodes"]["acme_o2"] == {
        # A task that never ran tells us nothing about who was needed on it.
        "roles": {"judged": False, "idle": [], "detail": {}},
        "class": None,
        "score": None,
        "backend": None,
        "real_backend": False,
        "controller_status": None,
        "policy_runs": {},
        "environment_faults": [],
    }
    # An environment fault in any task's policy runs blocks the company.
    faulted = agreement(
        make(tmp_path / "d", tasks=two, policy_runs={"acme_o1": {"own": GRADED}, "acme_o2": {"own": FAULT}})
    )
    vm = faulted["conditions"]["vm_verified"]
    assert vm["ok"] is False and vm["evidence"]["environment_faults"] == {"acme_o2": ["own"]}
    assert vm["evidence"]["policy_runs"] == {"acme_o1": {"own": GRADED}, "acme_o2": {"own": FAULT}}


def test_a_checkpoint_at_the_old_location_counts_for_the_task_it_names(tmp_path):
    folder = make(tmp_path, tasks=("acme_o1", "acme_o2"), policy_runs={"own": GRADED}, legacy=True)
    assert (folder / "runtime" / "CONTROLLER.json").is_file()
    assert not (folder / "runtime" / "tasks" / "acme_o1").exists()
    report = agreement(folder)
    assert report["done"] and report["conditions"]["vm_verified"]["ok"] is True
    assert report["conditions"]["vm_verified"]["evidence"]["episodes"]["acme_o1"]["class"] == "teacher_passed"
    # A company-level checkpoint naming another task says nothing about this one.
    state = json.loads((folder / "runtime" / "CONTROLLER.json").read_text())
    (folder / "runtime" / "CONTROLLER.json").write_text(json.dumps(state | {"task_id": "acme_o9"}))
    stale = agreement(folder)["conditions"]["vm_verified"]
    assert stale["ok"] is False and stale["evidence"]["episodes"]["acme_o1"]["controller_status"] is None


def test_a_policy_run_with_an_environment_fault_blocks_vm_verified_despite_a_passing_teacher(tmp_path):
    runs = {"own": GRADED, "macu": FAULT}
    report = agreement(make(tmp_path, policy_runs=runs))
    vm = report["conditions"]["vm_verified"]
    assert vm["ok"] is False and report["done"] is False
    assert vm["evidence"]["episodes"]["acme_o1"]["class"] == "teacher_passed"
    assert vm["evidence"]["environment_faults"] == {"acme_o1": ["macu"]}
    assert vm["evidence"]["policy_runs"] == {"acme_o1": runs}
    # The failed policy's grade is still not a difficulty verdict: a fault is not a failure.
    assert report["difficulty"] == {
        "passed_by": ["own"],
        "failed_by": ["macu"],
        "label": "medium",
        "by_task": {"acme_o1": "medium"},
    }
    clean = agreement(
        make(tmp_path / "b", policy_runs={k: v | {"environment_fault": False} for k, v in runs.items()})
    )
    assert clean["conditions"]["vm_verified"]["ok"] is True


def test_difficulty_label_from_which_policies_passed():
    assert difficulty({}) == {"passed_by": [], "failed_by": [], "label": None}
    assert difficulty({"own": GRADED, "macu": GRADED})["label"] == "easy"
    failed = GRADED | {"passed": False, "score": 0.3}
    assert difficulty({"own": failed, "macu": failed})["label"] == "hard"
    assert difficulty({"own": GRADED, "macu": failed}) == {
        "passed_by": ["own"],
        "failed_by": ["macu"],
        "label": "medium",
    }
    skipped = {"score": None, "passed": None, "environment_fault": False, "reason": "teacher_did_not_pass"}
    assert difficulty({"own": skipped}) == {"passed_by": [], "failed_by": [], "label": None}
    assert difficulty({"own": GRADED, "macu": skipped})["label"] == "easy"
    # Across tasks a policy passes only when it passed every task it was graded on.
    merged = merge_policy_runs(
        {"t1": {"own": GRADED, "macu": GRADED}, "t2": {"own": failed, "macu": skipped}}
    )
    assert merged == {"own": {"passed": False}, "macu": {"passed": True}}
    assert difficulty(merged)["label"] == "medium"
    assert merge_policy_runs({"t1": {"own": skipped}, "t2": {}}) == {"own": {"passed": None}}


def test_a_check_that_skipped_an_app_does_not_count(tmp_path):
    folder = make(tmp_path)
    checks = json.loads((folder / "world" / "CHECKS.json").read_text())
    checks["apps_checked"] = ["gmail_mock"]
    (folder / "world" / "CHECKS.json").write_text(json.dumps(checks))
    report = agreement(folder)
    assert report["conditions"]["checks"]["ok"] is False and not report["done"]


def test_every_role_must_do_graded_work_and_take_part_in_the_coordination():
    """A task two people could finish is a single-agent task with spectators on the roster."""
    from company_envs.world.agreement import roles_used

    def report(*workers):
        return {"contributions": {"workers": list(workers)}}

    boss = {"worker_id": "boss", "changes": [{"check_ids": ["c1"]}]}
    clerk = {"worker_id": "clerk", "changes": [{"check_ids": ["c2"]}]}
    bus = [
        {"event": "send", "sender": "boss", "recipient": "clerk"},
        {"event": "send", "sender": "clerk", "recipient": "boss"},
    ]
    assert roles_used(report(boss, clerk), bus)["idle"] == []

    # Changed records nothing is graded on: the role is decoration.
    watcher = {"worker_id": "watcher", "changes": [{"check_ids": []}]}
    assert roles_used(report(boss, clerk, watcher), bus)["idle"] == ["watcher"]

    # Did graded work, but nobody ever wrote to them and they never wrote to anyone.
    loner = {"worker_id": "loner", "changes": [{"check_ids": ["c3"]}]}
    assert roles_used(report(boss, clerk, loner), bus)["idle"] == ["loner"]

    # Without a message log the coordination half cannot be judged, so only the work half applies.
    assert roles_used(report(boss, clerk, loner), None)["idle"] == []
    assert roles_used({}, bus) == {"judged": False, "idle": [], "detail": {}}


def test_a_task_whose_golden_can_never_be_written_is_retired_here_too(tmp_path):
    """The driver retires a task on GOLDEN_FAILED.json and revives it on golden.json.

    The two lists have to stay the same list: a task the driver has retired but the agreement still
    counts is a company that can never finish. rainbow-shops holds an accepted, all-checks
    calibration on one of its two tasks and is held out of delivery by the other task's golden
    author, which returns ``'p31'`` where an object is required.
    """
    from company_envs.world.agreement import RETIRED_MARKERS, REVIVED_BY

    assert "GOLDEN_FAILED.json" in RETIRED_MARKERS
    assert REVIVED_BY["GOLDEN_FAILED.json"] == "golden.json"
    two = ("acme_o1", "acme_o2")
    folder = make(tmp_path, tasks=two, vm={"acme_o1": "teacher_passed", "acme_o2": "teacher_failed"})
    assert agreement(folder)["conditions"]["vm_verified"]["ok"] is False
    (folder / "tasks" / "acme_o2" / "GOLDEN_FAILED.json").write_text(
        json.dumps({"task_id": "acme_o2", "reason": "golden author could not meet the task contract"})
    )
    vm = agreement(folder)["conditions"]["vm_verified"]
    assert vm["ok"] is True and set(vm["evidence"]["episodes"]) == {"acme_o1"}
    # A golden written later revives it, and the company is judged on both again.
    (folder / "tasks" / "acme_o2" / "golden.json").write_text(json.dumps([]))
    assert agreement(folder)["conditions"]["vm_verified"]["ok"] is False
