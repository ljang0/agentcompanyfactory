import re
import math


def text(value):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'<[^>]*>', ' ', value)
    for entity, replacement in [('&nbsp;', ' '), ('&amp;', '&'), ('&#39;', "'"), ('&#x27;', "'"), ('&quot;', '"')]:
        value = value.replace(entity, replacement)
    value = value.replace('–', '-').replace('—', '-').replace('‑', '-')
    return re.sub(r'\s+', ' ', value).strip().lower()


def records(state, app, collection):
    native = state.get(app)
    if not isinstance(native, dict):
        return []
    values = native.get(collection)
    if not isinstance(values, list):
        return []
    return [row for row in values if isinstance(row, dict)]


def unique_record(rows, identity):
    matches = [row for row in rows if row.get('id') == identity]
    if len(matches) != 1:
        return None
    return matches[0]


def sentences(value):
    if not isinstance(value, str):
        return []
    value = re.sub(r'</(?:p|li|h[1-6]|tr)>|<br\s*/?>', '\n', value)
    return [text(part) for part in re.split(r'\n+|(?<=[.!?])\s+', value) if text(part)]


def new_issue_text(before, after, old_comments, new_comments):
    # Compare business text, not timestamps, labels, or interface changes.
    old_parts = sentences(before.get('description'))
    for comment in old_comments:
        old_parts.extend(sentences(comment.get('content')))
    old_text = ' '.join(old_parts)
    candidates = []
    if text(before.get('description')) != text(after.get('description')):
        candidates.extend(sentences(after.get('description')))
    old_ids = [comment.get('id') for comment in old_comments]
    for comment in new_comments:
        if comment.get('id') and comment.get('id') not in old_ids:
            candidates.extend(sentences(comment.get('content')))
    additions = []
    for part in candidates:
        # Appending a generic sentence to an unchanged seed paragraph does not
        # turn the old paragraph into new requirements.
        for old_part in sorted(old_parts, key=len, reverse=True):
            if old_part and old_part in part:
                part = part.replace(old_part, ' ')
        part = text(part)
        if part and part not in old_text:
            additions.append(part)
    return additions


def matches(value, pattern):
    return re.search(pattern, value) is not None


ACTION = r'\b(check\w*|test\w*|review\w*|verif\w*|validat\w*|compar\w*|reproduc\w*|confirm\w*|record\w*|document\w*|defin\w*|specif\w*|agre\w*|resolv\w*|investigat\w*|assess\w*|establish\w*|determin\w*|preserv\w*|retain\w*|demonstrat\w*|captur\w*|rerun\w*|re-run\w*|rehears\w*|inspect\w*|need\w*|requir\w*|must|outstanding|pending|open|before|await\w*|block\w*)\b'


def planned(parts, patterns):
    # Requirements can span adjacent sentences, such as a case followed by its
    # expected result. This measures concrete coverage, not prose correctness.
    for index in range(len(parts)):
        window = ' '.join(parts[max(0, index - 1):min(len(parts), index + 2)])
        if matches(window, ACTION) and all(matches(window, pattern) for pattern in patterns):
            return True
    return False


def september_day(day):
    number = str(day)
    padded = '0' + number if day < 10 else number
    return r'(?:2026-09-' + padded + r'\b|\bsep(?:tember|t)?\.?\s*' + number + r'(?:st|nd|rd|th)?\b|\b' + number + r'(?:st|nd|rd|th)?\s+sep(?:tember|t)?\b)'


def verify(initial, final, files, events):
    failures = []
    old_issues = records(initial, 'jira_mock', 'issues')
    new_issues = records(final, 'jira_mock', 'issues')
    old_comments = records(initial, 'jira_mock', 'comments')
    new_comments = records(final, 'jira_mock', 'comments')
    known_people = [row.get('id') for row in records(initial, 'jira_mock', 'users') if row.get('id')]
    valid_statuses = ['To Do', 'In Progress', 'In Review', 'Done']
    priorities = ['Highest', 'High', 'Medium', 'Low', 'Lowest']
    issue_ids = ['RD-202', 'RD-203', 'RD-206', 'RD-211']
    additions = {}

    if not old_issues or not new_issues:
        return {'3': {'passed': False, 'reason': 'Initial or final Jira issue records are missing or unreadable.'}}

    for identity in issue_ids:
        before = unique_record(old_issues, identity)
        after = unique_record(new_issues, identity)
        if before is None or after is None:
            failures.append(identity + ': missing or duplicate starting/final issue identity')
            continue
        if after.get('key') != identity or after.get('projectId') != 'project-relaydesk':
            failures.append(identity + ': issue key or Relaydesk project identity is incorrect')
        if not text(after.get('summary')):
            failures.append(identity + ': issue summary is empty')
        if after.get('assigneeId') not in known_people:
            failures.append(identity + ': no recognized responsible person is assigned')
        if after.get('priority') not in priorities:
            failures.append(identity + ': priority is missing or invalid')
        if after.get('status') not in valid_statuses:
            failures.append(identity + ': progress status is missing or invalid')
        if identity != 'RD-211' and after.get('status') == 'Done':
            failures.append(identity + ': unfinished engineering is marked Done')
        points = after.get('storyPoints')
        if points is not None:
            if isinstance(points, bool) or not isinstance(points, (int, float)):
                failures.append(identity + ': estimate is not a number or an explicit unestimated value')
            elif not math.isfinite(points) or points < 0:
                failures.append(identity + ': estimate is negative or non-finite')
        prior = [row for row in old_comments if row.get('issueId') == identity]
        current = [row for row in new_comments if row.get('issueId') == identity]
        parts = new_issue_text(before, after, prior, current)
        additions[identity] = parts
        if not parts:
            failures.append(identity + ': no new follow-up requirements in its description or new comments')

    # Historical Jira review evidence cannot be rewritten to manufacture progress.
    for before in old_comments:
        if before.get('issueId') not in issue_ids + ['RD-212']:
            continue
        after = unique_record(new_comments, before.get('id'))
        if after is None:
            failures.append(str(before.get('issueId')) + ': an existing review comment was removed or duplicated')
        elif (after.get('issueId') != before.get('issueId') or
              after.get('userId') != before.get('userId') or
              text(after.get('content')) != text(before.get('content'))):
            failures.append(str(before.get('issueId')) + ': historical review content or attribution was altered')

    required = {
        'RD-202': [
            ('keyboard return and focus', [r'\b(keyboard|tab|focus)\b', r'\b(return\w*|back|restor\w*)\b', r'\bfocus\b']),
            ('empty-result recovery', [r'\b(empty|zero|no matches|no results)\b', r'\b(all requests|clear\w*|reset\w*|recover\w*|next action|continue\w*)\b']),
            ('saved-filter preservation', [r'\b(filter\w*|saved.view|query)\b', r'\b(retain\w*|preserv\w*|same|restor\w*|persist\w*)\b']),
            ('checks tied to the candidate version', [r'(?:\brevision\b|\bcommit\b|\bbranch\b|b461efa|keep-return-filters)'])
        ],
        'RD-203': [
            ('imported undecided requests', [r'\bimport\w*\b', r'\b(undecided|neither|false|pending|no decision)\b']),
            ('waiting versus unassigned requests', [r'\b(waiting|assigned)\b', r'\b(unassigned|no reviewer|without (?:a )?reviewer|missing reviewer|null(?:able)? reviewer)\b']),
            ('conflicting decision flags', [r'\b(conflict\w*|both|contradict\w*)\b', r'\b(flag\w*|true|approv\w*|reject\w*|declin\w*)\b']),
            ('old and new consumer compatibility', [r'\b(consumer\w*|client\w*|browser)\b', r'\b(compatib\w*|coexist\w*|old and new|legacy|existing fields|old fields)\b']),
            ('derived or stored state decision', [r'\b(deriv\w*|projection|serializ\w*|serialis\w*)\b', r'\b(stor\w*|persist\w*)\b']),
            ('transition and recovery', [r'\b(migrat\w*|transition\w*|rollout|roll.out)\b', r'\b(recover\w*|rollback|roll.back|revert\w*|fallback|fall.back)\b'])
        ],
        'RD-206': [
            ('index migration and operational review', [r'\b(index|migration)\b', r'\b(concurren\w*|lock\w*|operational|production|load|transaction\w*)\b']),
            ('migration failure recovery', [r'\b(migrat\w*|index|build)\b', r'\b(rollback|roll.back|recover\w*|fail\w*|invalid|interrupt\w*|revert\w*)\b']),
            ('attachment removal and count consistency', [r'\b(attachment\w*|file\w*)\b', r'\b(delet\w*|remov\w*)\b', r'\b(count\w*|total\w*)\b']),
            ('checks tied to the candidate version', [r'(?:\brevision\b|\bcommit\b|\bbranch\b|64a2185|attachment-request-index)'])
        ]
    }
    for identity, checks in required.items():
        parts = additions.get(identity, [])
        for description, patterns in checks:
            if not planned(parts, patterns):
                failures.append(identity + ': new work does not specify ' + description)

    # Capacity reconciliation can be completed, but the engineering and maintenance
    # work it schedules remains open. Existing story points are not hours.
    capacity = ' '.join(additions.get('RD-211', []))
    capacity_checks = [
        ('June leave on September 4', [r'\bjune\b', september_day(4), r'\b(leave|away|absen\w*|unavailable|off)\b']),
        ('Imani two-hour panel on September 3', [r'\bimani\b', september_day(3), r'\b(?:two|2)(?:[ -]+hours?|h\b)', r'\b(panel|hiring|interview\w*)\b']),
        ('September 7 closure', [september_day(7), r'\b(clos\w*|holiday|labor day|labour day|non.working)\b']),
        ('preserved support allowance', [r'\bsupport\b', r'\b(reserv\w*|retain\w*|preserv\w*|protect\w*|allowance|budget\w*|allocat\w*|cover\w*|set aside)\b']),
        ('dependency maintenance and September 18 deadline', [r'\b(rd-212|background.job|dependency)\b', september_day(18), r'\b(reserv\w*|retain\w*|preserv\w*|protect\w*|allow\w*|priorit\w*|allocat\w*|maintenance|deadline)\b']),
        ('remaining work still requiring sizing', [r'\b(siz\w*|estimat\w*)\b', r'\b(need\w*|pending|await\w*|before|unconfirmed|unknown|outstanding|not yet|still|must|required)\b', r'\b(rd-202|rd-203|rd-206|repair\w*|remediation|remaining|follow.up|migration|contract|browser|return.filter\w*|index)\b']),
        ('explicit limits on delivery commitments', [r'\b(commit\w*|promis\w*|delivery dates?|release dates?|repair dates?|schedule\w*)\b', r'\b(no|not|without|before|until|pending|subject to|conditional|unconfirmed|cannot|can.t|defer\w*|await\w*)\b'])
    ]
    for description, patterns in capacity_checks:
        if not all(matches(capacity, pattern) for pattern in patterns):
            failures.append('RD-211: new capacity plan does not record ' + description)

    maintenance = unique_record(new_issues, 'RD-212')
    if maintenance is None:
        failures.append('RD-212: dependency maintenance obligation is missing or duplicated')
    else:
        if maintenance.get('projectId') != 'project-relaydesk' or maintenance.get('key') != 'RD-212':
            failures.append('RD-212: maintenance work has lost its Relaydesk identity')
        if maintenance.get('status') not in ['To Do', 'In Progress', 'In Review']:
            failures.append('RD-212: outstanding dependency maintenance is not open')
        if maintenance.get('assigneeId') not in known_people:
            failures.append('RD-212: maintenance has no recognized responsible person')
        if maintenance.get('priority') not in priorities:
            failures.append('RD-212: maintenance priority is missing or invalid')
        maintenance_text = text(maintenance.get('description'))
        maintenance_text += ' ' + ' '.join(text(row.get('content')) for row in new_comments if row.get('issueId') == 'RD-212')
        for description, pattern in [
            ('September 18 maintenance deadline', september_day(18)),
            ('retry behavior checks', r'\bretr(?:y|ies)\b'),
            ('retained job argument checks', r'\barguments?\b')
        ]:
            if not matches(maintenance_text, pattern):
                failures.append('RD-212: missing ' + description)

    if failures:
        return {'3': {'passed': False, 'reason': '; '.join(failures)}}
    return {'3': {'passed': True, 'reason': 'All four named Jira issues contain new, candidate-specific follow-up requirements with recognized assignees and valid priorities. Engineering and dependency maintenance remain open. RD-211 records leave, the panel, closure, support, maintenance deadline, sizing needs and commitment limits. Historical Jira review comments are retained. Prose accuracy, adequacy of estimates and cross-document consistency require semantic review.'}}
