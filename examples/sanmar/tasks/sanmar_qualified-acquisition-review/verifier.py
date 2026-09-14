import re
import math
from datetime import date, datetime


CAMPAIGNS = {
    'CAM-DECOR': ('AS-DECOR', ('Independent decorators',)),
    'CAM-START': ('AS-START', ('Start with wholesale basics',)),
    'CAM-RETURN': ('AS-RETURN', ('Return to your catalog',)),
}

OBLIGATIONS = (
    ('CL-SVC-019', 'P001', 'CT-P001', 'APP-605', 'Kestrel'),
    ('CL-SVC-020', 'P002', 'CT-P002', 'APP-606', 'Moonrise'),
    ('CL-SVC-021', 'P003', 'CT-P003', 'APP-607', 'Oakline'),
    ('CL-SVC-022', 'P004', 'CT-P004', 'APP-608', 'Dovetail'),
    ('CL-SVC-023', 'P005', 'CT-P005', 'APP-609', 'Hillcrest'),
    ('SVC-107', 'P006', 'CT-P006', 'APP-610', 'Slate Run'),
)


def plain(value):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>', '', value, flags=re.I | re.S)
    value = re.sub(r'</(?:p|div|tr|li|h[1-6])\s*>|<br\s*/?>', '\n', value, flags=re.I)
    value = re.sub(r'</(?:td|th)\s*>', ' | ', value, flags=re.I)
    value = re.sub(r'<[^>]*>', ' ', value)
    for old, new in (('&nbsp;', ' '), ('&amp;', '&'), ('&quot;', '"'), ('&#39;', "'"), ('&#x27;', "'")):
        value = value.replace(old, new)
    return value.strip()


def normalized(value):
    return re.sub(r'\s+', ' ', plain(value)).strip().lower()


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    value = value.strip().replace(',', '')
    value = re.sub(r'^(?:USD\s*|\$\s*)', '', value, flags=re.I)
    if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)', value):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def same_amount(a, b):
    a, b = number(a), number(b)
    return a is not None and b is not None and abs(a - b) <= 0.011


def absolute_date(value):
    if not isinstance(value, str):
        return None
    if not re.match(r'^\d{4}-\d{2}-\d{2}(?:$|T| )', value):
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        return datetime.fromisoformat(value.replace('Z', '+00:00')).date()
    except ValueError:
        return None


def index_records(state, app, collection):
    app_state = state.get(app)
    if not isinstance(app_state, dict):
        return None, 'Missing ' + app + ' records.'
    records = app_state.get(collection)
    if not isinstance(records, list):
        return None, 'Missing or unreadable ' + app + '.' + collection + '.'
    result = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get('id'), str) or not record.get('id'):
            return None, 'A record in ' + app + '.' + collection + ' has no readable identity.'
        rid = record['id']
        if rid in result:
            return None, 'Duplicate record identity ' + rid + '.'
        result[rid] = record
    return result, ''


def changed_fields(before, after, fields):
    return before is None or any(before.get(field) != after.get(field) for field in fields)


def written_records(state):
    result = {}
    docs = state.get('google_docs_mock', {}).get('documents', {})
    if isinstance(docs, dict):
        for rid, record in docs.items():
            if isinstance(record, dict) and plain(record.get('content')):
                result['document:' + str(rid)] = plain(record['content'])
    items = state.get('google_drive_mock', {}).get('items', {})
    if isinstance(items, dict):
        for rid, record in items.items():
            if isinstance(record, dict) and not record.get('trashed') and record.get('type') != 'folder' and plain(record.get('content')):
                result['drive:' + str(rid)] = plain(record['content'])
    return result


def new_writing(initial, final, files):
    old = written_records(initial)
    old_texts = set(normalized(value) for value in old.values())
    result = []
    for rid, content in written_records(final).items():
        if normalized(content) not in old_texts and normalized(content) != normalized(old.get(rid, '')):
            result.append((rid, content))
    if isinstance(files, dict):
        for path, content in files.items():
            if isinstance(content, str) and plain(content) and normalized(content) not in old_texts:
                result.append(('file:' + str(path), plain(content)))
    return result


def mentions(text, value):
    return re.search(r'(?<![\w-])' + re.escape(value) + r'(?![\w-])', text, re.I) is not None


def campaign_lines(text, campaign_id):
    aliases = (campaign_id,) + CAMPAIGNS[campaign_id][1]
    lines = plain(text).splitlines()
    return [line for line in lines if any(mentions(line, alias) for alias in aliases)]


def money_values(text):
    matches = re.findall(r'(?<![\w.-])(?:USD\s*|\$\s*)?([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)(?![\w.-])', text, re.I)
    return [number(value) for value in matches]


def has_amount(text, amount):
    return any(same_amount(value, amount) for value in money_values(text))


def budget(record):
    daily = number(record.get('dailyBudget'))
    lifetime = number(record.get('lifetimeBudget'))
    if record.get('dailyBudget') is not None and daily is None:
        return None, 'Unreadable daily budget.'
    if record.get('lifetimeBudget') is not None and lifetime is None:
        return None, 'Unreadable lifetime budget.'
    daily = 0.0 if daily is None else daily
    lifetime = 0.0 if lifetime is None else lifetime
    if daily < 0 or lifetime < 0:
        return None, 'Negative advertising budget.'
    if daily > 0 and lifetime > 0:
        return None, 'Both daily and lifetime budgets are positive; effective spending is ambiguous.'
    return (daily, lifetime), ''


def september_days(record):
    start, end = date(2026, 9, 1), date(2026, 9, 30)
    if record.get('startDate'):
        parsed = absolute_date(record['startDate'])
        if parsed is None:
            return None
        start = max(start, parsed)
    if record.get('endDate'):
        parsed = absolute_date(record['endDate'])
        if parsed is None:
            return None
        end = min(end, parsed)
    return max(0, (end - start).days + 1)


def exposure(record):
    values, error = budget(record)
    if error:
        return None, error
    if record.get('status') == 'paused':
        return 0.0, ''
    days = september_days(record)
    if days is None:
        return None, 'Unreadable campaign schedule.'
    if days == 0:
        return None, 'An active campaign has no September delivery days.'
    daily, lifetime = values
    if daily > 0:
        return daily * days, ''
    if lifetime > 0:
        spent = number(record.get('amountSpent'))
        if spent is None or spent < 0:
            return None, 'A lifetime budget has no readable spending history.'
        remaining = lifetime - spent
        if remaining <= 0:
            return None, 'An active lifetime budget has no remaining funds.'
        # Conservatively reserve all remaining lifetime funds for September.
        return remaining, ''
    return None, 'An active campaign has no positive budget.'


def advertising_check(initial, final, files):
    old_campaigns, error = index_records(initial, 'meta_ads_mock', 'campaigns')
    if error:
        return False, error
    campaigns, error = index_records(final, 'meta_ads_mock', 'campaigns')
    if error:
        return False, error
    old_sets, error = index_records(initial, 'meta_ads_mock', 'adSets')
    if error:
        return False, error
    sets, error = index_records(final, 'meta_ads_mock', 'adSets')
    if error:
        return False, error
    old_ads, error = index_records(initial, 'meta_ads_mock', 'ads')
    if error:
        return False, error
    ads, error = index_records(final, 'meta_ads_mock', 'ads')
    if error:
        return False, error

    control_fields = ('status', 'dailyBudget', 'lifetimeBudget', 'budgetOptimization', 'startDate', 'endDate')
    for rid, old in old_campaigns.items():
        if rid not in CAMPAIGNS:
            if rid not in campaigns or changed_fields(old, campaigns[rid], control_fields):
                return False, 'Unrelated campaign ' + rid + ' lost its original spending or delivery settings.'
    for rid, old in old_sets.items():
        if old.get('campaignId') not in CAMPAIGNS:
            if rid not in sets or changed_fields(old, sets[rid], control_fields + ('campaignId',)):
                return False, 'Unrelated ad set ' + rid + ' lost its original spending or delivery settings.'
    for rid, old in old_ads.items():
        if old.get('campaignId') not in CAMPAIGNS:
            if rid not in ads or changed_fields(old, ads[rid], ('status', 'campaignId', 'adSetId', 'creativeId')):
                return False, 'Unrelated advertisement ' + rid + ' changed its delivery or creative assignment.'
    for rid, campaign in campaigns.items():
        if rid not in old_campaigns and campaign.get('status') == 'active':
            return False, 'Additional active campaign ' + rid + ' is outside the three-campaign allocation.'

    spending = {}
    settings = {}
    for cid, spec in CAMPAIGNS.items():
        sid = spec[0]
        if cid not in old_campaigns or sid not in old_sets:
            return False, 'Missing starting campaign or ad set: ' + cid + '/' + sid + '.'
        if cid not in campaigns or sid not in sets:
            return False, 'Required campaign or ad set was removed: ' + cid + '/' + sid + '.'
        campaign, ad_set = campaigns[cid], sets[sid]
        status = campaign.get('status')
        if status not in ('active', 'paused') or ad_set.get('status') != status:
            return False, cid + ' and ' + sid + ' must have matching active or paused statuses.'
        if ad_set.get('campaignId') != cid:
            return False, sid + ' is attached to the wrong campaign.'
        cb, error = budget(campaign)
        if error:
            return False, cid + ': ' + error
        sb, error = budget(ad_set)
        if error:
            return False, sid + ': ' + error
        if not all(same_amount(a, b) for a, b in zip(cb, sb)):
            return False, cid + ' and ' + sid + ' show inconsistent budgets.'
        for record in (campaign, ad_set):
            delivery = record.get('deliveryStatus')
            if status == 'paused' and delivery not in ('not_delivering', 'paused'):
                return False, record['id'] + ' is paused but reports delivery.'
            if status == 'active' and delivery not in ('active', 'scheduled'):
                return False, record['id'] + ' is active but cannot deliver.'
        children = [record for record in sets.values() if record.get('campaignId') == cid]
        if any(record.get('id') != sid and record.get('status') == 'active' for record in children):
            return False, cid + ' has an additional active ad set whose budget needs separate reconciliation.'
        if status == 'active':
            if september_days(campaign) != september_days(ad_set):
                return False, cid + ' and ' + sid + ' have different September delivery durations.'
            eligible = [ad for ad in ads.values() if ad.get('campaignId') == cid and ad.get('adSetId') == sid and ad.get('status') == 'active' and ad.get('reviewStatus') == 'approved']
            if not eligible:
                return False, cid + ' has no active approved advertisement in ' + sid + '.'
        amount, error = exposure(ad_set)
        if error:
            return False, sid + ': ' + error
        spending[cid] = amount
        settings[cid] = (status, cb)

    total = sum(spending.values())
    if total > 6000.011:
        return False, 'September planned media exposure is $' + str(round(total, 2)) + ', above the recorded $6,000 authority. Any increased authority requires separate authenticated review.'

    writings = new_writing(initial, final, files)
    if not writings:
        return False, 'No new written implementation evidence accompanies the advertising settings.'
    # These are value reconciliations. A judge must identify the selected decision,
    # distinguish it from alternatives, and assess approval and explanation.
    reconciled = []
    for source, text in writings:
        if not re.search(r'\bSeptember\b|\bSept\.?\b|2026-09', text, re.I):
            continue
        valid = True
        for cid in CAMPAIGNS:
            status, values = settings[cid]
            lines = campaign_lines(text, cid)
            status_pattern = r'\b(?:paused|pause|off|no paid delivery|not delivering)\b' if status == 'paused' else r'\b(?:active|continue|retain|running|enabled|maintain)\b'
            rows = [line for line in lines if re.search(status_pattern, line, re.I)]
            if not rows:
                valid = False
                break
            if status == 'active':
                daily, lifetime = values
                values_to_match = [spending[cid], daily if daily > 0 else lifetime]
                if not any(has_amount(line, amount) for line in rows for amount in values_to_match):
                    valid = False
                    break
        if valid:
            reconciled.append((source, text))
    if not reconciled:
        return False, 'No new September document reconciles all three campaign identities, statuses and spending amounts with the persisted settings.'
    reported_total = any(
        has_amount(line, total) and re.search(r'\b(?:total|combined|effective|media|advertising|acquisition)\b', line, re.I)
        for source, text in reconciled for line in text.splitlines()
    )
    if not reported_total:
        return False, 'The new implementation evidence does not report the reconciled September media exposure of $' + str(round(total, 2)) + '.'
    return True, 'All three campaign/ad-set pairs reconcile with new September written values; planned media exposure is $' + str(round(total, 2)) + '. Unrelated campaign controls are preserved. Approval, daily-budget risk wording and decision meaning require separate review.'


def obligation_content(company_id, text):
    text = normalized(text)
    request = re.search(r'\b(?:ask|request|collect|obtain|chase|follow up|follow-up|followup|remind|send|return|secure|confirm|check|review|clarify|route|refer|provide|await|pending|waiting)\b', text)
    if not request:
        return False
    if company_id == 'P001':
        return bool(re.search(r'\b(?:signed|signature|unsigned|sign)\b', text) and re.search(r'\bresale\b.*\bcertificate\b|\bcertificate\b.*\bresale\b', text))
    if company_id == 'P002':
        return bool(re.search(r'\bsales[ -]tax\s+licen[cs]e\b', text))
    if company_id == 'P003':
        return bool(re.search(r'\b(?:certificate|resale|documents|paperwork)\b', text) and re.search(r'\b(?:gareth|central credit|central review|credit team|credit review)\b', text))
    if company_id == 'P004':
        return bool(re.search(r'\bdecorat\w*\b', text) and re.search(r'\b(?:channels?|where.*sell|selling|sales routes?)\b', text) and re.search(r'\b(?:documents?|paperwork|certificate|licen[cs]e)\b', text))
    if company_id == 'P005':
        return bool(re.search(r'\b(?:refer|referral|route|send|provide|share|direct)\b', text) and re.search(r'\breseller\s+(?:resource|directory|referral|locator)|\b(?:local|qualified)\s+reseller\b', text))
    if company_id == 'P006':
        return bool(re.search(r'\b(?:legal|registered|trade|trading|business|entity)\s+name\b|\bDBA\b', text) and re.search(r'\b(?:certificate|application|paperwork|documents)\b', text))
    return False


def linked_note(note, ticket_id, company_id, contact_id, deals):
    kind, rid = note.get('associatedType'), note.get('associatedId')
    if (kind, rid) in (('ticket', ticket_id), ('company', company_id), ('contact', contact_id)):
        return True
    return kind == 'deal' and rid in deals and deals[rid].get('companyId') == company_id


def duplicate_relationship(initial_companies, final_companies, initial_contacts, final_contacts):
    base = initial_companies.get('P002')
    contact = initial_contacts.get('CT-P002')
    if base is None or contact is None:
        return 'Missing the original Moonrise company or contact.'
    if 'P002' not in final_companies or 'CT-P002' not in final_contacts:
        return 'Moonrise no longer retains its original company and contact identities.'
    for rid, record in final_companies.items():
        if rid in initial_companies:
            continue
        same_domain = bool(base.get('domain')) and normalized(record.get('domain')) == normalized(base.get('domain'))
        same_name = bool(base.get('name')) and normalized(record.get('name')) == normalized(base.get('name'))
        if same_domain or same_name:
            return 'New company ' + rid + ' duplicates the original Moonrise relationship P002.'
    for rid, record in final_contacts.items():
        if rid not in initial_contacts and contact.get('email') and normalized(record.get('email')) == normalized(contact.get('email')):
            return 'New contact ' + rid + ' duplicates Moonrise contact CT-P002.'
    return ''


def obligations_check(initial, final):
    old, current = {}, {}
    for collection in ('tickets', 'tasks', 'notes', 'companies', 'contacts', 'deals'):
        records, error = index_records(initial, 'hubspot_mock', collection)
        if error:
            return False, error
        old[collection] = records
        records, error = index_records(final, 'hubspot_mock', collection)
        if error:
            return False, error
        current[collection] = records
    error = duplicate_relationship(old['companies'], current['companies'], old['contacts'], current['contacts'])
    if error:
        return False, error

    owners = ('Imani Brooks', 'Owen Delgado', 'Mara Ellison', 'Gareth Pike', 'Priya Nair')
    owner_ids = ('person-imani', 'person-owen', 'person-mara', 'person-gareth', 'person-priya')
    valid_owners = set(normalized(value) for value in owners + owner_ids)
    valid_owners.update(name.lower().replace(' ', '.') + '@cedarlineapparel.com' for name in owners)
    completed = []
    for tid, company_id, contact_id, intake_id, name in OBLIGATIONS:
        if tid not in old['tickets']:
            return False, 'Missing starting obligation ' + tid + ' for ' + name + '.'
        ticket = current['tickets'].get(tid)
        if ticket is None:
            return False, name + ' obligation ' + tid + ' was removed.'
        if ticket.get('companyId') != company_id or ticket.get('contactId') != contact_id:
            return False, tid + ' no longer belongs to its original customer.'
        company, contact = current['companies'].get(company_id), current['contacts'].get(contact_id)
        if company is None or contact is None or contact.get('companyId') != company_id:
            return False, name + ' lacks its original linked company/contact records.'
        if normalized(ticket.get('owner')) not in valid_owners:
            return False, tid + ' has no recognized desk or credit owner.'
        if company_id != 'P005' and ticket.get('status') not in ('new', 'waiting_on_contact', 'waiting_on_us', 'in_progress'):
            return False, name + ' application is no longer open despite the recorded unresolved document requirement.'
        if company_id != 'P005' and ticket.get('closeDate') not in (None, ''):
            return False, name + ' unresolved application has a closure date.'
        for record, before in ((company, old['companies'].get(company_id)), (contact, old['contacts'].get(contact_id))):
            if before is None:
                return False, 'Missing starting identity for ' + name + '.'
            if record.get('lifecycleStage') != before.get('lifecycleStage') and record.get('lifecycleStage') in ('customer', 'evangelist'):
                return False, name + ' was promoted to a buying customer without authenticated central documentary evidence.'

        candidates = []
        for task in current['tasks'].values():
            if task.get('companyId') != company_id or task.get('contactId') not in (None, '', contact_id):
                continue
            if normalized(task.get('owner')) not in valid_owners:
                continue
            if task.get('type') not in ('call', 'email', 'to_do') or task.get('priority') not in ('low', 'medium', 'high'):
                continue
            due = absolute_date(task.get('dueDate'))
            if due is None or due < date(2026, 9, 1):
                continue
            if task.get('status') not in ('not_started', 'in_progress'):
                continue
            if task.get('completedDate') not in (None, ''):
                continue
            title, notes = plain(task.get('title')), plain(task.get('notes'))
            if not title or not notes:
                continue
            before = old['tasks'].get(task['id'])
            if not changed_fields(before, task, ('title', 'notes', 'dueDate', 'owner', 'status', 'companyId', 'contactId')):
                continue
            deal_id = task.get('dealId')
            if deal_id and (deal_id not in current['deals'] or current['deals'][deal_id].get('companyId') != company_id):
                continue
            candidates.append(task)
        if not candidates:
            return False, name + ' lacks a new or substantively updated open follow-up task with a recognized owner, absolute future date and its specific outstanding action.'

        notes = []
        for note in current['notes'].values():
            if not linked_note(note, tid, company_id, contact_id, current['deals']):
                continue
            body = plain(note.get('body'))
            if not body:
                continue
            if not changed_fields(old['notes'].get(note['id']), note, ('body', 'associatedType', 'associatedId')):
                continue
            if normalized(note.get('createdBy')) not in valid_owners:
                continue
            if not any(mentions(body, reference) for reference in (tid, intake_id, company_id, name)):
                continue
            if company_id == 'P002' and not (mentions(body, 'APP-606') and mentions(body, 'INQ-612')):
                continue
            notes.append(note)
        if not notes:
            return False, name + ' lacks a new associated note recording its specific obligation and source identity' + (' and keeping INQ-612 with APP-606.' if company_id == 'P002' else '.')
        completed.append(name)

    return True, 'New dated tasks and associated obligation notes cover ' + ', '.join(completed) + '. Original customer links and unresolved application states are retained; Moonrise has no duplicate company or email identity. Action accuracy and reconsideration conditions require separate review.'


def verify(initial, final, files, events):
    # Runtime events have no supplied schema. Never infer trusted authorship from
    # editable owner, sender, createdBy, currentUser, or timestamp fields.
    # This verifier checks observable records; event attribution and the sequence
    # of approval versus implementation require a documented event adapter.
    if not isinstance(initial, dict) or not isinstance(final, dict):
        raise TypeError('initial and final must map app IDs to native states')
    if initial == final and not files:
        return {
            '3': {'passed': False, 'reason': 'No new business settings or written implementation evidence.'},
            '4': {'passed': False, 'reason': 'No new customer follow-up tasks or associated obligation notes.'},
        }
    passed3, reason3 = advertising_check(initial, final, files)
    passed4, reason4 = obligations_check(initial, final)
    return {
        '3': {'passed': passed3, 'reason': reason3},
        '4': {'passed': passed4, 'reason': reason4},
    }
