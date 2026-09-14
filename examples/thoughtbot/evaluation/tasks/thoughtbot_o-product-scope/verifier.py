import re

TARGETS = ('RD-201', 'RD-203', 'RD-205', 'RD-207')
PRIORITIES = ('Highest', 'High', 'Medium', 'Low', 'Lowest')


def result(passed, reason):
    return {'3': {'passed': passed, 'reason': reason}}


def plain(value):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'<[^>]*>', ' ', value)
    for encoded, decoded in (
        ('&nbsp;', ' '), ('&amp;', '&'), ('&lt;', '<'),
        ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'"),
        ('&#x27;', "'"),
    ):
        value = value.replace(encoded, decoded)
    return re.sub(r'\s+', ' ', value).strip()


def words(value):
    return tuple(re.findall(r'[a-z0-9]+', plain(value).lower()))


def prose(value):
    if not isinstance(value, str):
        return ''
    # Timestamp and retained branch metadata alone are not requirements work.
    value = re.sub(
        r'(?im)^\s*(last updated|updated at|pull request|revision)\s*:.*$',
        '', value,
    )
    return plain(value)


def index_records(state, app, collection):
    app_state = state.get(app)
    if not isinstance(app_state, dict):
        return {}, 'Missing ' + app + ' state.'
    rows = app_state.get(collection)
    if not isinstance(rows, list):
        return {}, 'Missing or unreadable ' + app + '.' + collection + '.'
    indexed = {}
    for row in rows:
        if not isinstance(row, dict):
            return {}, 'Unreadable record in ' + app + '.' + collection + '.'
        identity = row.get('id')
        if not isinstance(identity, str) or not identity:
            return {}, 'A record in ' + app + '.' + collection + ' has no usable ID.'
        if identity in indexed:
            return {}, 'Duplicate record ID in ' + app + '.' + collection + '.'
        indexed[identity] = row
    return indexed, ''


def new_prose(before, after):
    old = prose(before)
    new = prose(after)
    if not new or words(old) == words(new):
        return False
    # Deletion, rearrangement, formatting and case changes are insufficient.
    old_words = set(words(old))
    return bool(set(words(new)) - old_words)


def scope_coverage(issue_id, description):
    # These are subject-coverage checks, not judgments of factual correctness.
    # They do not establish that a proposal, qualification or denial is sound.
    text = prose(description).lower()
    patterns = {
        'RD-201': (
            r'\b(review\w*|approv\w*)\b',
            r'\b(visib\w*|find\w*|locat\w*|display\w*|show\w*|detail|history|activity)\b',
        ),
        'RD-203': (
            r'\b(state|status|decision\w*)\b',
            r'\b(import\w*|legacy|existing)\b',
            r'\b(consumer\w*|compatib\w*|transition\w*|migrat\w*|client\w*)\b',
        ),
        'RD-205': (
            r'\bdepartment\w*\b',
            r'\b(rout\w*|assign\w*|reviewer\w*)\b',
        ),
        'RD-207': (
            r'\b(reopen\w*|re-open\w*|resubmit\w*|re-submit\w*)\b',
            r'\b(declin\w*|reject\w*)\b',
            r'\b(permission\w*|right\w*|authori\w*|rule\w*|polic\w*|who|role\w*|promise\w*)\b',
        ),
    }
    return all(re.search(pattern, text) for pattern in patterns[issue_id])


def github_priority(issue, labels):
    values = issue.get('labels')
    if not isinstance(values, list):
        return None
    found = set()
    for value in values:
        if not isinstance(value, str):
            return None
        name = value
        if value in labels:
            name = labels[value].get('name')
        if isinstance(name, str):
            for priority in PRIORITIES:
                if name.lower() == priority.lower():
                    found.add(priority)
    if len(found) != 1:
        return None
    return list(found)[0]


def preserve_comments(before, after, location):
    if not isinstance(before, list) or not isinstance(after, list):
        return 'Missing comment history for ' + location + '.'
    indexed = {}
    for comment in after:
        if not isinstance(comment, dict):
            return 'Unreadable comment history for ' + location + '.'
        identity = comment.get('id')
        if not isinstance(identity, str) or identity in indexed:
            return 'Missing or duplicate comment ID for ' + location + '.'
        indexed[identity] = comment
    for comment in before:
        if not isinstance(comment, dict) or not isinstance(comment.get('id'), str):
            return 'Unreadable initial comment history for ' + location + '.'
        retained = indexed.get(comment['id'])
        if retained is None:
            return 'Historical comment removed from ' + location + '.'
        for field in ('content', 'authorId', 'userId', 'issueId', 'date', 'createdAt'):
            if field in comment and retained.get(field) != comment[field]:
                return 'Historical comment ' + field + ' changed in ' + location + '.'
    return ''


def verify(initial, final, files, events):
    if not isinstance(initial, dict) or not isinstance(final, dict):
        return result(False, 'Initial and final app states are required.')
    if initial == final:
        return result(False, 'The required GitHub and Jira records have not changed.')

    collections = {}
    for side, state in (('initial', initial), ('final', final)):
        for app, collection in (
            ('github_mock', 'issues'),
            ('github_mock', 'labels'),
            ('github_mock', 'users'),
            ('jira_mock', 'issues'),
            ('jira_mock', 'users'),
            ('jira_mock', 'comments'),
            ('gmail_mock', 'emails'),
        ):
            rows, error = index_records(state, app, collection)
            if error:
                return result(False, side + ': ' + error)
            collections[(side, app, collection)] = rows

    old_github = collections[('initial', 'github_mock', 'issues')]
    github = collections[('final', 'github_mock', 'issues')]
    old_jira = collections[('initial', 'jira_mock', 'issues')]
    jira = collections[('final', 'jira_mock', 'issues')]
    known_github_users = collections[('initial', 'github_mock', 'users')]
    known_jira_users = collections[('initial', 'jira_mock', 'users')]
    owner_changes = []
    priority_changes = []

    for identity in TARGETS:
        if identity not in old_github or identity not in old_jira:
            return result(False, 'Initial record missing for ' + identity + '.')
        if identity not in github or identity not in jira:
            return result(False, 'Required GitHub or Jira record removed: ' + identity + '.')
        old_g = old_github[identity]
        old_j = old_jira[identity]
        g = github[identity]
        j = jira[identity]

        if g.get('repoId') != 'repo-relaydesk':
            return result(False, identity + ' is not in the Relaydesk repository.')
        if isinstance(g.get('number'), bool) or g.get('number') != int(identity[3:]):
            return result(False, identity + ' has a changed GitHub issue number.')
        if j.get('projectId') != 'project-relaydesk' or j.get('key') != identity:
            return result(False, identity + ' has a changed Jira project or key.')
        for field in ('authorId', 'createdAt'):
            if field not in g or g[field] != old_g.get(field):
                return result(False, identity + ' lost its original GitHub ' + field + '.')
        for field in ('reporterId', 'createdAt'):
            if field not in j or j[field] != old_j.get(field):
                return result(False, identity + ' lost its original Jira ' + field + '.')
        if not plain(g.get('title')) or not plain(j.get('summary')):
            return result(False, identity + ' needs a populated title in both systems.')

        for system, before, after in (('GitHub', old_g, g), ('Jira', old_j, j)):
            if not new_prose(before.get('description'), after.get('description')):
                return result(False, identity + ' has no new requirements prose in ' + system + '.')
            if not scope_coverage(identity, after.get('description')):
                return result(False, identity + ' omits its required subject matter in ' + system + '.')

        # These are still unfinished product changes or investigations.
        # GitHub has no separate In Review column in this native schema.
        expected_columns = {'To Do': 'todo', 'In Progress': 'inprogress', 'In Review': 'inprogress'}
        status = j.get('status')
        if not isinstance(status, str) or status not in expected_columns:
            return result(False, identity + ' is marked finished or has an invalid Jira status.')
        if g.get('status') != 'open' or g.get('closedAt') is not None:
            return result(False, identity + ' is closed in GitHub although unfinished work remains.')
        if g.get('column') != expected_columns[status]:
            return result(False, identity + ' has inconsistent GitHub and Jira progress.')

        assignees = g.get('assignees')
        owner = j.get('assigneeId')
        if not isinstance(assignees, list) or not assignees:
            return result(False, identity + ' has no GitHub owner.')
        if not all(isinstance(person, str) and person in known_github_users for person in assignees):
            return result(False, identity + ' names an unknown GitHub owner.')
        if len(set(assignees)) != len(assignees):
            return result(False, identity + ' has duplicate GitHub assignees.')
        if not isinstance(owner, str) or owner not in known_jira_users or owner not in assignees:
            return result(False, identity + ' has inconsistent or missing ownership.')
        if set(assignees) != set(old_g.get('assignees', [])) or owner != old_j.get('assigneeId'):
            owner_changes.append(identity)

        priority = github_priority(g, collections[('final', 'github_mock', 'labels')])
        if priority is None or priority != j.get('priority'):
            return result(False, identity + ' has missing, ambiguous or inconsistent priorities.')
        old_priority = github_priority(old_g, collections[('initial', 'github_mock', 'labels')])
        if priority != old_priority or j.get('priority') != old_j.get('priority'):
            priority_changes.append(identity)

        error = preserve_comments(old_g.get('comments'), g.get('comments'), 'GitHub ' + identity)
        if error:
            return result(False, error)

    old_comments = collections[('initial', 'jira_mock', 'comments')]
    final_comments = collections[('final', 'jira_mock', 'comments')]
    # Preserve the correspondence, while allowing unrelated status/priority work.
    error = preserve_comments(list(old_comments.values()), list(final_comments.values()), 'Jira')
    if error:
        return result(False, error)

    old_emails = collections[('initial', 'gmail_mock', 'emails')]
    emails = collections[('final', 'gmail_mock', 'emails')]
    for identity, before in old_emails.items():
        after = emails.get(identity)
        if after is None:
            return result(False, 'An original email was removed: ' + identity + '.')
        for field in ('threadId', 'from', 'to', 'cc', 'bcc', 'subject', 'body', 'timestamp', 'attachments'):
            if field in before and (field not in after or after[field] != before[field]):
                return result(False, 'Original email ' + field + ' changed: ' + identity + '.')
        if after.get('folder') in ('trash', 'spam') and after.get('folder') != before.get('folder'):
            return result(False, 'An original email was moved out of usable correspondence: ' + identity + '.')

    reason = (
        'All eight required descriptions contain new prose and retain their subject matter; '
        'issue identities, known owners, priorities and unfinished statuses agree across systems. '
        'Original email correspondence and issue comments are retained. '
        'Semantic review must establish that the revised scope and open questions actually agree '
        'and that progress changes are justified.'
    )
    if owner_changes:
        reason += ' Reassignments requiring rationale review: ' + ', '.join(owner_changes) + '.'
    if priority_changes:
        reason += ' Priority changes requiring rationale review: ' + ', '.join(priority_changes) + '.'
    return result(True, reason)
