# Instant Ramen Style

## When it fits

Use this style for small, reversible work where rapid delivery matters more
than exhaustive assurance: tiny offline apps, narrow improvements, and
experiments with limited blast radius. Shared contracts, infrastructure, or
hard-to-reverse changes are reasons to consider Slow Brew, not automatic
rules. Explicit mission instructions still win.

## What to skip

Skip the plan and approval round trip. Put `gates` directly in `job.yaml`,
which makes autolab enter the implement phase immediately. Do not add a style
field or change autolab code for this. Do not build a comprehensive test suite
or an acceptance framework for a small product.

## What never to skip

Keep durable `NOTES.md` handoffs and per-iteration evidence. The mediator still
does not write implementation. It may write the minimal smoke-gate commands in
`job.yaml`, because no planning agent supplies them in this style. Run
`run-once` and `loop` only in the foreground of the live mediator session.
Respect the common charter rules for secrets and permissions.

## Gate scale

Use a handful of cheap smoke checks that prove the requested result at its
real endpoint: for example, the build succeeds, an endpoint answers, or a
required file exists. Reuse existing commands. Never author a custom test
harness for this style. Name the endpoint or artifact each gate probes, and
independently inspect the delivered result before declaring completion.

## Reporting

Declare one small goal for each iteration in NOTES and keep the final report
short. Every final mission report, and the implementation episode's own
`report.md` when it has one, must answer exactly these three questions:

- Style chosen:
- Why:
- Was it right in hindsight:

