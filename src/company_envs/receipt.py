"""One shape for every stage's receipt: what happened, and whether it says anything about the world.

The largest defect class this pipeline has is a single sentence: **an empty, skipped or faulted run
writes the receipt a real verdict writes.** Eight instances were filed before the 2026-09-10 sweep
and seven more during it, in seven different files, by agents who did not know about each other:

- ``controller.py``: a company whose teacher never passed marked ``launch`` and the policy steps
  ``status="done"``, and ``--from episode:own`` was accepted past a launch that never booted a VM.
- ``barrier.py``: a desktop collision is reported when it is found, so "the desktops differ" and
  "there are no desktops" were the same answer -- **30 of the 39 unseeded companies reported
  ``barrier ok: true``**, and all 10 mailbox "failures" in the cohort were "there is no world yet".
- ``interact_check.py``: ``canvas_mock`` filed a ``mode: volume`` report having never grown past
  1,764 bytes, because its only collection is nested and the amplifier could not reach it.
- ``state_seed.py``: ``extend_collection`` skipped a shard with only a line on stderr, so a
  collection that lost its history read exactly like one that never had any.
- ``bulk_layer.py``: a provider fault during a re-lay was indistinguishable from the specs being
  refused -- and because the strip came first, a fault destroyed a layer that was fine.
- ``task_author.py``: ``MINE.json`` recorded ``requested: 2, accepted: []`` with no reason,
  identical to a designer that simply accepted nothing.
- ``app_evidence.py``: a volume run that never reached its target printed as a volume **pass**.

Every fix was local, and the class kept reappearing because nothing made the wrong thing hard to
write. This module is the shape that does.

The argument for its existence is not that fifteen authors were careless. It is that **four of them
independently wrote this module's ``rollup``**: ``barrier.py`` as ``bool(tasks) and all(...)`` over
its findings, ``bulk_layer.py`` as a verdict/fault split per app, ``agreement.py`` as
``bool(tasks) and all(...)`` per condition, and ``readability.py`` as its own
``_RANK = {"unmeasured": -1, "plain": 0, "dense": 1, "system-log": 2}`` with a worst-item roll-up.
Four times, four files, none of them wrong. A shape that four people reach for and none of them can
reuse is a missing module, not fifteen lapses -- and the three that stopped short of the fourth's
``unmeasured`` rank are exactly where the defects were found.

The four outcomes are the ones the pipeline actually has, and the distinctions are operational --
each answers a different question a driver asks:

``passed``      the work was measured and it holds.
``refused``     a verdict about *this world*. Re-running reaches the same answer; repair or discard.
``faulted``     the environment failed. It says nothing about the world and must be retried.
``unmeasured``  the inputs were not there. No evidence either way, which is **not** a failure.

The verdict/fault split is not a judgement call, and three agents reinvented it before one noticed
it was already in the type system: ``models.py`` makes ``PromptTooLarge`` and ``ModelOutputInvalid``
``ValueError`` subclasses -- verdicts about the work -- and ``ModelUnavailable`` and
``CallBudgetExhausted`` ``RuntimeError`` subclasses -- faults about the environment. So
``except ValueError`` is the verdict catch anywhere in this pipeline and everything else, a plain
bug included, is a fault because it says nothing about the world either. ``Receipt.from_exception``
and ``attempt`` apply that rule for you, which is the point: a stage that routes its exceptions
through them cannot label an outage as a verdict, however little its author knew about the split.

**An outcome is not automatically a gate input, and in this pipeline it is usually not one.** The
batch driver's accept slot is a *re-run trigger*, not a gate: a marker it refuses makes the step run
again. So a verdict belongs there only where re-running is a plausible remedy, and that is a separate
question from whether the verdict is true. Measured the hard way -- putting ``passed`` in the accept
slot of ``world/SEED.json`` was scoped as a one-line win and would have been catastrophic: 50 of the
60 SEED.json on disk read ``status: seeded_review_failed``, which ``outcome_of`` correctly maps to
``refused``, so the slot would have re-run **the seed step on all 50, about 1,650 core-hours, over
worlds that already exist**. ``review_accepted`` is the right mechanism for that verdict and stops
each of those companies for nothing. The same shape refuses ``done`` on AGREEMENT.json (a company
failing ``vm_verified`` would recompute an identical 64-second verdict every loop for ever, because
its inputs never change) and ``blocking`` on REPORT-readability.json (re-reading prose no stage
rewrites). What belongs in an accept slot is the question ``current`` answers -- has the code that
decided this changed -- because re-running is exactly the remedy for that.

So: use these outcomes to say what a run learned, and to decide what a *caller* does next. Before
putting one in a driver gate, ask what refusing it re-runs and what that costs.

Two properties matter more than elegance, and both are about the reader:

- **A reader must tell the four apart without knowing the stage.** ``outcome_of`` answers for any
  receipt this pipeline has ever written, including the eight older vocabularies (``skipped``,
  ``oversized``, ``unmeasured``, ``faults``, ``note``, ``status: done`` with ``skipped: true``).
- **``ok`` keeps its meaning.** ``passed`` is ``ok: True``, ``refused`` and ``faulted`` are
  ``ok: False``, and ``unmeasured`` is ``ok: None`` -- the shape ``barrier.py`` arrived at, chosen
  because ``all()`` over findings still refuses a report with an unmeasured one in it. Existing
  gates that read ``ok`` keep working unchanged; the new field only tells them which of the three
  non-passes they have.
"""

from dataclasses import dataclass, field
from types import MappingProxyType

PASSED = "passed"
REFUSED = "refused"
FAULTED = "faulted"
UNMEASURED = "unmeasured"
OUTCOMES = (PASSED, REFUSED, FAULTED, UNMEASURED)

# The outcomes that say something about this world. A fault and an unmeasured run do not, and the
# difference is what a driver needs: a refusal earns a repair round, a fault earns a retry, an
# unmeasured run earns whatever produces its missing inputs.
MEASURED = (PASSED, REFUSED)

# ``ok`` for each outcome, kept exactly as the gates on disk already read it. Read-only, because
# the whole point is that no caller gets to decide that its own refusal is an ok.
OUTCOME_OK = MappingProxyType({PASSED: True, REFUSED: False, FAULTED: False, UNMEASURED: None})

# How a roll-up picks one word for many findings. A fault dominates because it invalidates the
# whole reading; a refusal outranks an unmeasured finding because a real negative verdict must not
# be softened into "no evidence" -- the roll-up carries the unmeasured names alongside it instead.
_RANK = {PASSED: 0, UNMEASURED: 1, REFUSED: 2, FAULTED: 3}

# Reasons are for a human reading a marker, not a transcript: one provider error once arrived as
# 3,859,614 characters and the receipt holding it is read by every later stage.
REASON_LIMIT = 400

_RESERVED = ("outcome", "ok", "reason", "missing")


@dataclass(frozen=True)
class Receipt:
    """A stage's answer, with the outcome in a field that has no default.

    That omission is the whole design. Every instance of the class this module exists for was a
    receipt whose author never had to say which of the four it was, so the reader assumed the
    generous one. ``Receipt(...)`` cannot be constructed without answering, and the named
    constructors make each answer cost the evidence it needs: a refusal and a fault need a reason,
    an unmeasured run needs the inputs it did not have, and a pass may carry neither.
    """

    outcome: str
    reason: str = ""
    missing: tuple = ()
    detail: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}, not {self.outcome!r}")
        if self.outcome in (REFUSED, FAULTED) and not self.reason:
            raise ValueError(f"a {self.outcome} receipt must say why: nothing downstream can guess it")
        if self.outcome == UNMEASURED and not self.missing:
            raise ValueError(
                "an unmeasured receipt must name the inputs it did not have; "
                "without them a reader cannot tell it from a failure"
            )
        if self.outcome == PASSED and self.missing:
            raise ValueError(
                f"a passed receipt cannot be missing {list(self.missing)}: a run that could not read "
                "its inputs has not passed, it has measured nothing"
            )
        if shadowed := sorted(set(self.detail) & set(_RESERVED)):
            # Otherwise a detail key could smuggle ok: True past a refusal, which is the defect
            # this class exists to make unwritable.
            raise ValueError(f"detail may not set {shadowed}; they are the receipt's own fields")
        object.__setattr__(self, "missing", tuple(str(name) for name in self.missing))
        object.__setattr__(self, "reason", str(self.reason)[:REASON_LIMIT])
        object.__setattr__(self, "detail", dict(self.detail))

    # -- the four answers -------------------------------------------------------------------

    @classmethod
    def passed(cls, **detail):
        """Measured, and it holds."""
        return cls(PASSED, detail=detail)

    @classmethod
    def refused(cls, reason, **detail):
        """A verdict about this world: re-running reaches the same answer."""
        return cls(REFUSED, reason=reason, detail=detail)

    @classmethod
    def faulted(cls, reason, **detail):
        """The environment failed. Nothing here is a statement about the world; retry it."""
        return cls(FAULTED, reason=reason, detail=detail)

    @classmethod
    def unmeasured(cls, missing, what, *, reason=None, **detail):
        """The inputs were not there, named. No evidence either way.

        ``missing`` is required and may not be empty: the barrier's mailbox finding used to come
        back as ``{"ok": False, "reason": "[Errno 2] ... identities.json"}``, which is the receipt a
        real mailbox defect writes, and a reader had to parse an errno out of a string to tell.
        ``reason`` overrides the default sentence for a stage that can say which step owes the
        inputs -- "the seed has not written ..." is more use to an operator than "are not there".
        """
        missing = tuple(str(name) for name in missing)
        if not missing:
            raise ValueError(f"{what}: an unmeasured receipt must name what it was waiting for")
        names, verb = ", ".join(missing), "is" if len(missing) == 1 else "are"
        return cls(
            UNMEASURED,
            reason=reason or f"{what} was not measured: {names} {verb} not there",
            missing=missing,
            detail=detail,
        )

    @classmethod
    def from_exception(cls, exc, what, **detail):
        """The outcome this exception's own type names, by the rule ``models.py`` already encodes.

        A ``ValueError`` is a verdict about the work (``PromptTooLarge``, ``ModelOutputInvalid``,
        every hand-raised refusal in this codebase); anything else -- ``ModelUnavailable``,
        ``CallBudgetExhausted``, ``OSError``, a plain bug -- is a fault, because it says nothing
        about the world either. Routing exceptions through here is what stops the next stage from
        filing a provider outage as a refusal: it never has to know the rule to get it right.
        """
        outcome = REFUSED if isinstance(exc, ValueError) else FAULTED
        return cls(
            outcome,
            reason=f"{what}: {type(exc).__name__}: {exc}",
            detail={"error_type": type(exc).__name__, **detail},
        )

    # -- derived ---------------------------------------------------------------------------

    @property
    def ok(self):
        """True, False or None, as every gate on disk already reads it."""
        return OUTCOME_OK[self.outcome]

    @property
    def measured(self):
        """Whether this receipt is evidence about the world at all."""
        return self.outcome in MEASURED

    @property
    def body(self):
        """The JSON object to write or embed. ``outcome`` first, so a human sees it first."""
        return {
            "outcome": self.outcome,
            "ok": self.ok,
            **({"reason": self.reason} if self.reason else {}),
            **({"missing": list(self.missing)} if self.missing else {}),
            **self.detail,
        }

    def with_detail(self, **detail):
        """The same outcome carrying more evidence; the outcome itself is never revised here."""
        return Receipt(
            self.outcome, reason=self.reason, missing=self.missing, detail={**self.detail, **detail}
        )

    def __bool__(self):
        """Refuse the truth test, because three of the four outcomes would pass it.

        ``if receipt:`` is the bug in miniature -- a refusal, a fault and an unmeasured run are all
        truthy objects -- so asking costs a TypeError instead of a wrong answer. Use ``.ok``,
        ``.measured``, or compare ``.outcome``.
        """
        raise TypeError(
            f"a Receipt has no truth value: ask for .ok, .measured, or .outcome (this one is {self.outcome})"
        )


def gate(*, taken, ok, what, missing=(), reason="", **detail):
    """One decision, four outcomes, and "the measurement was not taken" as its own answer.

    Both questions are keyword-only and neither has a default, which is the lever: a caller cannot
    reach a pass without first saying the measurement happened. Forgetting is how ``canvas_mock``
    filed a ``mode: volume`` report at 1,764 bytes, how a volume run that never reached its target
    printed as a volume pass in ``app_evidence.py``, and how the readability gate wrote
    plain/plain/plain over a world that had been moved aside.
    """
    if not taken:
        return Receipt.unmeasured(missing or (f"the inputs {what} needs",), what, **detail)
    if ok:
        return Receipt.passed(**detail)
    return Receipt.refused(reason or f"{what} did not hold", **detail)


def attempt(what, work, **detail):
    """Run ``work()``; return ``(value, None)`` or ``(None, Receipt)`` for whatever it raised.

    The generalisation of ``bulk_layer.guarded`` and of ``state_seed.extend_collection``'s shard
    loop. A caller that uses it gets the verdict/fault split for free and, more to the point, gets
    a receipt for the failure at all: a stage with no failure receipt is an infinite retry, and a
    shard that left only a line on stderr made a collection that lost its history read exactly like
    one that never had any.
    """
    try:
        return work(), None
    except Exception as exc:  # noqa: BLE001 -- from_exception is exactly the classifier for this
        return None, Receipt.from_exception(exc, what, **detail)


def rollup(receipts, what, **detail):
    """One receipt for many, by ``_RANK``, carrying every unmeasured name it saw.

    An empty sequence is ``unmeasured`` and never a pass. That is not pedantry: ``barrier.py``
    reported ``ok: true`` for 30 of 39 companies whose worlds did not exist, because its desktop
    comparison found no collisions in a world with no desktops, and ``bool(tasks) and all(...)``
    is the hand-written version of this line.
    """
    receipts = list(receipts)
    outcomes = [outcome_of(r) for r in receipts]
    missing = sorted({name for r in receipts for name in missing_of(r)})
    if not outcomes:
        return Receipt.unmeasured(missing or (f"anything to measure for {what}",), what, **detail)
    worst = max(outcomes, key=lambda name: _RANK[name])
    counts = {name: outcomes.count(name) for name in OUTCOMES if name in outcomes}
    detail = {"outcomes": counts, **detail}
    if worst == PASSED:
        return Receipt.passed(**detail)
    if worst == UNMEASURED:
        return Receipt.unmeasured(missing or (f"inputs for {counts[UNMEASURED]} of {what}",), what, **detail)
    reasons = [reason_of(r) for r, o in zip(receipts, outcomes, strict=True) if o == worst]
    said = "; ".join(r for r in reasons[:4] if r)
    reason = f"{counts[worst]} of {len(outcomes)} {what} {worst}: {said}"
    return Receipt(worst, reason=reason, missing=missing, detail=detail)


# -- reading receipts a dozen stages wrote, in eight vocabularies -----------------------------

# The older spellings, in the order a reader must try them. Each is a real shape on disk:
# ``unmeasured`` is barrier's, ``faults``/``fault`` is bulk_layer's marker, ``oversized`` is
# task_author's refused mining, ``volume_tested`` is the interact reports', and ``status`` carries
# the seeder's and the controller's words.
_LEGACY_STATUS = {
    "failed": FAULTED,
    "error": FAULTED,
    "faulted": FAULTED,
    "seeded_review_failed": REFUSED,
    "refused": REFUSED,
    "skipped": REFUSED,
    "unmeasured": UNMEASURED,
    "running": FAULTED,  # a receipt left mid-flight: the run did not finish, so it measured nothing
    "done": PASSED,
    "passed": PASSED,
    "seeded_reviewed": PASSED,
    "seeded_not_verified": PASSED,
}


def outcome_of(value, default=UNMEASURED):
    """Which of the four this receipt records, whatever vocabulary it was written in.

    The driver reads receipts from a dozen stages and each had its own words, so "did this run
    measure anything" was a different question per file -- which is why the same defect could be
    fixed fourteen times without the fifteenth site being found. ``default`` is ``unmeasured``
    because a missing or unreadable receipt is exactly that: no evidence either way.
    """
    if isinstance(value, Receipt):
        return value.outcome
    if not isinstance(value, dict) or not value:
        return default
    if (named := value.get("outcome")) in OUTCOMES:
        return named
    # A recorded fault outranks everything else the receipt says: bulk_layer's marker keeps the
    # previous layer's entries beside ``faults`` precisely so nothing is lost, and reading its
    # ``ok`` instead would call that marker finished.
    if value.get("faults") or value.get("fault"):
        return FAULTED
    if value.get("unmeasured") is True or value.get("volume_tested") is False:
        return UNMEASURED
    if value.get("oversized"):
        return REFUSED
    if (status := value.get("status")) in _LEGACY_STATUS:
        legacy = _LEGACY_STATUS[status]
        # ``status: done`` with ``skipped: true`` is the controller's shape, and it is a done row
        # only to the resume loop. Everything else reading it wants "nothing ran".
        if legacy == PASSED and value.get("skipped"):
            return REFUSED
        if legacy != PASSED or value.get("ok") is not False:
            return legacy
    if value.get("skipped"):
        # A stage that skipped and still claims ok has declared the skip harmless (calibration
        # with no live apps); a skip without that claim measured nothing about the world.
        return PASSED if value.get("ok") is True else REFUSED
    # ``ok`` is the step's own gate and wins; ``passed`` and ``accepted`` are the measurement words
    # the grade and the authoring receipts use. Only a real boolean is a verdict: ``accepted: []`` is
    # a list of ids and says nothing, which is exactly the shape ``MINE.json`` used to be read by.
    for key in ("ok", "passed", "accepted"):
        if isinstance(flag := value.get(key), bool):
            return PASSED if flag else REFUSED
        if key in value and flag is None:
            return UNMEASURED
    return default


def ok_of(value):
    """``True``/``False``/``None`` for any receipt, derived from its outcome rather than its ``ok``."""
    return OUTCOME_OK[outcome_of(value)]


def passed(value):
    """Whether this receipt is a pass. A fault, a refusal and a missing receipt are all not."""
    return outcome_of(value) == PASSED


def faulted(value):
    """Whether this receipt records an environment failure, so a retry is the right response."""
    return outcome_of(value) == FAULTED


def measured(value):
    """Whether this receipt is evidence about the world at all."""
    return outcome_of(value) in MEASURED


def reason_of(value):
    """The stage's own words for this outcome, from whichever field it used."""
    if isinstance(value, Receipt):
        return value.reason
    if not isinstance(value, dict):
        return ""
    for key in ("reason", "oversized", "fault", "error", "note", "skipped"):
        if isinstance(text := value.get(key), str) and text:
            return text[:REASON_LIMIT]
    if faults := value.get("faults"):
        if isinstance(faults, dict):
            return "; ".join(f"{k}: {v}" for k, v in sorted(faults.items()))[:REASON_LIMIT]
        return str(faults)[:REASON_LIMIT]
    return ""


def missing_of(value):
    """The inputs this receipt says it did not have, as a tuple."""
    if isinstance(value, Receipt):
        return value.missing
    if isinstance(value, dict):
        named = value.get("missing") or value.get("unmeasured")
        if isinstance(named, (list, tuple)):
            return tuple(str(name) for name in named)
    return ()


def summary(value):
    """One line a human can read without knowing the stage: the outcome and its reason."""
    outcome = outcome_of(value)
    reason = reason_of(value)
    return f"{outcome}: {reason}" if reason else outcome
