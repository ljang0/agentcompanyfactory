"""Compile known CRM profiles instead of reauthoring their native envelopes."""


def hubspot_directories(world, plan, people):
    accounts, contacts = world.get("accounts", []), world.get("contacts", [])
    if not accounts or not contacts or any(not a.get("domain") for a in accounts):
        return {}  # Profile research is a prerequisite; never invent missing companies.
    by_id = {a["id"]: a for a in accounts}
    if any(c.get("account_id") not in by_id or not c.get("email") for c in contacts):
        return {}
    start = min(
        (p["first_date"] for p in plan if p["app_id"] == "hubspot_mock" and p.get("first_date")), default=None
    )
    if start is None:
        return {}
    # Initial directory import at the history boundary, not company founding or
    # the start of a customer relationship. Known source timestamps take precedence.
    imported = start[:10] + "T00:00:00Z"
    owner = people[0].get("name", "") if len(people) == 1 else ""

    def lifecycle(account):
        return "customer" if account.get("status") == "active" else "lead"

    companies = [
        {
            "id": a["id"],
            "name": a["name"],
            "domain": a["domain"],
            "industry": a.get("industry", ""),
            "phone": a.get("phone", ""),
            "city": a.get("city", ""),
            "state": a.get("state", ""),
            "country": a.get("country", ""),
            "numberOfEmployees": a.get("number_of_employees"),
            "annualRevenue": a.get("annual_revenue"),
            "lifecycleStage": lifecycle(a),
            "owner": owner,
            "description": a.get("business_model", ""),
            "createDate": a.get("created_at", imported),
        }
        for a in accounts
    ]
    native_contacts = []
    for contact in contacts:
        account = by_id[contact["account_id"]]
        first, _, last = contact["name"].partition(" ")
        native_contacts.append(
            {
                "id": contact["id"],
                "firstName": first,
                "lastName": last,
                "email": contact["email"],
                "phone": contact.get("phone", ""),
                "jobTitle": contact.get("job_title", ""),
                "companyId": contact["account_id"],
                "lifecycleStage": lifecycle(account),
                "leadStatus": contact.get(
                    "lead_status", "connected" if account.get("status") == "active" else "new"
                ),
                "owner": owner,
                "city": account.get("city", ""),
                "state": account.get("state", ""),
                "country": account.get("country", ""),
                "createDate": contact.get("created_at", imported),
                "lastActivityDate": contact.get("last_activity_at", contact.get("created_at", imported)),
                "timeline": [],
            }
        )
    return {"companies": companies, "contacts": native_contacts}
