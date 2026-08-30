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
so it neither links into `localtest-<paper-id>/` nor names that repository —
say only that a separate internal repository holds the raw run log. The
workspace's `README_PROJECT.md` already records which one. The `localtest` column of `main/papers/INDEX.md` carries the level
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

## Repository-backed generation tests

A study project whose subject is media generation may add generation tests
the same way, and for the same reason: one bounded, resumable folder per
subject, backed by its own repository on the ordinary `init-repo` naming
path. `autolab project init-localtest <subject>` reuses that path directly;
`autolab project init-repo gentest-<subject>` plus a hand-written yaml is the
alternative when the `paper-id`/`localtest` vocabulary does not fit. Either
is fine — record which one the project chose in `README_PROJECT.md`, as with
any other folder.

**A subject is not only a checkpoint or a LoRA set.** A *workflow family* —
"sprite-sheet animation from a video model", "skeletal rigging on
AI-generated parts", "instruction-guided keyframe editing" — is a subject of
exactly the same shape: it gets `main/<subject>/summary.md` and
`main/<subject>/tips.md` like any other, its own `gentest-<subject>/`
repository by the same route, and its own row in the project's `INDEX.md`
(under the index's workflow-family heading rather than its checkpoint one).
The distinction matters only for what a tip is *about*: for a checkpoint a
tip says "under these settings this model produces that"; for a workflow
family it says "wiring these steps together in this order produces that, and
this step is where it breaks". Nothing else changes — same append-only
`tips.md`, same evidence line, same absence of a level scale. Do not fold a
workflow subject into an existing checkpoint's `gentest-` repository: that
repository's yaml records one subject and one state, and a finished
(`verified`) checkpoint test is not the place to start an unfinished
workflow one.

The resumable yaml records the **subject** (a checkpoint, a LoRA set, or a
workflow family), the **backend** the images were generated on (in generic
terms only — never a hostname, port, or path), the **model or workflow**
under test, and the **state**. `report.md` is the raw run log: the matrix
spec, per-image parameters and timings, and what was judged against what.

The distilled, publishable result is `main/<subject>/tips.md`. A tip is
"under these conditions this comes out", and a condition that did nothing is
a tip too — "that negative prompt changed nothing" saves the next run the
same experiment. Every tip carries on one line the evidence that produced it
(matrix cell or seed, plus the settings that mattered) and the date it was
found. `tips.md` is **append-only**: a later run that contradicts an earlier
tip adds a newer one rather than rewriting it, so the file reads as a history
of what was learned and a second run always knows where to append. Keep it
apart from `summary.md`, which says what the model *is* and what its authors
and the public recommend — hearsay belongs there, findings belong here.

**There is no level scale for generation tests.** The `L1`–`L4` axis above is
a claim about how far a paper was reproduced and means nothing for a
generation sweep. The `tips` column of the project's `INDEX.md` therefore
says only whether tips exist — `no`, or the date of the most recent tip.

Like `test.md`, `tips.md` must stand on its own when copied out of `main/`:
it neither links into the generation-test folder nor names that repository,
and it carries no host facts. Generated files are large and often not
publishable, so raw outputs stay in the test repository's ignored `.local/`;
a few small contact sheets may be committed to the test repository, and one
or two in `main/` when they carry the point.

## The open-question queue: `main/QUESTIONS.md`

Every generation run ends knowing more than it set out to learn, and knowing
what it still cannot answer. Until now those unknowns died at the bottom of
one subject's `tips.md` under "Still open", where nothing ever read them
back. `main/QUESTIONS.md` is the queue that fixes that. It is part of `main/`
and therefore **publish-ready like the rest of it**: no host facts, no
credentials, no internal repository names.

One entry per open question, each carrying:

- **subject** — the `main/<subject>/` it belongs to, or `-` when the question
  is what the next subject should be;
- **the question**, stated so a run that has never read this file can act on
  it;
- **why it matters** — what a decision or an asset would gain from the
  answer;
- **what evidence would close it** — the matrix, the comparison, or the
  observation that would settle it, not a vague "investigate";
- **raised** — the date it was raised;
- **status** — `open`, or `blocked` naming the human action it waits on (see
  the handoff paragraph in "Repository-backed local tests"; a run that needs
  a host-level install raises its question here as `blocked` rather than
  leaving the request only in a test repository, because this file is the
  only thing a later fire is guaranteed to read).

A run **closes** an entry by appending to the subject's `tips.md` the tip
that answers it and marking the entry closed with the date and a pointer to
that tip; it **raises** the new questions its own work created. Closed
entries stay in the file — the queue is a history of what was asked, the same
way `tips.md` is a history of what was learned.

Each `tips.md` keeps its own "Still open" section: that is the local
narrative, written for someone reading that one subject end to end.
`QUESTIONS.md` is the **cross-subject queue a routine reads first**, before
it decides what this fire is about. The two are allowed to overlap and the
queue is the one that gets consumed.

A fire that finds only `blocked` entries **says so and stops**. It does not
invent a question to have something to do; an empty actionable queue is a
real answer and reporting it is the whole of that fire's work.

## "study" pattern

- `main/` ... workspace where you store summaries of knowledge.
- `main/QUESTIONS.md` ... the project's open-question queue (see above).
  A study routine reads it before it decides what a fire is about.
- `publish/` ... a copy of the `main/` reports that pass the publication
  review, produced by the `publish` routine as its own mission (never as a
  self-check inside a papers or localtest run). The routine checks each
  report against the publication conditions — no local-environment or
  secret facts, the paper's version stated, no long or unverifiable
  quotations, no internal-workflow residue — and when one fails, it
  **fixes the report in `main/` first and then copies it here**, so the two
  always agree. A published file is never edited on the way out; a
  difference between `main/` and `publish/` is a defect, not a policy.
  Nothing is moved out of `main/` — it keeps every report, published or
  not. Because `main/` is written publish-ready, private material —
  hostnames, paths, ports, internal repository names, raw run logs —
  belongs in the `localtest-<paper-id>/` repositories and `.local/`, never
  in `main/papers/`. `publish/` is read by strangers: its `README.md`
  says what the papers are, what each file contains, and what the test
  levels mean — nothing about `main/`, routines, missions, reviews, or
  any other internal arrangement, and the same goes for every published
  file (no "a separate internal repository holds…" asides; just leave
  such things out). Never push `publish/`; the developer reviews and
  pushes it by hand.

## "game" pattern

- `main/` ... current source code of the project.
- `direction/` ... concept documents of the project.
- `devlog/` ... plans and reports of past works.
