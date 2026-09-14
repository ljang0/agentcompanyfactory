# SanMar pilot

The included example is Cedarline Apparel Supply, a fictional apparel wholesaler
informed by SanMar research. It has four roles: sales supervisor, account
representative, order coordinator and marketing specialist. Its apps are Gmail,
Slack, Calendar, Drive, Docs, Sheets, HubSpot and Meta Ads.

- [Inspect assignments, references and grades](../examples/sanmar/README.md).
- [Download the complete world and replay it](COLLABORATOR.md).
- [Read measured acceptance and remaining work](END-TO-END-PLAN.md).
- [Follow the general stage commands](PIPELINE.md).

The full snapshot's company folder is `experiments/pilot-sanmar/company`. Use
`configs/pilot-sanmar.local.toml`, copied from `configs/pilot-sanmar.toml`, when
working with that archive. Its accepted baseline and tasks should be reused while
their inputs and proof hashes still match.

The two tasks share one world lineage. Keep them in the same benchmark split, and
keep private assessments, references and verifiers off ordinary worker desktops.
Both reference replays and resets passed; teacher and ordinary outcomes are listed
in the example index. The inspection release does not establish full ordinary-team
or ablation acceptance.
