---
name: company-task-grader
description: "Write and calibrate task graders for shared hub app worlds, then grade state and files with worker attribution. Use after task design and initial seeding, not to write setups or certify runtime releases."
---

These rules add to the authoring instructions above; where both speak, they agree.

Every check needs a unique `id` and a `description`. Several checks may cover one criterion; extra
checks do not raise that criterion's weight, so a criterion is not strengthened by adding checks to
it, only by checking the right value.

## State checks

`predicate` carries `app_id`, `selector`, `operator`, `value_json`. `value_json` is a JSON-encoded
operand *string*: `"\"solved\""` for the word solved, `"3"` for the number, `"null"` for none, and
null itself for the operators that take no operand. Leave `rubric` null and the evidence path lists
empty.

Selectors look like `$.tickets[0].status`, `$.tickets[*].status`, `$.comments["1310"]`,
`$.tickets[?(@.id==1310)].status`. A filter compares one direct field to a JSON scalar. There is no
recursive descent (`$..`), no eval, no Python, no shell and no script expression; unsupported syntax
is rejected, not ignored.

What each operator actually decides:

- `exists`: at least one value is selected. A JSON null exists; a missing field does not.
- `equals`: exactly one match, equal to the operand including its type.
- `contains`: one selected list holds the exact operand, an object holds its key, or a string holds
  the operand as a substring. It needs a selector matching exactly one value, so name the record
  (`$.documents["doc-x"].content`), never a wildcard over a collection. Keywords alone do not prove
  content quality.
- `count_gte`: at least the positive integer operand. Counts entries of a selected collection, or
  selector matches under a wildcard or filter, where an object counts once however many fields it
  has. It pins a quantity, not a value.
- `changed`: the selected values differ from the initial ones, deletion included. It proves a write
  happened, not what was written.
- `unchanged`: values exist on both sides and match. Use only as a guard on actual progress.

`changed` and `exists` are the exception, not the tool: an agent that overwrites the field with
gibberish passes both. Use them where the requirement is the change itself, where several answers
are equally correct, or as guards. Never credit existing records, empty thresholds, unchanged
scaffolding, assumptions, comments or hardcoded success. File existence alone cannot verify content.
If the check language cannot express a state criterion faithfully, report the limit for harness
extension or task-design repair rather than substituting a weak ticket-closed or generic-text check.
Accept valid alternative business outcomes.

A scored state check may never select an interface-state collection, and the draft is rejected if it
does. These are the app's view, not the business: `selectedItems`, `uploadQueue`, `undoStack`,
`redoStack`, `clipboard`, `selectionRange`, `currentSortColumn`, `currentSortDirection`,
`currentView`, `currentListFilters`, `currentDate`, `activeView`, `activeSheetId`, `activeModule`,
`searchQuery`, `selectedId`, `sidebarOpen`, `viewMode`, `sortConfig`, `showFormulas`,
`showGridlines`, `isDragging`, `navigatorFilter`, `navigatorExpandedSections`, `shoppingCart`,
`callHistory`, `bookmarkedMessages`, `storageUsed`, `storageTotal`, and `currentUser`, which is
also the signed-in account. Every worker can set all of them without doing any of the work. Select
the records the work changes instead; a judgment or artifact `app_path` may still read a profile.

## Semantic checks

For `kind=artifact` or `judgment`, leave predicate null and guards empty and supply a `rubric` that
assesses the actual work. Include `app_paths` (`{app_id, selector}`) and/or `material_paths`
relative to `world/materials`; paths, symlinks included, must stay inside that directory. Each
selector names the records the criterion is about — a filter on an id or a field, an index, a
sub-path — never a bare collection (`$.documents`), which is rejected so the judge reads those
records in full instead of a trimmed dump. Select the content, the supporting records and the
counterevidence needed to judge the criterion. A rubric must distinguish a complete result from a
partial or incorrect one and from an inconclusive one, and must require new task work. Missing or
unreadable evidence, and no change, earn no credit.

Semantic checks share at most 40% of the score and earn zero when no judge runs, while mechanical
checks carry 60% with a judge and 100% without one. A criterion that a value check can express
faithfully is worth more as a state check than as a rubric.

## The mechanical floor

`pinned_share` is the share of the mechanical score resting on value checks (`equals`, `contains`,
`sheet_cell`, `sheet_contains`, `pdf_contains`, `docx_contains`, `slide_contains`, `image_size`,
`file_contains`), each state criterion weighted equally and its own checks weighted equally
within it. `count_gte`, `file_exists` and `file_absent` do not count: a
junk edit that appends records, saves an empty file or deletes one clears them. A draft below half is handed back
to you, and calibration refuses the proof. At the floor, a run that writes in the right places
without producing the right values earns at most 0.30 of the task.

Calibration then requires every check to fail on the untouched initial world and every one to pass
on the reference result. Those two scores prove nothing about a `changed`-only check: the untouched
world fails it, the reference passes it, and so does vandalism.

## Plain English

Write check descriptions and rubrics for a domain reviewer who does not know this pipeline. Describe
correct app or document results in ordinary words and name the records. Use no pipeline jargon or
undefined acronyms.
