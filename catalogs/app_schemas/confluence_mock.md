# confluence_mock Schema

**Deploy order**: 19 (alphabetical among all `*_mock` directories; `BASE_PORT=8000` → port 8019)  
**Base URL**: `http://172.17.46.46:8019/`  
**Go Endpoint**: `GET /go?sid=<sid>` → `{initial_state, current_state, state_diff}`  
**Inject**: `POST /post?sid=<sid>` with body `{"action":"set","state":{...}}`

## State Schema

| Key | Type | Description and default |
|-----|------|-------------------------|
| `users` | `User[]` | Three users: `user-1` through `user-3` |
| `currentUser` | `User` | Default: `user-1` (Admin User) |
| `spaces` | `Space[]` | Two spaces: Engineering and Human Resources |
| `pages` | `Page[]` | Three pages |
| `comments` | `Comment[]` | One default comment |
| `versions` | `Version[]` | One default historical version |
| `templates` | `Template[]` | Meeting Notes and Project Plan templates |

### Entity fields

- User: `{ id, username, displayName, email, avatar }`
- Space: `{ id, key, name, description, icon? }`
- Page: `{ id, spaceId, parentId, title, content, authorId, created, updated, version, labels[] }`
- Comment: `{ id, pageId, userId, content, created }`
- Version: `{ id, pageId, content, authorId, created, version }`
- Template: `{ id, name, content }`

Page and template `content` values are HTML strings.

## Minimal Inject Example

```json
{
  "type": "chrome_open_url",
  "parameters": {
    "url": "http://172.17.46.46:8019/",
    "inject_state": true,
    "state_content": {
      "action": "set",
      "state": {
        "users": [{"id":"user-1","username":"admin","displayName":"Admin User","email":"admin@example.com","avatar":"https://picsum.photos/100/100?random=user1"}],
        "currentUser": {"id":"user-1","username":"admin","displayName":"Admin User","email":"admin@example.com","avatar":"https://picsum.photos/100/100?random=user1"},
        "spaces": [{"id":"space-1","key":"ENG","name":"Engineering","description":"Engineering docs","icon":"Code"}],
        "pages": [{"id":"page-1","spaceId":"space-1","parentId":null,"title":"Home","content":"<h1>Home</h1>","authorId":"user-1","created":"2026-01-01T00:00:00.000Z","updated":"2026-01-01T00:00:00.000Z","version":1,"labels":[]}],
        "comments": [],
        "versions": [],
        "templates": []
      }
    }
  }
}
```

## Observable State Changes (for LLM evaluation)

| User Action | State Field Changed |
|-------------|---------------------|
| Create a space | `spaces` (new `{id,name,key,description}` item) |
| Create a page | `pages` (new item with generated `id`, timestamps, `version:1`, and `labels:[]`) |
| Save edited page title/content | `pages[*].title`, `pages[*].content`, `pages[*].updated`, `pages[*].version`; prior content appended to `versions` |
| Drag a page under another page (or to the root) | `pages[*].parentId` |
| Add a comment | `comments` (new entry) |
| Restore a page version | `pages[*].content`, `pages[*].updated`, `pages[*].version`; displaced content appended to `versions` |
| Switch user | `currentUser` |

