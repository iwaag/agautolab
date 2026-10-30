# Organize the work

- Read the request and current graph with `wfe run show <run>`. Use `wfe run help start|progress|complete|fail` for recording work (ask for each command separately).
- A talk is consultation. For substantive work, the person's catalog describes activities they want visible as separate nodes: `wfe run nodes <run>`.
- `wfe nodes help` explains creating nodes from templates; `wfe run help adjust` explains changing this run's graph. The reusable workflow remains separate. Choose steps that fit the request rather than adding every available template.
- Work already done outside a node can be recorded honestly with `wfe run help record-done`. Future work remains pending.
- Questions requiring the person's answer belong in `wfe run ask`; see `wfe run help ask`. A recorded question can wait without a running process. Answers and new instructions appear in `wfe run state`; `take-up` and `ack` record your response to them.
- Generated documentation steps are optional. Pending documentation does not prevent execution from becoming completed. Use documents when they carry the result or evidence the reader needs.
- Keep run-specific documents in the run folder. A root report can be mistaken for the project's maintained documentation and overwritten by another run.
- WFE may respond to an operation with a reminder instead of recording it. Read what it observed, address what actually applies, or continue as the response explains. A reminder is evidence about records, not a judgement about your work.

- Agreements, acceptance and integration are separate records, not permissions to work. Record your own actions under your execution identity; do not attribute your judgement to the person.
- Repository bindings describe intended use; they are not filesystem enforcement. The run copy is where edits belong, not the person's shared workspace.
- Changes requested in human review or returned by a requesting agent reopen obligations. Consult `wfe review help` or `wfe run help correction`, preserve completed work and add the necessary correction.
