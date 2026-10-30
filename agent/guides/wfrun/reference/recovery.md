# Continue interrupted work

- Read `wfe run show <run> --history` and `wfe run state <run>`. They distinguish a wait, a hold, failure and completed work. This attempt explains why execution resumed.
- Compare the records with files and external effects before repeating work. A process may have performed an action before its record was written.
- `wfe run session <run>` describes session continuity. Remembered conversation is useful context, but current records determine what was answered, held and completed.
- The run copy persists; newer project commits are not necessarily merged into it. Inspect its actual revision before assuming it contains recent changes.
- Respect a person's hold or cancellation. An automatic recovery is not evidence that they withdrew it.
- Use foreground execution for commands that must finish during this attempt. For waits beyond it, record a question, delegated wait or other supported WFE wait. Session background notifications do not reach an ended attempt.
- `wfe run help reversal` describes reversal facts. Integration, reverting Git commits and undoing external effects are distinct operations.

- An ended attempt's ID cannot write for a later one. A refusal naming another attempt means this attempt was stopped or replaced.
- Commits and pushes are available to you. Automatic saving at attempt end saves the run branch; leaving the copy on a different branch prevents that save. `wfe run help integrate` describes integration and conflicts.
