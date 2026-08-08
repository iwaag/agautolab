# Slow Brew Style

## When it fits

Use this style when careful review is worth the extra sessions: broad or
hard-to-reverse changes, shared contracts, infrastructure, security-sensitive
work, or missions whose acceptance needs independent scrutiny. These are
hints for judgment, not hard-coded selection rules. Explicit mission
instructions still win.

## What to skip

Skip ceremony that does not reduce the mission's real risk. Do not invent
extra deliverables, selection algorithms, or acceptance infrastructure merely
because this is the formal style.

## What never to skip

Use the plan, review, approve-or-reject, implement, and independent-audit flow
described in `AGENT_GUIDE.md`, including its plan-review craft section. The
mediator writes neither implementation nor tests; coding agents author the
plan, gates, and implementation. Keep durable NOTES and evidence, and obey the
common charter rules.

## Gate scale

Scale gates to the mission's risk and verify the real named endpoints. Reject
weak gates, but also reject an acceptance framework materially larger than the
product unless the mission itself demands it. Prefer existing, deterministic
checks and finish with an independent audit proportionate to the impact.

## Reporting

Record plan decisions and evidence in NOTES, then provide a concise final
report. Every final mission report, and the implementation episode's own
`report.md` when it has one, must answer exactly these three questions:

- Style chosen:
- Why:
- Was it right in hindsight:

