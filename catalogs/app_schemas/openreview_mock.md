# openreview_mock Schema

**Deploy order**: 62 (alphabetical among all `*_mock` directories; `BASE_PORT=8000` → port 8062)  
**Base URL**: `http://172.17.46.46:8062/`  
**Go Endpoint**: `GET /go?sid=<sid>` → `{initial_state, current_state, state_diff}`  
**Inject**: `POST /post?sid=<sid>` with body `{"action":"set","state":{...}}`

## State Schema

| Key | Type | Description and default |
|-----|------|-------------------------|
| `user` | `CurrentUser` | Default: `{id:"~Sarah_Chen1", role:"area_chair"}` |
| `venue` | `Venue` | NeurIPS 2025 venue configuration |
| `profiles` | `Record<string, Profile>` | Profile ID → profile |
| `groups` | `Record<string, Group>` | Group ID → group |
| `notes` | `Record<string, Note>` | Submission note ID → submission |
| `reviews` | `Record<string, Note>` | Review/comment note ID → reply note |
| `edges` | `Edge[]` | Assignment, affinity, bid, and conflict edges |
| `invitations` | `Record<string, Invitation>` | Invitation ID → invitation |
| `edgeBrowserConfig` | `object` | Default: `{maxPapersPerReviewer:5, minReviewersPerPaper:3}` |

### Entity fields

- CurrentUser: `{ id, role }`
- Venue: `{ id, shortPhrase, fullName, website, submissionName, officialReviewName, officialMetaReviewName, reviewerName, areaChairName, reviewRatingName, metaReviewRecommendationName, deadline, dates }`, where `dates` is `{submission,review,decision}`
- Profile: `{ id, active, content }`; content is `{ names[], emails[], preferredEmail, emailsConfirmed[], history[], expertise[], homepage?, dblp? }`
- Profile name: `{ fullname, first, middle, last, preferred, username }`
- Profile history: `{ position, institution:{name,domain}, start, end }`; expertise: `{ keywords[], start, end }`
- Group: `{ id, members[], readers[], writers[], signatories[], domain, cdate, ddate }`
- Note: `{ id, forum, invitations[], domain, number, cdate, tcdate, mdate, tmdate, ddate, pdate, odate, replyto, signatures[], readers[], writers[], nonreaders[], license, content, details }`
- Edge: `{ id, invitation, head, tail, label, weight, readers[], writers[], nonreaders[], signatures[], cdate, tcdate, mdate, tmdate, ddate }`
- Invitation: `{ id, domain, cdate, duedate?, expdate?, edit, details?, replyForumViews?, edge? }`

`Note.content` is an object whose values use OpenReview's `{value: ...}` wrapper. Submissions use the keys `title`, `authors`, `authorids`, `abstract`, `keywords`, `TLDR`, `pdf`, `venue`, `venueid`, and `_bibtex`. Official reviews use `title`, `review`, `rating`, `confidence`, `soundness`, `presentation`, `contribution`, `strengths`, `weaknesses`, `questions`, and `limitations`. Comments use `title` and `comment`.

## Minimal Inject Example

```json
{
  "type": "chrome_open_url",
  "parameters": {
    "url": "http://172.17.46.46:8062/",
    "inject_state": true,
    "state_content": {
      "action": "set",
      "state": {
        "user": {"id":"~Sarah_Chen1","role":"area_chair"},
        "profiles": {},
        "groups": {},
        "notes": {},
        "reviews": {},
        "edges": [],
        "invitations": {},
        "edgeBrowserConfig": {"maxPapersPerReviewer":5,"minReviewersPerPaper":3}
      }
    }
  }
}
```

The app deep-merges injected objects with defaults, so omitted default objects such as `venue` remain available.

## Observable State Changes (for LLM evaluation)

| User Action | State Field Changed |
|-------------|---------------------|
| Submit an official review | `reviews[generatedReviewId]` |
| Edit an existing official review | review fields in `reviews[reviewId]`, plus `mdate` and `tmdate` |
| Post a forum comment | `reviews[generatedCommentId]` |
| Assign a reviewer to a paper | `edges` (assignment edge appended) |
| Unassign a reviewer | matching assignment `edges[*].ddate` |
| Invite a profile to the reviewer pool | `groups["NeurIPS.cc/2025/Conference/Reviewers"].members`; generated affinity/conflict entries appended to `edges` |

