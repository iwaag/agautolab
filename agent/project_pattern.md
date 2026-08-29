# project patterns

Each folder in the project workspace is either a cloned repository or a
plain local folder, depending on the developer's request.

- When the developer gives a repository URL for a folder, clone it there.
- When the developer asks for a new repository without naming a URL, use
  `autolab project init-repo <folder>`: it creates the repository on the
  local Gitea under the standard name — `autodev/<project>` for `main/`,
  `autodev/<project>-<folder>` for any other folder — and clones it into
  the workspace. Run `autolab --help` for the exact usage.
- When the developer asks for a plain local folder, just create it.

A pattern is a starting layout, not a constraint: a project may add
folders beyond its pattern (with `init-repo` or as plain local folders),
and doing so does not change what pattern the project follows. Record
added folders in `README_PROJECT.md` like any other.

After adding or changing folders, record in `README_PROJECT.md` what each
folder is for and where its repository lives, so the next run needs no
other explanation.

## Repository-backed local tests

A study project may add `localtest-<paper-id>/` for one bounded local
reproduction attempt. It is a convention, not a third mandatory study folder.
Create it with `autolab project init-localtest <paper-id>` from the project
workspace. It uses the ordinary `init-repo` naming path: for example,
`localtest-2608.23283/` in `studyarxiv` maps to
`autodev/studyarxiv-localtest-2608.23283`; an old-style ID such as
`hep-th/9901001` maps to `localtest-hep-th-9901001/` and
`autodev/studyarxiv-localtest-hep-th-9901001`.

The command creates a committed `.gitignore`, `README.md`, `localtest.yaml`,
and `report.md`. `localtest.yaml` is the resumable record, with only these
states: `prepared`, `waiting_external`, `running`, `verified`, `failed`,
`adoption_pending`, and `complete`. Record commands, upstream revisions,
expected and actual evidence, cleanup, and any upper-actor handoff in
`report.md`; keep private host facts, credentials, and large artifacts in the
ignored `.local/` directory. Add the folder and repository to
`README_PROJECT.md` when creating it.

`report.md` is the raw run log. The distilled, publishable result of a local
test lives in `main/papers/<paper-id>/test.md`, beside `summary.md`, and
states the **level** the test reached:

| level | meaning |
|---|---|
| `L1` | the system was built locally and its most basic function was confirmed working |
| `L2` | a workflow described in the paper was completed end to end |
| `L3` | a small, minimal *original* verification experiment measured performance (not a paper reproduction) |
| `L4` | a reproduction of the paper's own experiments, or a performance check beyond them |

A `test.md` names the level reached, the upstream repository and revision
tested, the environment in generic terms only (for example "Apple-silicon
Mac, local Ollama, model `<name>`" — never a hostname, path, or port), the
evidence for the level, and what a later run would have to do to raise it.
Assign the level honestly: a one-shot smoke command is `L1`, however much
setup it took. `test.md` must stand on its own when copied out of `main/`,
so it does not link into `localtest-<paper-id>/`; it names the repository
instead. The `localtest` column of `main/papers/INDEX.md` carries the level
(`no`, `L1`…`L4`), or a `localtest.yaml` state such as `waiting_external`
while a test is in progress. A local test ends by writing or updating
`test.md` and that column.

**The scale travels with the reports.** A bare `L1` means nothing to a
reader who does not have this file, and it collides with unrelated `L0`/`L1`
notation some papers use for their own hierarchies. So the four levels are
restated where the reports are read: `main/papers/INDEX.md` spells them out
where it documents its `localtest` column, and any repository the reports
are copied into — `publish/` — restates them in its own `README.md`. Never
assume the reader has the autolab checkout.

## "study" pattern

- `main/` ... workspace where you store summaries of knowledge.
- `publish/` ... an edited copy of `main/` reports, produced by the
  `publish` routine as its own mission (never as a self-check inside a
  papers or localtest run). The routine reviews each report against the
  publication conditions — no local-environment or secret facts, the
  paper's version stated, no long or unverifiable quotations — edits it
  into compliance, copies it here and commits locally. `main/` stays
  intact: nothing is moved or emptied. Never push `publish/`; the
  developer reviews and pushes it by hand.

## "game" pattern

- `main/` ... current source code of the project.
- `direction/` ... concept documents of the project.
- `devlog/` ... plans and reports of past works.
