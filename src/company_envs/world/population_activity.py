"""Derive contact activity summaries from actual CRM events, not initialization dates."""

from datetime import UTC, datetime


def _instant(value):
    value = datetime.fromisoformat(value)
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def expected_activity(crm):
    activity = {}
    contacts = {r["id"]: r.get("companyId") for r in crm.get("contacts", [])}

    def record(company, value):
        if company and isinstance(value, str) and value:
            at = _instant(value)
            activity[company] = max(activity.get(company, at), at)

    for collection in ("deals", "tickets", "tasks", "notes", "meetings"):
        for item in crm.get(collection, []):
            company = item.get("companyId")
            if item.get("associatedType") == "company":
                company = item.get("associatedId")
            elif item.get("associatedType") == "contact":
                company = contacts.get(item.get("associatedId"))
            company = company or contacts.get(item.get("contactId"))
            for field in ("createDate", "lastActivityDate", "completedDate"):
                record(company, item.get(field))
            if item.get("status") in {"closed", "completed"} or item.get("stage") in {
                "closed_won",
                "closed_lost",
            }:
                record(company, item.get("closeDate") or item.get("date"))
    return activity


def activity_errors(crm):
    expected = expected_activity(crm)
    errors = []
    for contact in crm.get("contacts", []):
        value = contact.get("lastActivityDate")
        at = expected.get(contact.get("companyId"))
        if at is not None and (not value or _instant(value).date() < at.date()):
            errors.append(
                f"HubSpot {contact['id']}: lastActivityDate precedes associated activity on {at.date()}"
            )
    return errors


def refresh_activity(crm):
    expected = expected_activity(crm)
    changed = []
    for contact in crm.get("contacts", []):
        at = expected.get(contact.get("companyId"))
        if at is not None and (
            not contact.get("lastActivityDate") or _instant(contact["lastActivityDate"]) < at
        ):
            contact["lastActivityDate"] = at.isoformat()
            changed.append(contact["id"])
    return changed
