# Historical SanMar population helpers

These scripts record the company-specific authoring and repair work behind the
first pilot. They contain fixed Cedarline records, IDs, dates and counts. They
are retained as historical construction code, not a new-company interface.

New worlds use `seed-core`, `populate-world`, `review-world`, `verify-world` and
`freeze-world`. Changes to accepted worlds use the explicit repair commands in
[the pipeline guide](../../../docs/PIPELINE.md).

The helpers still resolve `experiments/pilot-sanmar/company` relative to the
repository root. Do not run them against the frozen inspection archive. Some
perform model authoring or write population files; names such as `prepare.py`
do not imply a harmless setup operation.

They previously lived under `scripts/pilot_stage3_*.py`. Their names here drop
that prefix, and their repository-root lookup accounts for the new directory.
The measured September 14 archive retains its original paths and bytes.
