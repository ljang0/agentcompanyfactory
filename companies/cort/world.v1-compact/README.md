# world/ — Stage 2 deliverables (seed skill output)

For every app in ../apps.json write `<app_id>.state.json`: the complete native state
with every documented state key (see the schema path in apps.json). The hydrator posts
it verbatim to the app's `/post {action: set}`; nothing else seeds the app.

Also write:
- `world.json`        canonical entities and history that the app states project from
- `identities.json`   {worker_id: {app_id: user record}} for every app with an identity_key;
                      each record must also appear in that app's users collection
- `materials/<worker_id>/...`   per-worker desktop files delivered only to that worker's VM

Do not put the private feasible path, grading key or delegation plan in any of these.
