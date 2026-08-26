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

After adding or changing folders, record in `README_PROJECT.md` what each
folder is for and where its repository lives, so the next run needs no
other explanation.

## "study" pattern

- `main/` ... workspace where you store summaries of knowledge.
- `publish/` ... only reviewed summaries of knowledge, moved here from
  `main/` on the developer's explicit approval. Never push `publish/`;
  the developer pushes it by hand after review.

## "game" pattern

- `main/` ... current source code of the project.
- `direction/` ... concept documents of the project.
- `devlog/` ... plans and reports of past works.
