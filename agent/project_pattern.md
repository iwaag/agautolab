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

## "study" pattern

- `main/` ... workspace where you store summaries of knowledge.
- `publish/` ... only reviewed summaries of knowledge, moved here from
  `main/` on the developer's explicit approval. Never push `publish/`;
  the developer pushes it by hand after review.

## "game" pattern

- `main/` ... current source code of the project.
- `direction/` ... concept documents of the project.
- `devlog/` ... plans and reports of past works.
