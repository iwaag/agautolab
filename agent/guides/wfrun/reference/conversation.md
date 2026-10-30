# Conversation roles

- Read the conversation and reply to the message this run serves; multiple requests may be open. `wfe conversation help` describes reply, link, workflow references and membership operations.
- Speak from your project's knowledge and mission. Contributions do not start other agents; the person and conductor request participation. Disagreement need not be resolved by consensus.
- A standing conductor rests between turns. A participant run completes its requested work independently of the room's lifetime. A conversation reply does not complete outstanding delegated work.
- Lengthy work belongs in an ordinary workflow rather than occupying the shared conversation slot. A delegated result is relayed with `--kind result`; a progress note does not answer a request.
- Later instructions about existing work must reach that run, preserving the person's identity and source message. `wfe run instruct` and `answer` support `--source-message`; do not present your relay as the person's own new words without that provenance.
- `wfe conversation link` records your interpretation of a message's relation to work. `wfe conversation workflow attach` records a workflow used as discussion material; mentioning its name alone does not attach it.
- When describing integration, inspect `wfe projects run <project> <workflow>/<run> --integration`. A stale local main does not establish the published state.
- Role-specific project guides and current conversation records supply the conductor's or participant's particular duties.
