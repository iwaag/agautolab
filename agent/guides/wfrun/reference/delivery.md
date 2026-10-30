# Answer or hand over a result

Use the destination in this attempt. An ordinary run view and a WFE conversation room are different surfaces.

## A request answered in the run view

- The person reads the run's outcomes, questions and attached files. Put an answer in those records, not only in your session output.
- For a concise answer, the node's outcome can contain the answer itself: `wfe run complete <run> <node> --outcome <answer>` after starting the node.
- For a longer answer, write it in the run folder and attach the existing file with `--artifact <path>` on completion. The outcome explains what the file answers. `wfe run help attach` describes attaching an existing result separately.
- A short outcome is useful as a summary when the body is available elsewhere. “Answered the question” is not the body.

## A request from a WFE conversation room

See [conversation roles](conversation.md) when participating in or conducting a room.

- `wfe conversation show` reads the room and message IDs. `wfe conversation help` describes replies and other operations.
- Reply with `wfe conversation reply --re <message-id> …`. `--completing <node>` records the reply and node completion together. A progress note is not the answer to the request.
- A file path alone gives the reader no way to open it. Include the result's usable link; the room's linked-run information supplies file routes.
- Questions still use `wfe run ask`; WFE delivers them and routes answers back. Later words concerning existing work can be carried to that run with `wfe run instruct` or `answer`, preserving their source message.

## Results the person needs to try or review

- `wfe present help` explains freezing and serving a result. `wfe review help` explains handing finished work to the person for review and receiving requested changes.
- A presentation and human-review node suit work that needs this form of inspection. A simple factual answer does not need a server or review ceremony.
- Verify that the delivered files exist and that the proposed opening method works. Describe checks actually performed and any limits. A native application needs practical launch instructions.
- Keep ownership of presentation servers clear: `wfe present status` shows them; `stop` ends them. A stopped page is not an available link. Human review can restart its registered presentations when the person begins reviewing.
- The project's showcase is separate from one run's report. When the work changes what the project offers, consult `wfe boast status <project>` and `wfe boast reference`.
