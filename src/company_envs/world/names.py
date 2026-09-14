"""Give colliding people new names, deterministically, across a whole seeded world.

Sixty seeds that start together cannot see each other's people, so the same model-default
names land in many companies. The registry now stops that for new seeds; this step repairs
worlds already written: every full name that an earlier company claimed is replaced by a
fresh one, consistently in the canonical world, identities, every app state and every desktop
file, along with the email addresses derived from it. The mapping is recorded in
world/NAMES.json and the new names are registered.
"""

import random
import re
from collections import Counter
from pathlib import Path

from company_envs.storage import decided_by, now, read, write

from .state_seed import looks_like_person, people_in, register_people

FIRST = [
    "Adaeze",
    "Bartholomew",
    "Callista",
    "Desmond",
    "Eirian",
    "Fumiko",
    "Gwendolyn",
    "Hamish",
    "Ilse",
    "Jovan",
    "Katarzyna",
    "Lucien",
    "Marguerite",
    "Nikolai",
    "Oluwaseun",
    "Pilar",
    "Quentin",
    "Rosalind",
    "Sigrid",
    "Tobias",
    "Ulrike",
    "Valentin",
    "Wilhelmina",
    "Xiomara",
    "Yusuf",
    "Zofia",
    "Anselm",
    "Beatrix",
    "Cormac",
    "Delphine",
    "Ezekiel",
    "Fenella",
    "Godfrey",
    "Henrietta",
    "Ignatius",
    "Jocasta",
    "Kasimir",
    "Leopoldine",
    "Matthias",
    "Ngozi",
    "Ottoline",
    "Percival",
    "Rhiannon",
    "Solveig",
    "Thaddeus",
    "Ursula",
    "Vasilis",
    "Winifred",
    "Yevgenia",
    "Zebedee",
    "Amara",
    "Benedikt",
    "Cosima",
    "Dorothea",
    "Emrys",
    "Florentina",
    "Gustavo",
    "Hyacinth",
    "Isidore",
    "Juniper",
    "Kwame",
    "Lavinia",
    "Magnus",
    "Nerissa",
    "Octavia",
    "Philippa",
    "Rafferty",
    "Seraphina",
    "Tancredi",
    "Umberto",
    "Verity",
    "Wendeline",
    "Yannick",
    "Zinaida",
]
LAST = [
    "Abernathy",
    "Bellweather",
    "Calloway",
    "Danforth",
    "Ellington",
    "Fairbanks",
    "Greenhalgh",
    "Hollister",
    "Isherwood",
    "Jankowski",
    "Kettleburn",
    "Lindqvist",
    "Marchetti",
    "Nakagawa",
    "Oyelaran",
    "Pemberton",
    "Quintrell",
    "Rasmussen",
    "Sandoval",
    "Thorvaldsen",
    "Underhill",
    "Vanterpool",
    "Westergaard",
    "Xanthopoulos",
    "Yarborough",
    "Zielinski",
    "Ashdown",
    "Blackwood",
    "Castellanos",
    "Delacroix",
    "Everdene",
    "Featherstone",
    "Galbraith",
    "Hargreaves",
    "Ivanova",
    "Jarnagin",
    "Kirkbride",
    "Lockhart",
    "Mbeki",
    "Northcote",
    "Okonkwo",
    "Pennington",
    "Radcliffe",
    "Sunderland",
    "Trevelyan",
    "Uttley",
    "Villanueva",
    "Whitlock",
    "Yamashiro",
    "Zabriskie",
    "Ainsworth",
    "Brannigan",
    "Cavendish",
    "Drummond",
    "Eskildsen",
    "Fontaine",
    "Goodwillie",
    "Havili",
    "Iqbal",
    "Jefferies",
    "Kowalczyk",
    "Lindgren",
    "Moncrieff",
    "Nightingale",
    "Oduya",
    "Prendergast",
    "Rutherford",
    "Stavropoulos",
    "Tremblay",
    "Vasquez",
]


def _parts(full):
    parts = [re.sub(r"[^a-z]", "", w.lower()) for w in full.split()]
    return [p for p in parts if p]


def email_forms(full):
    """The local-part shapes a name usually takes in an address, keyed by shape."""
    parts = _parts(full)
    if len(parts) < 2:
        return {}
    first, last = parts[0], parts[-1]
    return {
        "first.last": f"{first}.{last}",
        "firstlast": f"{first}{last}",
        "flast": f"{first[0]}{last}",
        "first_last": f"{first}_{last}",
        "first-last": f"{first}-{last}",
        "last.first": f"{last}.{first}",
    }


def identity_forms(full):
    """Every shape a name takes outside prose: mail local parts, handles, id and slug segments.

    Only the address was ever rewritten, so a renamed person kept their old identity everywhere an
    identifier quoted it: a person called Gustavo Calloway has the handle ``celia``, sits in the
    thread ``thread-reuben-arrangement-close`` and the conversation ``dm-avril-galen``. 197 of the
    202 renames on disk leave residue like that. A name is an entity with derived forms, not a
    string that appears in one field.
    """
    parts = _parts(full)
    if len(parts) < 2:
        return {}
    first, last = parts[0], parts[-1]
    return {
        **email_forms(full),
        "last-first": f"{last}-{first}",
        "last_first": f"{last}_{first}",
        "lastfirst": f"{last}{first}",
        "f-last": f"{first[0]}-{last}",
        "f.last": f"{first[0]}.{last}",
        "first": first,
        "last": last,
    }


def replacement_for(taken, rng):
    for _ in range(10_000):
        candidate = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise RuntimeError("ran out of replacement names")


# Fields whose entire value names the person rather than describing them: a handle, a display
# name, a given name on its own. A bare first name is left alone in prose because it is not
# decidable from the string, but a field that holds nothing else is not prose.
IDENTITY_KEYS = frozenset(
    [
        "displayname",
        "shortname",
        "firstname",
        "givenname",
        "lastname",
        "familyname",
        "surname",
        "nickname",
        "preferredname",
        "handle",
        "alias",
        "login",
        "screenname",
        "username",
        "slackhandle",
    ]
)


def _recase(old, new):
    """Give the replacement the case the text it replaces had.

    The whole-value branch returned the map's value verbatim, so a Slack identity whose
    ``fullName`` became "Godfrey Delacroix" got ``displayName: "godfrey"`` -- lower case, beside a
    capitalised full name -- and an id written ``U-RL-AUDREY`` became ``U-RL-Godfrey``. Three
    visual judges blocked a company on exactly that mismatch. 53 identity fields across 18 of the
    42 renamed worlds hold a bare given name, 44 of them ``displayName``.
    """
    if old.isupper() and not old.islower():
        return new.upper()
    if old[:1].isupper():
        return new[:1].upper() + new[1:]
    return new


def _identity_key(key):
    return re.sub(r"[^a-z]", "", key.lower()) in IDENTITY_KEYS


def rename_in(node, name_map, token_map, keys=False, given_map=None, identity=False):
    """Apply the mapping to every string in a tree: whole names and every derived identifier.

    With ``keys``, the keys too: a keyed collection buckets its records under the id itself, so a
    Slack thread lives at ``thread-celia-signature`` and a conversation at ``dm-avril-celia``, and a
    rename that only reached values left the person's old name as the key of their own thread. It is
    off by default because identities.json is keyed by worker id, and that id is also what
    company.json, worker_apps.json and the materials directories name the worker by -- none of which
    this step rewrites.

    ``given_map`` holds bare first and last names that ``token_map`` refuses: they are only three
    letters, or the world has two people who share them. They apply to nothing but the whole value
    of an identity field, where the string is the person and cannot be a month abbreviation or a
    sentence.
    """
    if isinstance(node, dict):
        return {
            rename_in(k, name_map, token_map) if keys and isinstance(k, str) else k: rename_in(
                v, name_map, token_map, keys, given_map, _identity_key(k) if isinstance(k, str) else False
            )
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [rename_in(v, name_map, token_map, keys, given_map, identity) for v in node]
    if isinstance(node, str):
        # A field that is nothing but one derived form -- a handle, a local part, a slug -- is this
        # person and nothing else, so it is replaced whether or not a separator sits beside it.
        stripped = node.strip()
        whole = token_map.get(stripped.casefold())
        if whole is None and identity:
            whole = (given_map or {}).get(stripped.casefold())
        if whole and stripped == node:
            return _recase(node, whole)
        return rename_text(node, name_map, token_map)
    return node


def rename_text(text, name_map, token_map):
    for old, new in name_map.items():
        if old in text:
            text = re.sub(rf"(?<![\w-]){re.escape(old)}(?![\w-])", new, text)
    lowered = text.casefold()
    for old, new in token_map.items():  # longest form first: first.last before first
        if old in lowered:
            text = _swap_token(text, old, new)
            lowered = text.casefold()
    return text


EDGE = r"[-_.@/+:]"


def _swap_token(text, old, new):
    """Replace ``old`` where it is part of an identifier, never where it is a word in a sentence.

    An identifier says which person it means: ``celia@...``, ``dm-avril-celia``,
    ``thread-celia-signature`` and ``@celia`` are all one person, and a separator with a character
    on the far side of it is what makes them identifiers. "Thanks, Celia." is a sentence -- the full
    stop there ends it rather than joining two parts of an id -- and a bare first name in prose is
    not decidable from the string anyway: it may be a Celia the registry never renamed. Prose is
    left to ``name_map``, which only ever matches a whole name.
    """
    token = re.escape(old)
    pattern = re.compile(
        rf"(?:(?<=[A-Za-z0-9]{EDGE})|(?<=[@/])){token}(?![A-Za-z0-9])"
        rf"|(?<![A-Za-z0-9]){token}(?={EDGE}[A-Za-z0-9])",
        re.IGNORECASE,
    )
    return pattern.sub(lambda m: _recase(m.group(0), new), text)


def derived_map(name_map, world_names):
    """Old derived form -> new derived form, for every renamed person, shape by shape.

    A bare first or last name is only mapped when it belongs to one person in this world. Two
    people called Marcus make "marcus" undecidable, and renaming one of them would rewrite the
    other's identifiers too. Ordered longest form first, because ``rename_text`` applies them in
    order and "celia.renshaw" has to be taken before "celia".
    """
    owners = Counter(part for name in world_names for part in set(_parts(name)))
    token_map = {}
    for old, new in name_map.items():
        old_forms, new_forms = identity_forms(old), identity_forms(new)
        for shape, text in old_forms.items():  # first.last stays first.last, flast stays flast
            if shape not in new_forms:
                continue
            if shape in ("first", "last") and (len(text) < 4 or owners[text] > 1):
                continue
            token_map[text] = new_forms[shape]
    return dict(sorted(token_map.items(), key=lambda kv: -len(kv[0])))


def given_map(name_map, world_names):
    """The bare first and last names ``derived_map`` refuses only because they are short.

    ``derived_map`` keeps bare forms out below four characters so that "12-Jun-2026" is not read as
    a person called Jun: in prose and in identifiers a three-letter token is a month as often as a
    name. The whole value of an identity field is neither, so those forms are carried separately
    and applied only there. Names two people share stay out of both maps -- that ambiguity is real
    wherever the token appears.
    """
    owners = Counter(part for name in world_names for part in set(_parts(name)))
    out = {}
    for old, new in name_map.items():
        old_forms, new_forms = identity_forms(old), identity_forms(new)
        for shape in ("first", "last"):
            text = old_forms.get(shape)
            if text and shape in new_forms and len(text) < 4 and owners[text] == 1:
                out[text] = new_forms[shape]
    return out


def non_people(record):
    """The renames in a NAMES.json that today's person test rejects, old name -> new name.

    The detection this step used was looser than it is now, so it claimed 38 of the 202 names it
    renamed across 8 of the 42 worlds that have a NAMES.json: ``Balance Sheet``, ``Open Invoices``,
    ``Payroll Summary``, the Airtable field names ``History ID``, ``Invoice ID`` and ``Team IDs``,
    and one property, ``Juniper Court``, which now appears under a person's name 317 times in
    drive, 357 in gmail and 34 in quickbooks. Every one of the 38 fails ``looks_like_person``
    today, so the set is recoverable from the record without re-reading the world.
    """
    return {old: new for old, new in (record.get("renamed") or {}).items() if not looks_like_person(old)}


# What dedupe_people rewrites, plus the bulk spec: BULK.json is written after the rename and holds
# the new names (35 occurrences in 3 worlds), so a revert that skips it lets add_bulk re-lay the
# renamed records. Call transcripts, task briefs and reports also quote the new names; they are
# evidence of what happened rather than world state, and reverting them is a separate decision.
REVERT_FILES = ("world.json", "identities.json", "BULK.json")


def revert_map(record, renames):
    """new -> old, for whole names and only the derived forms this pass actually wrote.

    Reverting a form the pass never applied is not a no-op: ``Juniper Court`` became ``Xiomara
    Okonkwo``, and undoing the bare last name would rewrite every other Okonkwo in the world. So
    the inverse is built from the shapes recorded in the file -- on disk that is the six address
    shapes under ``emails``; newer records also carry ``identifiers`` -- and a shape is inverted
    only when the record shows it was written. Longest first, as ``rename_text`` requires. The
    short given names the pass applied to identity fields alone come back as a third map, applied
    the same narrow way.
    """
    written = {**(record.get("emails") or {}), **(record.get("identifiers") or {})}
    given = record.get("given") or {}
    name_map = {new: old for old, new in renames.items()}
    tokens, clashes, shorts = {}, {}, {}
    for old, new in renames.items():
        old_forms, new_forms = identity_forms(old), identity_forms(new)
        for shape, text in old_forms.items():
            if shape not in new_forms or written.get(text) != new_forms[shape]:
                continue
            tokens.setdefault(new_forms[shape], set()).add(text)
        for shape in ("first", "last"):
            text = old_forms.get(shape)
            if text and shape in new_forms and given.get(text) == new_forms[shape]:
                shorts.setdefault(new_forms[shape], set()).add(text)
    for new_form, olds in sorted(tokens.items()):
        if len(olds) > 1:  # two renames produced one form; the inverse is not a function
            clashes[new_form] = sorted(olds)
    token_map = {new: next(iter(olds)) for new, olds in tokens.items() if new not in clashes}
    return (
        {new: old for new, old in name_map.items() if new not in clashes},
        dict(sorted(token_map.items(), key=lambda kv: -len(kv[0]))),
        {new: next(iter(olds)) for new, olds in shorts.items() if len(olds) == 1},
        clashes,
    )


def revert_plan(folder):
    """What reverting this world's non-person renames would change, without changing it.

    The revert is destructive in the same way the rename was, so it is computed and reported
    first: the caller decides whether to apply it.
    """
    folder = Path(folder)
    world_dir = folder / "world"
    record = read(world_dir / "NAMES.json") if (world_dir / "NAMES.json").is_file() else {}
    renames = non_people(record)
    plan = {
        "company": folder.name,
        "renamed": renames,
        "kept": sorted(set(record.get("renamed") or {}) - set(renames)),
        "files": {},
        "occurrences": 0,
    }
    if not renames:
        return plan
    name_map, token_map, short_map, clashes = revert_map(record, renames)
    plan.update({"name_map": name_map, "token_map": token_map, "given_map": short_map, "ambiguous": clashes})
    for path in _revert_paths(world_dir):
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        hits = sum(text.count(new) for new in name_map) + sum(
            len(re.findall(rf"(?i)(?<![A-Za-z0-9]){re.escape(new)}(?![A-Za-z0-9])", text))
            for new in token_map
        )
        if hits:
            plan["files"][path.relative_to(folder).as_posix()] = hits
            plan["occurrences"] += hits
    return plan


def _revert_paths(world_dir):
    for name in REVERT_FILES:
        if (world_dir / name).is_file():
            yield world_dir / name
    yield from sorted(world_dir.glob("*.state.json"))
    yield from sorted(p for p in (world_dir / "materials").rglob("*") if p.is_file())


def revert_non_people(folder, apply=False):
    """Undo every rename whose old name fails today's person test; returns the plan.

    Defaults to reporting. With ``apply`` the world files are rewritten and the reversal is
    recorded in NAMES.json under ``reverted``, so the world says what was done to it twice.
    """
    folder = Path(folder)
    plan = revert_plan(folder)
    if not apply or not plan["renamed"]:
        return plan
    world_dir = folder / "world"
    name_map, token_map, short_map = plan["name_map"], plan["token_map"], plan["given_map"]
    for path in _revert_paths(world_dir):
        if path.suffix == ".json" and path.parent == world_dir:
            write(
                path,
                rename_in(
                    read(path),
                    name_map,
                    token_map,
                    keys=path.name.endswith(".state.json"),
                    given_map=short_map,
                ),
            )
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        reverted = rename_text(text, name_map, token_map)
        if reverted != text:
            path.write_text(reverted)
    record = read(world_dir / "NAMES.json")
    record["reverted"] = {
        "at": now(),
        "renamed": plan["renamed"],
        "identifiers": token_map,
        "given": short_map,
    }
    record["renamed"] = {k: v for k, v in (record.get("renamed") or {}).items() if k not in plan["renamed"]}
    record["emails"] = {k: v for k, v in (record.get("emails") or {}).items() if k not in token_map.values()}
    record["identifiers"] = {
        k: v for k, v in (record.get("identifiers") or {}).items() if k not in token_map.values()
    }
    record["given"] = {k: v for k, v in (record.get("given") or {}).items() if k not in short_map.values()}
    record["decided_by"] = decided_by(DECIDES_THIS)
    # NAMES.json deliberately carries no company_envs.receipt outcome, and the omission was decided
    # rather than missed (2026-09-10). It is a record of what was renamed, not a verdict: a world
    # sharing no person with an earlier company has nothing to rename, and that is a legitimate pass,
    # so a receipt here would add a word and no information. The driver's gate on this marker is its
    # existence, which is the right gate for a record of that kind. If a later change gives dedupe a
    # way to fail partway -- renaming some people and not others -- that is when this needs one.
    write(world_dir / "NAMES.json", record)
    plan["applied"] = True
    return plan


# What decides which people a world renames: these rules. The same file is
# scripts/batch_companies.py's NAMES_CODE, and the digest recorded in a NAMES.json is compared against
# this tuple, so the two have to name the same files. All 42 NAMES.json on disk read fresh on
# 2026-09-10 against a module changed four times that week, which is the 42 markers the migration
# re-takes, at 1-9 s each.
DECIDES_THIS = (Path(__file__),)


def current(marker):
    """Whether a NAMES.json was decided by these rules as they stand now.

    The driver's accept slot, bulk_layer.current's contract. A marker with no ``decided_by`` is stale.
    A world with nothing to rename rewrites no state file, so re-taking this costs the dedupe and
    starts no cascade.
    """
    return isinstance(marker, dict) and marker.get("decided_by") == decided_by(DECIDES_THIS)


def dedupe_people(folder, companies_dir=None, seed=0):
    """Rename every person this world shares with an earlier company; returns the mapping."""
    folder = Path(folder)
    companies_dir = Path(companies_dir) if companies_dir else folder.parent
    world_dir = folder / "world"
    files = [world_dir / "world.json", world_dir / "identities.json", *sorted(world_dir.glob("*.state.json"))]
    trees = {p: read(p) for p in files if p.is_file()}
    if not trees:
        raise ValueError(f"{folder} has no seeded world")
    names, _ = people_in(list(trees.values()))
    # Claim this world's people first, under the registry lock, so a parallel company that
    # shares an unregistered name loses the race and renames instead of both keeping it.
    registry = register_people(companies_dir, folder.name, names, set())
    taken = set(registry["names"])
    colliding = sorted(n for n in names if registry["names"].get(n) not in (None, folder.name))
    rng = random.Random(f"{seed}:{folder.name}")
    name_map = {old: replacement_for(taken, rng) for old in colliding}
    token_map = derived_map(name_map, names)
    short_map = given_map(name_map, names)
    if name_map:
        for path, tree in trees.items():
            write(
                path,
                rename_in(
                    tree,
                    name_map,
                    token_map,
                    keys=path.name.endswith(".state.json"),
                    given_map=short_map,
                ),
            )
        for path in (world_dir / "materials").rglob("*"):
            if path.is_file():
                try:
                    text = path.read_text()
                except UnicodeDecodeError:
                    continue
                renamed = rename_text(text, name_map, token_map)
                if renamed != text:
                    path.write_text(renamed)
    write(
        world_dir / "NAMES.json",
        {
            "renamed_at": now(),
            "decided_by": decided_by(DECIDES_THIS),
            "renamed": name_map,
            "emails": {k: v for k, v in token_map.items() if "." in k or len(k.split()) > 1},
            "identifiers": token_map,
            # Recorded separately because they were applied to identity fields only; a revert has
            # to put them back the same way rather than through the identifier rules.
            "given": short_map,
        },
    )
    register_people(companies_dir, folder.name, set(name_map.values()) | (names - set(name_map)), set())
    return name_map
