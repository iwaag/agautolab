# Development styles

Two ways to run a mission. Name one in NOTES.md with a one-line reason, or
switch mid-mission the same way; the same jobs continue either way.

- **instant-ramen** — write `gates` straight into `job.yaml`, so the job
  starts in the implement phase. No plan/approve round trip. Small,
  reversible work.
- **slow-brew** — leave `gates` out, so the coding agent proposes a plan and
  gates and you approve or reject them, then audit the result yourself.
  Shared contracts, infrastructure, hard-to-reverse work.

Cost differs: slow-brew spends at least one extra coding-agent iteration plus
your review sessions.
