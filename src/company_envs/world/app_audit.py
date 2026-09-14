"""Check documented hub features against a seeded browser session.

A found control proves reachability only. A missing observation needs follow-up;
it does not prove a feature is absent. Empty seeds cannot prove record visibility.
"""

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from company_envs.world.hub_app import UI_STATE_KEYS, HubProcess, validate_state

# These arrays remember navigation or selection, rather than independent records.
NAVIGATION_KEYS = {"recentMatters", "recentContacts", "recentlyViewed", "following", "dismissedNotifications"}
RECORD_KEYS = {"notifications", "invitations", "callHistory"}
INTERNAL_ACTIONS = {"INIT_STATE", "SET_STATE", "LOAD_STATE", "RESET_STATE", "RESET_DB"}


def schema_inventory(text):
    """Read full schema tables, including typed arrays and action-table variants."""
    collections, routes, actions, internal = {}, [], [], []
    section = ""
    in_state_table = False
    state_rows = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            section = line[3:].lower()
            in_state_table = section.startswith("state schema")
        elif line.startswith("### ") and in_state_table and state_rows:
            in_state_table = False
        if not line.startswith("|"):
            continue
        cells = [c.strip().strip("`") for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        if len(cells) < 2 or cells[0].lower() in {
            "key",
            "path",
            "route",
            "url pattern",
            "action",
            "action type",
            "user action",
        }:
            continue
        if re.fullmatch(r"[-: ]+", cells[0]):
            continue
        name = cells[0]
        if in_state_table:
            state_rows = True
            kind, description = cells[1].lower(), " ".join(cells[2:]).lower()
            array = "[]" in kind or kind.startswith("array")
            keyed = kind.startswith("object") and re.search(r"map|keyed|dictionary|→|record", description)
            if (
                (array or keyed)
                and name not in (UI_STATE_KEYS - RECORD_KEYS) | NAVIGATION_KEYS
                and not re.search(r"[.\[\]]", name)
            ):
                collections[name] = {"type": cells[1], "description": " ".join(cells[2:]), "line": number}
        elif "routes" in section and name.startswith("/"):
            if urlsplit(name).path not in {"/go", "/state", "/post", "/upload"}:
                routes.append({"route": name, "description": " ".join(cells[1:]), "line": number})
        elif "actions" in section or section.startswith("observable state changes"):
            row = {"action": name, "description": " | ".join(cells[1:]), "line": number}
            (internal if name in INTERNAL_ACTIONS else actions).append(row)
    # Repeated descriptions still retain every source line for inspection.
    return {"collections": collections, "routes": routes, "actions": actions, "internal_actions": internal}


def records(value):
    """Flatten record maps and maps of message lists, preserving record objects."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [record for item in value.values() for record in (item if isinstance(item, list) else [item])]
    return []


def record_labels(value):
    labels = []
    for record in records(value):
        if isinstance(record, str):
            labels.append(record)
        elif isinstance(record, dict):
            full_name = " ".join(str(record.get(k, "")) for k in ("firstName", "lastName")).strip()
            candidates = [full_name] if full_name else []
            candidates += [
                record.get(k)
                for k in (
                    "name",
                    "fullName",
                    "displayName",
                    "title",
                    "subject",
                    "summary",
                    "content",
                    "body",
                    "payee",
                    "description",
                    "number",
                    "matterNumber",
                    "billNumber",
                    "label",
                    "lastMessage",
                    "email",
                )
            ]
            # Prefer a display label over generic roles, dates or foreign keys.
            label = next((x for x in candidates if isinstance(x, str) and x.strip()), None)
            if label:
                labels.append(re.sub(r"<[^>]+>", "", label).strip())
    return list(dict.fromkeys(labels))


def resolve_route(route, state):
    """Use seeded IDs in documented route parameters; refuse invented records."""
    if "*" in route:
        return route.replace("*", "audit-unmatched-route")
    aliases = {
        "labelId": "labels",
        "docId": "documents",
        "threadId": "emails",
        "ticketId": "tickets",
        "viewId": "views",
        "userId": "users",
        "orgId": "organizations",
        "key": "projects",
        "reportId": "reportCategories",
    }

    def replace(match):
        parameter = match.group(1)
        collection = aliases.get(parameter)
        if collection is None:
            prefix = route[: match.start()].rstrip("/").split("/")[-1]
            collection = prefix if prefix in state else prefix + "s"
        candidates = records(state.get(collection))
        if parameter == "reportId":
            candidates = [r for category in candidates for r in category.get("reports", [])]
        for record in candidates:
            if not isinstance(record, dict):
                continue
            singular = collection[:-3] + "y" if collection.endswith("ies") else collection.removesuffix("s")
            fields = [parameter] if parameter not in {"id", "key"} else [parameter, singular + "Id", "id"]
            fields += ["id"]
            for field in fields:
                if field in record:
                    return quote(str(record[field]), safe="")
        raise ValueError(f"No seeded value for {parameter} in {collection}")

    return re.sub(r":([A-Za-z][A-Za-z0-9_]*)", replace, route)


def session_url(base, route, sid):
    parts = urlsplit(route)
    if parts.scheme or parts.netloc or not route.startswith("/"):
        raise ValueError("Audit routes must stay on the local app")
    query = dict(parse_qsl(parts.query))
    query["sid"] = sid
    return base + urlunsplit(("", "", parts.path, urlencode(query), parts.fragment))


def normalized(text):
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def action_names(action):
    """Only match whole control labels; a generic 'Create' proves no entity type."""
    name = re.sub(r"\([^)]*\)", "", action).strip()
    name = normalized(name.replace("_", " "))
    name = re.sub(r"\b(a|an|new)\b ?", "", name).strip()
    names = {name}
    if name.startswith(("add ", "create ")):
        noun = name.split(" ", 1)[1]
        names.update(f"{verb} {noun}" for verb in ("add", "create", "new"))
    if name.startswith("update "):
        names.add("edit " + name[7:])
    return names


SNAPSHOT = """(selectors) => {
    const visible = el => el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
    const text = el => (el.innerText || el.textContent || '').trim();
    const controls = [...document.querySelectorAll('button,a,input,textarea,select,[role="button"],[role="menuitem"],[role="tab"],[contenteditable="true"],.cursor-pointer,div[style*="cursor: pointer"]')]
      .filter(visible).map(el => ({name: el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') || (el.labels && [...el.labels].map(text).join(' ')) || text(el),
        tag: el.tagName, disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'}));
    const lists = {};
    for (const [key, selector] of Object.entries(selectors)) {
      lists[key] = [...document.querySelectorAll(selector)].filter(visible).map(text).filter(Boolean);
    }
    return {text: document.body.innerText, controls, lists};
}"""


class BrowserUI:
    """Small Playwright wrapper; writes use ordinary clicks and typing only."""

    def __init__(self, browser, base, sid, output, selectors):
        self.browser = browser
        self.base, self.sid, self.output, self.selectors = base, sid, Path(output), selectors
        self.errors = []
        self.context = None
        self.fresh()

    def fresh(self):
        """Discard local storage and open dialogs before a separate check."""
        if self.context is not None:
            self.context.close()
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.page = self.context.new_page()
        self.page.set_default_timeout(2500)
        self.page.on(
            "console",
            lambda message: (
                self.errors.append(
                    {
                        "kind": "console",
                        "message": message.text,
                        "url": message.location.get("url") or self.page.url,
                        "page": self.page.url,
                    }
                )
                if message.type == "error"
                else None
            ),
        )
        self.page.on(
            "pageerror",
            lambda error: self.errors.append(
                {"kind": "javascript", "message": str(error), "url": self.page.url}
            ),
        )
        self.page.on("dialog", lambda dialog: dialog.dismiss())

    def navigate(self, route):
        response = self.page.goto(
            session_url(self.base, route, self.sid), wait_until="domcontentloaded", timeout=15000
        )
        self.pause()
        if response and response.status >= 400:
            raise ValueError(f"HTTP {response.status} for {route}")

    def pause(self):
        self.page.wait_for_timeout(350)

    def step(self, step):
        operation = step["op"]
        if operation == "press" and not any(k in step for k in ("selector", "role", "text", "placeholder")):
            self.page.keyboard.press(step["value"])
        else:
            if "selector" in step:
                target = self.page.locator(step["selector"])
            elif "role" in step:
                target = self.page.get_by_role(step["role"], name=step["name"], exact=True)
            elif "placeholder" in step:
                target = self.page.get_by_placeholder(step["placeholder"], exact=True)
            else:
                target = self.page.get_by_text(step["text"], exact=True)
            if "index" in step:
                target = target.nth(step["index"])
            if operation in {"click", "dblclick", "hover"}:
                getattr(target, operation)()
            elif operation in {"fill", "press", "select_option"}:
                getattr(target, operation)(step["value"])
            else:
                raise ValueError(f"Unknown browser operation: {operation}")
        self.pause()

    def snapshot(self, name):
        snapshot = self.page.evaluate(SNAPSHOT, self.selectors)
        snapshot["url"] = self.page.url
        snapshot["screenshot"] = f"{name}.png"
        self.page.screenshot(path=str(self.output / snapshot["screenshot"]))
        return snapshot

    def close(self):
        self.context.close()


def state_value(client, sid):
    response = client.current(sid)
    return response.get("stored_state", response)


# A write recipe is three facts: which collection the app appends to, what the creation form's
# field is called, and what its submit button says. All three are in the app's own source.
# One idiom is not enough to find the append: the hub apps write it eight ways, and reading only
# the first two leaves 13 of 88 apps looking as if their source never appends to a collection.
_APPEND = re.compile(r"(?<![\w.'\"])([A-Za-z_]\w*)\s*:\s*\[[^\]]*?\.\.\.\s*[\w?.\[\]]*?\.\1\b")
_KEYED = re.compile(r"\.\.\.\s*\(?\s*[\w?.]*\.([A-Za-z_]\w*)\s*\[")
# newState.ec2 = [...prev.ec2, instance], and const expenses = [...state.expenses, expense].
_ASSIGNED = re.compile(r"([A-Za-z_]\w*)\s*=\s*\[[^\]]*?\.\.\.\s*[\w?.\[\]]*?\.\1\b")
# items: {...state.items, [folder.id]: folder} -- a record map grows by key, not by index.
_MAP_INSERT = re.compile(r"([A-Za-z_]\w*)\s*[:=]\s*\{[^{}]*?\.\.\.\s*[\w?.]*\.?\1\b[^{}]*?\[[^\]]*\]\s*:")
_MAP_INSERT_FIRST = re.compile(r"([A-Za-z_]\w*)\s*[:=]\s*\{[^{}]*?\[[^\]]*\]\s*:[^{}]*?\.\.\.\s*[\w?.]*\.?\1\b")
_MUTATED = re.compile(r"\b([A-Za-z_]\w*)\s*\.\s*(?:push|unshift|concat)\s*\(")
# setMeetings([...meetings, meeting]) names the collection in the setter, not in a reducer case.
_SETTER = re.compile(r"\bset([A-Z]\w*)\s*\(\s*(?:[\[{]|\(?\s*\w+\s*\)?\s*=>\s*[\[{(])")
# const newTransactions = [...prev.transactions]; newTransactions.push(order) -- robinhood's idiom.
_ALIASED = re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*[\[{]\s*\.\.\.\s*[\w?.]*\.([A-Za-z_]\w*)\b")
_ALIAS_REACH = 4000
_CASE = re.compile(r"case\s*['\"]([A-Za-z0-9_]+)['\"]")
_FUNC = re.compile(r"(?:const|function)\s+([A-Za-z_]\w*)\s*=?\s*(?:\([^)]*\)|async)")
_PLACEHOLDER = re.compile(r"placeholder=[\"']([^\"'{}]+)[\"']")
_BUTTON = re.compile(r"<button[^>]*>\s*([^<>{]{2,40}?)\s*</button>", re.DOTALL)
_SOURCE_SUFFIXES = {".jsx", ".js", ".tsx", ".ts"}


def append_sites(text):
    """Every place the source grows a collection, as (collection, idiom, position).

    An append is written eight ways across the hub apps. A search that reads only the object-literal
    spread sees no write at all in aws_console (``newState.ec2 = [...prev.ec2, instance]``),
    Expensify (``const expenses = [...state.expenses, expense]``), google_drive
    (``items: {...state.items, [folder.id]: folder}``), klaviyo (``profiles.push(profile)``),
    zoom_web (``setMeetings([...meetings, meeting])``) or robinhood (a copy appended to under a
    local name), and those apps then look unwritable when they are not.
    """
    for idiom, pattern in (
        ("array_spread", _APPEND),
        ("keyed_index", _KEYED),
        ("assign_spread", _ASSIGNED),
        ("map_insert", _MAP_INSERT),
        ("map_insert", _MAP_INSERT_FIRST),
        ("mutated", _MUTATED),
    ):
        for match in pattern.finditer(text):
            yield match.group(1), idiom, match.start()
    for match in _SETTER.finditer(text):
        name = match.group(1)
        yield name[0].lower() + name[1:], "setter", match.start()
    for match in _ALIASED.finditer(text):
        alias, collection = match.group(1), match.group(2)
        tail = text[match.end() : match.end() + _ALIAS_REACH]
        grown = re.search(rf"\b{re.escape(alias)}\s*\.\s*(?:push|unshift|splice)\s*\(", tail)
        assigned = re.search(rf"\b{re.escape(collection)}\s*:\s*{re.escape(alias)}\b", tail)
        if grown or assigned:
            yield collection, "aliased", match.start()


def derive_write_hints(source_root, collections=()):
    """Where each collection is appended to, and the form vocabulary that reaches it.

    Twelve apps have a hand-written probe plan and they are the only real write evidence in the
    project; a generic probe found a creation form on one of eight apps and landed a write on none.
    The difference is knowledge the app already carries: 76 of 88 apps append to a documented
    collection somewhere in their own source under the two original patterns, and 84 of 88 once the
    append is read in every idiom the apps use (append_sites). The component that dispatches that
    append names its field and its button. A plan author -- or a search -- starts from these three
    facts rather than from the home page.
    """
    root = Path(source_root)
    if not root.is_dir():
        return {}
    texts = {p: p.read_text(errors="replace") for p in root.rglob("*") if p.suffix in _SOURCE_SUFFIXES}
    wanted = set(collections)
    writers = {}
    for path, text in texts.items():
        for collection, idiom, position in append_sites(text):
            if wanted and collection not in wanted:
                continue
            head = text[:position]
            case = (list(_CASE.finditer(head)) or [None])[-1]
            func = (list(_FUNC.finditer(head)) or [None])[-1]
            writers.setdefault(collection, []).append(
                (path, idiom, case.group(1) if case else None, func.group(1) if func else None)
            )
    hints = {}
    for collection, found in sorted(writers.items()):
        components, actions, idioms = set(), set(), set()
        for path, idiom, case, func in found:
            actions.update(filter(None, (case, func)))
            idioms.add(idiom)
            for key in filter(None, (case, func)):
                # An action name is quoted where it is dispatched; a mutator is called by name.
                quoted = rf"['\"]{re.escape(key)}['\"]" if key.isupper() else rf"\b{re.escape(key)}\s*\("
                components.update(
                    other for other, text in texts.items() if other != path and re.search(quoted, text)
                )
        placeholders, buttons = [], []
        for path in sorted(components, key=lambda p: p.name):
            placeholders += _PLACEHOLDER.findall(texts[path])
            buttons += [b.strip() for b in _BUTTON.findall(texts[path]) if "{" not in b and b.strip()]
        hints[collection] = {
            "writers": sorted({p.relative_to(root).as_posix() for p, _, _, _ in found}),
            "idioms": sorted(idioms),
            "actions": sorted(actions),
            "forms": sorted(p.relative_to(root).as_posix() for p in components),
            "placeholders": list(dict.fromkeys(placeholders))[:12],
            "buttons": list(dict.fromkeys(buttons))[:12],
        }
    return hints


def write_probe(ui, client, sid, probe):
    """Require the requested value in the changed collection, including after reload."""
    if not probe:
        return {"ok": False, "gap": "No UI write recipe supplied"}
    result = {
        "ok": False,
        "steps": probe["steps"],
        "collection": probe["collection"],
        "marker": probe["marker"],
    }
    try:
        ui.navigate(probe.get("route", "/"))
        for step in probe.get("prepare", []):
            ui.step(step)
        result["before_view"] = ui.snapshot("write-before")
        before = state_value(client, sid)
        result["before"] = before.get(probe["collection"])
        if probe["marker"] in json.dumps(result["before"]):
            raise ValueError("Write marker already exists before the edit")
        for step in probe["steps"]:
            ui.step(step)
        for _ in range(20):
            after = state_value(client, sid)
            value = after.get(probe["collection"])
            if value != result["before"] and probe["marker"] in json.dumps(value):
                break
            ui.pause()
        else:
            raise ValueError("UI edit did not save the expected value to /state")
        result["after_view"] = ui.snapshot("write-after")
        result["after"] = value
        result["changed_keys"] = sorted(
            k for k in before.keys() | after.keys() if before.get(k) != after.get(k)
        )
        ui.navigate(probe.get("route", "/"))
        result["after_reload"] = state_value(client, sid).get(probe["collection"])
        result["ok"] = probe["marker"] in json.dumps(result["after_reload"])
        if not result["ok"]:
            result["gap"] = "The write disappeared after reload"
    except Exception as error:  # noqa: BLE001 - keep failed browser checks in the report
        result["gap"] = str(error)
        try:
            result["failure_view"] = ui.snapshot("write-failure")
            result["after"] = state_value(client, sid).get(probe["collection"])
        except Exception as capture_error:  # noqa: BLE001 - preserve the original failure
            result["capture_error"] = str(capture_error)
    return result


def audit_session(ui, client, sid, state, inventory, plan):
    """Collect evidence without treating API state, empty seeds or shell text as UI proof."""
    report = {
        "renders": False,
        "collections_visible": {},
        "routes_ok": [],
        "actions_found": [],
        "write_roundtrip": {},
        "views": {},
        "gaps": [],
    }
    views = report["views"]

    def visit(name, route, steps=()):
        error_start = len(ui.errors)
        try:
            ui.fresh()
            client.seed(sid, state)
            ui.navigate(resolve_route(route, state))
            for step in steps:
                ui.step(step)
            snapshot = ui.snapshot(name)
            snapshot["errors"] = ui.errors[error_start:]
            snapshot["ok"] = (
                bool(snapshot["text"].strip() and snapshot["controls"]) and not snapshot["errors"]
            )
        except Exception as error:  # noqa: BLE001 - keep failed browser checks in the report
            try:
                snapshot = ui.snapshot(name)
            except Exception:  # noqa: BLE001 - a crashed page may not allow screenshots
                snapshot = {"text": "", "controls": [], "lists": {}}
            snapshot.update({"ok": False, "gap": str(error), "errors": ui.errors[error_start:]})
        views[name] = snapshot
        return snapshot

    home = visit("home", "/")
    report["renders"] = home["ok"]
    # A route must show its own content, not just a fallback with a healthy HTTP code.
    fallback = visit("unmatched", "/audit-route-that-does-not-exist")
    for index, entry in enumerate(inventory["routes"]):
        name = f"route-{index:03}"
        snapshot = visit(name, entry["route"])
        expected = plan.get("route_text", {}).get(entry["route"])
        same_fallback = normalized(snapshot["text"]) == normalized(fallback["text"])
        home_url = urlsplit(home.get("url", ""))
        target_url = urlsplit(resolve_route(entry["route"], state)) if ":" not in entry["route"] else None
        is_home_route = bool(
            home.get("url")
            and target_url
            and (target_url.path, target_url.fragment) == (home_url.path, home_url.fragment)
            and not target_url.query
        )
        allowed_fallback = (
            is_home_route
            or entry["route"] == "/"
            or "redirect" in entry["description"].lower()
            or "fallback" in entry["description"].lower()
            or "falls back" in entry["description"].lower()
        )
        ok = snapshot["ok"] and (not same_fallback or allowed_fallback)
        if expected:
            ok = snapshot["ok"] and expected in snapshot["text"]
        report["routes_ok"].append(
            {
                **entry,
                "ok": ok,
                "view": name,
                "gap": ""
                if ok
                else snapshot.get(
                    "gap",
                    "Route is blank, has errors, lacks expected text, or matches the unknown-route fallback",
                ),
            }
        )
    for index, view in enumerate(plan.get("views", [])):
        visit(view.get("name", f"extra-{index:03}"), view.get("route", "/"), view.get("steps", []))
    for key in inventory["collections"]:
        labels = record_labels(state.get(key))
        evidence = []
        allowed_views = plan.get("collection_views", {}).get(key)
        for name, view in views.items():
            if name == "unmatched" or (allowed_views is not None and name not in allowed_views):
                continue
            for row in view["lists"].get(key, []):
                for label in labels:
                    if normalized(label) and re.search(
                        r"(?<!\w)" + re.escape(normalized(label)) + r"(?!\w)", normalized(row)
                    ):
                        evidence.append({"view": name, "label": label, "row": row[:1000]})
        report["collections_visible"][key] = {
            "ok": bool(evidence),
            "seed_records": len(records(state.get(key))),
            "labels_tried": labels,
            "evidence": evidence[:10],
            "gap": ""
            if evidence
            else (
                "Seed collection is empty; record visibility is unverified"
                if not records(state.get(key))
                else "No seeded record label observed in a list; inspect the saved views"
            ),
        }
    for entry in inventory["actions"]:
        names = action_names(entry["action"])
        # Explicit aliases bind a schema action to a control in a particular saved view.
        aliases = plan.get("action_controls", {}).get(entry["action"], [])
        evidence = []
        for name, view in views.items():
            if name == "unmatched":
                continue
            for control in view["controls"]:
                if not control["disabled"] and (
                    normalized(control["name"]) in names
                    or any(alias["view"] == name and alias["name"] == control["name"] for alias in aliases)
                ):
                    evidence.append({"view": name, "control": control})
        report["actions_found"].append(
            {
                **entry,
                "ok": bool(evidence),
                "evidence": evidence[:5],
                "gap": ""
                if evidence
                else "No matching enabled control observed; requires a UI probe or manual follow-up",
            }
        )
    ui.fresh()
    client.seed(sid, state)
    report["write_roundtrip"] = write_probe(ui, client, sid, plan.get("write"))
    report["console_errors"] = ui.errors
    for category in ("collections_visible", "routes_ok", "actions_found"):
        entries = report[category]
        entries = (
            [{"collection": key, **value} for key, value in entries.items()]
            if isinstance(entries, dict)
            else entries
        )
        report["gaps"].extend({"check": category, **item} for item in entries if not item["ok"])
    for name, view in views.items():
        if name != "unmatched" and not name.startswith("route-") and not view["ok"]:
            report["gaps"].append(
                {"check": "view", "view": name, "gap": view.get("gap", "View is blank or has browser errors")}
            )
    if not report["renders"]:
        report["gaps"].append(
            {"check": "renders", "gap": home.get("gap", "Home is blank or has console/JavaScript errors")}
        )
    if not report["write_roundtrip"]["ok"]:
        report["gaps"].append({"check": "write_roundtrip", "gap": report["write_roundtrip"]["gap"]})
    return report


def audit_app(app, built, schema_path, state_path, output, browser, plan):
    """Serve a private build and always stop it, including after browser failures."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    evidence = output / app
    evidence.mkdir(exist_ok=True)
    schema = Path(schema_path).read_text()
    state_bytes = Path(state_path).read_bytes()
    state = json.loads(state_bytes)
    inventory = schema_inventory(schema)
    seed_schema_gap = None
    try:
        validate_state({"id": app, "state_keys": list(state)}, state, schema)
    except (ValueError, TypeError) as error:
        seed_schema_gap = str(error)
    sid = "audit-" + uuid4().hex
    started = time.monotonic()
    with HubProcess(built) as process:
        process.client.seed(sid, state)
        api_ok = state_value(process.client, sid) == state
        selectors = {
            key: "tbody tr, [role=row], [role=listitem], ul > li" for key in inventory["collections"]
        }
        selectors.update(plan.get("collection_selectors", {}))
        ui = BrowserUI(browser, process.client.base_url, sid, evidence, selectors)
        try:
            report = audit_session(ui, process.client, sid, state, inventory, plan)
        finally:
            ui.close()
    if not plan.get("write"):
        # "No UI write recipe supplied" is the commonest gap in the audit and the most expensive
        # one to close by hand. The app's own source says which collection it appends to and what
        # the form that does it is called, so the gap carries the answer to itself.
        report["write_roundtrip"]["hints"] = derive_write_hints(
            Path(schema_path).parent / "src", inventory["collections"]
        )
    if seed_schema_gap:
        report["gaps"].append({"check": "seed_schema", "gap": seed_schema_gap})
    report.update(
        {
            "app": app,
            "time": datetime.now(UTC).isoformat(),
            "seconds": round(time.monotonic() - started, 2),
            "schema_sha256": hashlib.sha256(schema.encode()).hexdigest(),
            "seed_sha256": hashlib.sha256(state_bytes).hexdigest(),
            "schema_path": str(schema_path),
            "state_path": str(state_path),
            "api_seed_readback": api_ok,
            "inventory": inventory,
            "plan": plan,
        }
    )
    marker = Path(built) / ".hub-build.json"
    report["build"] = json.loads(marker.read_text()) if marker.exists() else None
    (output / f"{app}.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def write_summary(reports, output):
    """Keep coverage gaps separate from verified UI failures and empty fixtures."""
    lines = [
        "# Hub UI audit",
        "",
        "Found controls prove reachability, not correct action results. Only the write probe checks persistence. Missing observations are follow-up items, not proof of absent features. Empty collections cannot pass. Screenshots and visible list/control evidence are in each app's folder and JSON.",
        "",
        "| App | API seed | Renders | Collections | Routes | Actions | UI write |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    def count(entries):
        values = list(entries.values()) if isinstance(entries, dict) else entries
        return f"{sum(item['ok'] for item in values)}/{len(values)}"

    for report in reports:
        lines.append(
            f"| {report['app']} | {report.get('api_seed_readback', False)} | {report['renders']} | {count(report['collections_visible'])} | {count(report['routes_ok'])} | {count(report['actions_found'])} | {report['write_roundtrip']['ok']} |"
        )
    for report in reports:
        lines += ["", f"## {report['app']}", ""]
        if report.get("error"):
            lines.append(report["error"])
        for gap in report["gaps"]:
            label = gap.get("collection", gap.get("route", gap.get("action", gap["check"])))
            lines.append(f"- {label}: {gap['gap'].splitlines()[0]}")
    Path(output, "SUMMARY.md").write_text("\n".join(lines) + "\n")
