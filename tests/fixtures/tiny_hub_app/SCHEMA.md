# Tiny hub fixture

## State Schema

| Key | Type | Description |
| --- | --- | --- |
| `currentUser` | object | Active user with `id` and `name` strings. |
| `items` | array | Records with `id`, `title`, and `done` fields. |
| `ui` | object | Presentation state such as `view` and nested `filters`. |

This is a dependency-free Node protocol fixture. `server.js` uses only `node:http`
and `node:fs`. The vendored `node_modules/vite/bin/vite.js` delegates to it for
`build` (copy `index.html` into `dist`) and `preview --port P --host H --strictPort`.
`vite.config.js` satisfies the adapter's source-layout check. The package build
script also copies the HTML without dependencies.

The test replaces only the npm install subprocess with a local copy of the
vendored shim: `hub_app.build()` excludes `node_modules` from its source copy and
unconditionally calls npm. Node build/preview, readiness, HTTP, worker proxy,
optional Chrome render and cleanup run normally. No CUA-Gym checkout, registry,
installed npm, model, or VM is needed. Generated files live in pytest's temporary
directory, never in this fixture.

`POST /post?sid=...` accepts `set`, `set_current`, and `reset`. Each session has a
JSON file in `.mock-states` containing its initial and current state. `set` pins
both; `set_current` preserves the baseline; `reset` removes the session file.
`GET /state` returns `stored_state`, `has_custom_state`, and `sid`. `GET /go`
returns `initial_state`, `current_state`, and `state_diff`. Diff objects recurse
through keys and array indices, with `{old, new}`, `{added}`, or `{removed}` leaves.
Unseeded sessions use empty objects/arrays matching the top-level schema.

`POST /upload` accepts one multipart file, stores it under `.mock-files/<sid>`,
and returns `{success, files: [{original_name, stored_name, size, content_type, url}]}`.
The URL supports binary readback. Repeated uploads with the same filename overwrite
the fixture file; multipart batching and production upload behavior are out of scope.
The page fetches `/state` and displays it as text for the optional render check.
