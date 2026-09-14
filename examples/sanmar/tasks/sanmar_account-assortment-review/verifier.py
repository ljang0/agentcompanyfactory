import re
from decimal import Decimal
from datetime import datetime


def text(value):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', value, flags=re.I | re.S)
    value = re.sub(r'<!--.*?-->', '', value, flags=re.S)
    value = re.sub(r'</(?:p|div|tr|li|h[1-6])\s*>|<br\s*/?>', '\n', value, flags=re.I)
    value = re.sub(r'<[^>]+>', ' ', value)
    for encoded, decoded in [('&#39;', "'"), ('&#x27;', "'"), ('&quot;', '"'), ('&nbsp;', ' '), ('&amp;', '&')]:
        value = value.replace(encoded, decoded)
    return value.replace('\u2019', "'").strip()


def normalized(value):
    return re.sub(r'\s+', ' ', text(value)).strip().lower()


def present(value):
    return bool(normalized(value))


def has(value, pattern):
    return re.search(pattern, text(value), re.I) is not None


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    value = str(value).strip().replace(',', '')
    value = re.sub(r'^(?:USD\s*|\$\s*)', '', value, flags=re.I)
    if not re.fullmatch(r'[+-]?\d+(?:\.\d+)?', value):
        return None
    return Decimal(value)


def numeric_fact(value, expected):
    # Exact numeric tokens, rather than substrings such as 630 inside 1630.
    tokens = re.findall(r'(?<![\w.-])[+-]?\d+(?:,\d{3})*(?:\.\d+)?(?!\w|\.\d)', text(value))
    return any(number(token) == Decimal(str(expected)) for token in tokens)


def quantity_fact(value, quantity):
    if numeric_fact(value, quantity):
        return True
    words = {6: 'six', 48: 'forty eight', 120: 'one hundred twenty'}
    phrase = words.get(quantity)
    if not phrase:
        return False
    clean = normalized(value).replace('-', ' ')
    if quantity == 120:
        return has(clean, r'\bone hundred (?:and )?twenty\b')
    return has(clean, r'\b' + phrase + r'\b')


def records(state, app, collection):
    app_state = state.get(app, {})
    if not isinstance(app_state, dict):
        return []
    data = app_state.get(collection, [])
    if isinstance(data, dict):
        return [record for record in data.values() if isinstance(record, dict)]
    if isinstance(data, list):
        return [record for record in data if isinstance(record, dict)]
    return []


def matches_id(rows, identity, field='id'):
    return [row for row in rows if row.get(field) == identity]


def one(rows, identity, field='id'):
    found = matches_id(rows, identity, field)
    return found[0] if len(found) == 1 else None


def cell_value(cell):
    if not isinstance(cell, dict):
        return cell
    raw = cell.get('value', cell.get('formula', ''))
    formula = cell.get('formula', '')
    if isinstance(raw, str) and raw.startswith('='):
        return cell.get('computed', '')
    if isinstance(formula, str) and formula.startswith('='):
        return cell.get('computed', '')
    return raw


def header_name(value):
    return normalized(str(value)).replace('_', ' ')


def ledger_header(values):
    keys = set(values)
    return ('order' in keys and 'product' in keys and 'quantity' in keys) or ('return' in keys and 'posted amount' in keys)


def sheet_rows(sheet):
    data = sheet.get('data', {})
    if not isinstance(data, dict):
        return []
    grouped = {}
    for address, cell in data.items():
        match = re.fullmatch(r'([A-Z]+)([1-9]\d*)', str(address))
        if match:
            column, row = match.group(1), int(match.group(2))
            if row not in grouped:
                grouped[row] = {}
            grouped[row][column] = cell_value(cell)
    headers = None
    result = []
    for row_number in sorted(grouped):
        row = grouped[row_number]
        candidate = {column: header_name(value) for column, value in row.items()}
        if ledger_header(candidate.values()):
            headers = candidate
        elif headers:
            result.append({key: row.get(column, '') for column, key in headers.items() if key})
    return result


def csv_fields(line):
    fields = []
    current = ''
    quoted = False
    index = 0
    while index < len(line):
        char = line[index]
        if char == '"':
            if quoted and index + 1 < len(line) and line[index + 1] == '"':
                current += '"'
                index += 1
            else:
                quoted = not quoted
        elif char == ',' and not quoted:
            fields.append(current.strip())
            current = ''
        else:
            current += char
        index += 1
    if quoted:
        return []
    fields.append(current.strip())
    return fields


def exported_rows(content):
    # Reads the published ledger's CSV sections, also allowing Markdown tables.
    headers = None
    result = []
    for line in text(content).splitlines():
        line = line.strip()
        if line.startswith('#'):
            headers = None
            continue
        if not line:
            continue
        if '|' in line:
            values = [part.strip() for part in line.strip('|').split('|')]
        else:
            values = csv_fields(line)
        candidate = [header_name(value) for value in values]
        if ledger_header(candidate):
            headers = candidate
        elif headers and len(values) == len(headers):
            result.append(dict(zip(headers, values)))
    return result


def drive_ledger(state):
    item = one(records(state, 'google_drive_mock', 'items'), 'WB-COMMERCIAL')
    if item is None or item.get('trashed') is True or not present(item.get('content')):
        return None
    return exported_rows(item['content'])


def native_ledger(state):
    result = []
    for sheet in records(state, 'google_sheets_mock', 'sheets'):
        result += sheet_rows(sheet)
    return result


def order_errors(rows, label, identity, expected):
    found = [row for row in rows if normalized(str(row.get('order', ''))) == identity.lower() and 'product' in row]
    if len(found) != 1:
        return [label + ': expected one accepted-order line for ' + identity + ', found ' + str(len(found)) + '.']
    row = found[0]
    errors = []
    for field, expected_value in expected.items():
        actual = row.get(field)
        if field in ('quantity', 'unit price', 'gross'):
            valid = number(actual) == Decimal(str(expected_value))
        else:
            valid = normalized(str(actual)) == normalized(str(expected_value))
        if not valid:
            errors.append(label + ': ' + identity + ' has incorrect or missing ' + field + ' (observed ' + str(actual) + ').')
    quantity = number(row.get('quantity'))
    price = number(row.get('unit price'))
    gross = number(row.get('gross'))
    if quantity is not None and price is not None and gross is not None and quantity * price != gross:
        errors.append(label + ': ' + identity + ' quantity times unit price does not equal its merchandise amount.')
    processing = normalized(str(row.get('processing', '')))
    if identity == 'ORD-260818':
        # No external cancellation or reservation is available in this task.
        if not ('accepted' in processing and has(processing, r'await|pending|unallocated|not allocated|allocation (?:open|unresolved)')):
            errors.append(label + ': Northfork must remain accepted with allocation unresolved.')
        if has(processing, r'cancelled|canceled|shipped|fulfilled|reserved'):
            errors.append(label + ': Northfork has an unsupported processing change.')
    elif processing != 'shipped':
        errors.append(label + ': Mesa\'s shipped order is no longer recorded as shipped.')
    return errors


def credit_errors(rows, label, required):
    relevant = [row for row in rows if 'return' in row and (str(row.get('return', '')).strip() == 'RET-704' or str(row.get('order', '')).strip() == 'ORD-260811')]
    claim = [row for row in relevant if str(row.get('return', '')).strip() == 'RET-704']
    errors = []
    if required and len(claim) != 1:
        errors.append(label + ': Mesa return RET-704 is missing or duplicated.')
    for row in relevant:
        if str(row.get('return', '')).strip() != 'RET-704':
            errors.append(label + ': an additional unsupported return or credit was entered for Mesa ORD-260811.')
            continue
        for field, expected in [('order', 'ORD-260811'), ('account', 'A005'), ('requested', '2026-08-19')]:
            if normalized(str(row.get(field, ''))) != expected.lower():
                errors.append(label + ': RET-704 has incorrect ' + field + '.')
        for field, expected in [('units', 6), ('posted amount', 0), ('claimed amount', 79.2)]:
            if number(row.get(field)) != Decimal(str(expected)):
                errors.append(label + ': RET-704 must retain ' + field + ' of ' + str(expected) + '.')
        for field in ('received', 'credit', 'posted'):
            if present(str(row.get(field, '') or '')):
                errors.append(label + ': RET-704 has unsupported ' + field + ' evidence.')
        for field in ('authorized', 'authorised', 'authorization', 'authorisation', 'consent'):
            if field in row and normalized(str(row[field] or '')) not in ('', 'none', 'no', 'pending', 'not authorized', 'not authorised', 'not received', 'awaiting authorization', 'awaiting authorisation'):
                errors.append(label + ': RET-704 records unsupported return authorization.')
    return errors


def pending_stage(content, stage):
    # Checks explicit unresolved stage reporting, not whether someone has authority.
    clauses = re.split(r'[;\n.!?]', text(content))
    unresolved = r'\b(?:no|not|none|pending|await\w*|unapproved|unauthorized|unauthorised|unreceived|unposted|unconfirmed|unresolved|outstanding|unknown|zero)\b|\b0\b|request(?:ed)? only'
    return any(has(clause, stage) and has(clause, unresolved) for clause in clauses)


def action_present(content, kind):
    action = has(content, r'follow[ -]?up|check|chase|request|review|confirm|obtain|ask|seek|contact|send|route|escalat|pursue')
    central = has(content, r'\bLucia\b|central (?:operations|order|services)|order services')
    subject = has(content, r'cancel|consent') if kind == 'northfork' else has(content, r'photo|picture|label|return|disput|invoice')
    return action and central and subject


def northfork_facts(content):
    return (has(content, r'\bORD-260818\b') and has(content, r'\bCB080\b')
            and has(content, r'\bKhaki\b') and has(content, r'\bAdjustable\b')
            and quantity_fact(content, 120)
            and (numeric_fact(content, 5.25) or numeric_fact(content, 630))
            and action_present(content, 'northfork')
            and pending_stage(content, r'consent|cancellation|cancel|authorization|authorisation'))


def mesa_facts(content):
    return (has(content, r'\bORD-260811\b') and has(content, r'\bINV-260811\b')
            and has(content, r'\bCB210\b') and has(content, r'\bNavy\b') and has(content, r'\bXL\b')
            and quantity_fact(content, 6) and numeric_fact(content, 79.2) and numeric_fact(content, 633.6)
            and has(content, r'disput|claim|affected')
            and has(content, r'unpaid|payment (?:held|withheld|on hold)|hold\w* (?:the )?payment|held (?:the )?(?:invoice )?payment')
            and action_present(content, 'mesa')
            and pending_stage(content, r'authoriz|authoris|\bRMA\b')
            and pending_stage(content, r'receiv|receipt')
            and pending_stage(content, r'credit|post'))


def case_notes(initial, final, ticket_id, company_id, contact_id, order_id):
    result = []
    before_tickets = records(initial, 'hubspot_mock', 'tickets')
    after_tickets = records(final, 'hubspot_mock', 'tickets')
    before = one(before_tickets, ticket_id)
    after = one(after_tickets, ticket_id)
    if after and before and normalized(after.get('description')) != normalized(before.get('description')):
        result.append((ticket_id, text(after.get('description'))))
    before_notes = records(initial, 'hubspot_mock', 'notes')
    for note in records(final, 'hubspot_mock', 'notes'):
        association = (note.get('associatedType'), note.get('associatedId'))
        if association not in [('ticket', ticket_id), ('company', company_id), ('contact', contact_id)]:
            continue
        old = one(before_notes, note.get('id'))
        if present(note.get('body')) and (old is None or normalized(old.get('body')) != normalized(note.get('body'))):
            result.append((str(note.get('id', '')), text(note.get('body'))))
    # Service memoranda can also be kept in native documents. No new document ID is prescribed.
    for app, collection in [('google_docs_mock', 'documents'), ('google_drive_mock', 'items')]:
        old_records = records(initial, app, collection)
        for document in records(final, app, collection):
            if document.get('trashed') is True or document.get('type') == 'folder':
                continue
            content = text(document.get('content'))
            if not has(content, r'\b' + re.escape(order_id) + r'\b'):
                continue
            old = one(old_records, document.get('id'))
            if old is None or normalized(old.get('content')) != normalized(content):
                # A native case identifier ties this memo to the service record.
                if has(content, r'\b' + re.escape(ticket_id) + r'\b'):
                    result.append((str(document.get('id', '')), content))
    return result


def valid_due(value):
    if not isinstance(value, str) or not re.match(r'^\d{4}-\d{2}-\d{2}(?:T|$)', value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return False
    return parsed.date().isoformat() >= '2026-09-01'


def followups(initial, final, company_id, contact_id, ticket_id, order_id, kind):
    result = []
    previous = records(initial, 'hubspot_mock', 'tasks')
    owners = ['Imani Brooks', 'Owen Delgado', 'Mara Ellison', 'Priya Nair', 'Lucia Moreno']
    owner_ids = ['person-imani', 'person-owen', 'person-mara', 'person-priya', 'person-lucia']
    for task in records(final, 'hubspot_mock', 'tasks'):
        if task.get('companyId') != company_id:
            continue
        if task.get('contactId') not in (None, '', contact_id):
            continue
        if task.get('status') not in ('not_started', 'in_progress') or task.get('completedDate') not in (None, ''):
            continue
        owner = task.get('owner')
        if owner not in owners + owner_ids or not valid_due(task.get('dueDate')):
            continue
        content = text(task.get('title')) + '\n' + text(task.get('notes'))
        if not present(task.get('title')) or not present(task.get('notes')):
            continue
        if not (has(content, r'\b' + re.escape(ticket_id) + r'\b') or has(content, r'\b' + re.escape(order_id) + r'\b')):
            continue
        if not action_present(content, kind):
            continue
        old = one(previous, task.get('id'))
        substantive = ('title', 'notes', 'owner', 'dueDate', 'status', 'companyId', 'contactId')
        if old is None or any(task.get(field) != old.get(field) for field in substantive):
            result.append(task)
    return result


def channel_messages(state, channel):
    slack = state.get('slack_mock', {})
    messages = slack.get('messages', {}) if isinstance(slack, dict) else {}
    rows = messages.get(channel, []) if isinstance(messages, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def reply_ids(thread):
    result = []
    for reply in thread.get('replies', []):
        if isinstance(reply, str):
            result.append(reply)
        elif isinstance(reply, dict):
            identity = reply.get('messageId')
            if isinstance(identity, str):
                result.append(identity)
    return result


def verify(initial, final, files, events):
    # Only criterion 3 has method=state. Attribution and qualitative assessment
    # are not inferred from editable senderId, owner, or createdBy fields.
    errors = []
    if initial == final:
        return {'3': {'passed': False, 'reason': 'No new service-record or original-thread work is observable in the apps; files or event counts alone do not establish that work.'}}

    before_tickets = records(initial, 'hubspot_mock', 'tickets')
    after_tickets = records(final, 'hubspot_mock', 'tickets')
    for identity, company, contact, expected_open in [
        ('SVC-104', 'A005', 'CT-A005', True),
        ('SVC-105', 'A006', 'CT-A006', True),
        ('SVC-106', 'A006', 'CT-A006', False)
    ]:
        before = one(before_tickets, identity)
        after = one(after_tickets, identity)
        if before is None or after is None:
            errors.append(identity + ': the required starting case is missing or duplicated.')
            continue
        if after.get('companyId') != company or after.get('contactId') != contact:
            errors.append(identity + ': the case is attached to the wrong customer.')
        if expected_open:
            if after.get('status') not in ('waiting_on_us', 'in_progress') or after.get('closeDate') not in (None, ''):
                errors.append(identity + ': the unresolved central-services follow-up must remain open with the desk.')
            if not present(after.get('owner')):
                errors.append(identity + ': no case owner is assigned.')
        elif after.get('status') != 'closed' or not present(after.get('closeDate')) or not has(after.get('description'), r'\bSVC-105\b'):
            errors.append('SVC-106 must remain the closed duplicate linked to SVC-105.')

    expected_orders = {
        'ORD-260818': {'account': 'A006', 'accepted': '2026-08-18', 'invoice': '', 'product': 'CB080', 'color': 'Khaki', 'size': 'Adjustable', 'quantity': 120, 'unit price': 5.25, 'gross': 630},
        'ORD-260811': {'account': 'A005', 'accepted': '2026-08-11', 'invoice': 'INV-260811', 'product': 'CB210', 'color': 'Navy', 'size': 'XL', 'quantity': 48, 'unit price': 13.2, 'gross': 633.6}
    }
    before_drive = drive_ledger(initial)
    after_drive = drive_ledger(final)
    if before_drive is None:
        errors.append('The starting commercial ledger is unavailable; accepted commitments cannot be verified.')
    if after_drive is None:
        errors.append('The final commercial ledger is missing, trashed, or unreadable.')
    else:
        for identity, expected in expected_orders.items():
            errors += order_errors(after_drive, 'Commercial ledger', identity, expected)
        errors += credit_errors(after_drive, 'Commercial ledger', True)

    before_sheet = native_ledger(initial)
    after_sheet = native_ledger(final)
    for identity, expected in expected_orders.items():
        existed = any(str(row.get('order', '')).strip() == identity and 'product' in row for row in before_sheet)
        exists_now = any(str(row.get('order', '')).strip() == identity and 'product' in row for row in after_sheet)
        if existed or exists_now:
            errors += order_errors(after_sheet, 'Spreadsheet', identity, expected)
    credit_existed = any(str(row.get('return', '')).strip() == 'RET-704' for row in before_sheet)
    errors += credit_errors(after_sheet, 'Spreadsheet', credit_existed)

    north_notes = case_notes(initial, final, 'SVC-105', 'A006', 'CT-A006', 'ORD-260818')
    mesa_notes = case_notes(initial, final, 'SVC-104', 'A005', 'CT-A005', 'ORD-260811')
    valid_north_notes = [(identity, body) for identity, body in north_notes if northfork_facts(body)]
    valid_mesa_notes = [(identity, body) for identity, body in mesa_notes if mesa_facts(body)]
    if not valid_north_notes:
        errors.append('No new Northfork case note or service memorandum identifies ORD-260818, 120 Khaki Adjustable CB080 caps, its $5.25 unit price or $630 amount, and a central-services cancellation follow-up with consent unresolved.')
    if not valid_mesa_notes:
        errors.append('No new Mesa case note or service memorandum identifies ORD/INV-260811, six Navy XL CB210 units disputed for $79.20 against the $633.60 unpaid invoice, a central-services follow-up, and unresolved authorization, receipt, and posted-credit stages.')

    north_tasks = followups(initial, final, 'A006', 'CT-A006', 'SVC-105', 'ORD-260818', 'northfork')
    mesa_tasks = followups(initial, final, 'A005', 'CT-A005', 'SVC-104', 'ORD-260811', 'mesa')
    if not north_tasks:
        errors.append('Northfork lacks a new or substantively updated open task linked to its original case/order, with an assigned owner, an absolute follow-up date, and a specific central-services cancellation action.')
    if not mesa_tasks:
        errors.append('Mesa lacks a new or substantively updated open task linked to its case/order, with an assigned owner, an absolute follow-up date, and a specific central-services dispute action.')

    thread = one(records(final, 'slack_mock', 'threads'), 'TH-H260826', 'threadId')
    initial_thread = one(records(initial, 'slack_mock', 'threads'), 'TH-H260826', 'threadId')
    messages = channel_messages(final, 'CH-SERVICE')
    old_messages = channel_messages(initial, 'CH-SERVICE')
    if thread is None or initial_thread is None:
        errors.append('Northfork\'s original TH-H260826 thread is missing or duplicated.')
    elif thread.get('parentMessageId') != 'H260826' or thread.get('channelId') != 'CH-SERVICE':
        errors.append('The Northfork follow-up no longer belongs to the original order-services conversation.')
    else:
        ids = reply_ids(thread)
        if not all(identity in ids for identity in reply_ids(initial_thread)):
            errors.append('The original Northfork thread history was removed.')
        if one(messages, 'H260826', 'messageId') is None:
            errors.append('The original Northfork thread parent H260826 is missing or duplicated.')
        old_ids = set(message.get('messageId') for message in old_messages)
        valid_reply = False
        links = [identity for identity, body in valid_north_notes] + [str(task.get('id', '')) for task in north_tasks]
        for message in messages:
            identity = message.get('messageId')
            if not isinstance(identity, str) or identity in old_ids or identity not in ids or message.get('threadId') != 'TH-H260826':
                continue
            if len(matches_id(messages, identity, 'messageId')) != 1:
                continue
            body = text(message.get('content'))
            refers_original = has(body, r'\b(?:ORD-260818|SVC-105)\b')
            actionable = action_present(body, 'northfork')
            unresolved = pending_stage(body, r'consent|cancel|authorization|authorisation')
            linked_detail = any(link and link in body for link in links)
            if refers_original and actionable and unresolved and (northfork_facts(body) or linked_detail):
                valid_reply = True
        if not valid_reply:
            errors.append('No new substantive reply advances Northfork in TH-H260826: identify the original case/order, specify the central-services cancellation follow-up, retain unresolved consent, and include the accepted cap details or link to the new detailed note/task.')

    if errors:
        return {'3': {'passed': False, 'reason': ' '.join(errors)}}
    return {'3': {'passed': True, 'reason': 'New service notes and assigned, dated open tasks cover Mesa and Northfork. A new reply advances Northfork in its original thread. The accepted 120-cap order remains $630 pending allocation; Mesa retains its $633.60 shipped invoice and separate six-unit $79.20 claim with no recorded receipt or posted credit. SVC-104 and SVC-105 remain open; SVC-106 remains the closed duplicate.'}}
