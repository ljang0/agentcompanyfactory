---
name: company-task-amplify
description: "Make task variants from an accepted task and seeded hub world. Use for variants, not initial task design or runtime certification."
---

`allowed_records` is the whole editable surface. The base world and the rest of the company are not
in the payload, so a record you do not see is a record you cannot touch: an edit naming anything
outside `allowed_records` is rejected.

Each entry's `editable_leaves` is keyed by the **relative** pointer you put in `edit.path`, and maps
it to that leaf's current value. That value is the only `before_json` the replay accepts.

Each edit replaces one scalar leaf, keeps its JSON type (a string stays a string, a number a
number), and changes it: a second edit on the same path, and an `after_json` equal to the
`before_json`, are both rejected.

When you edit `grader_edits`, the changed check must still fail on the initial world and pass on the
reference as you have edited it; a check that the initial world already passes is rejected, and so is
one the edited reference fails.
