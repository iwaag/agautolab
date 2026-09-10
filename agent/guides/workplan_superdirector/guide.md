
The topic is supposed to be about planning and preparation for next mission in the development.
Your reply to this conversation will be sent to the developer.

# If the developer wants you to prepare the project

The file "README_PROJECT.md" explains how the folders inside the workspace are supposed to work.

If "README_PROJECT.md" doesn't exist, create it to explain how each folder works.
You edit "README_PROJECT.md" only when you added new repositories or local folders in the workspace, or changed the way to manage development of the project.

The command "autolab doc patterns" explains how project structure should be managed based on pattern. If you are asked to create project based on specific pattern, follow it. If not enough information is provided, ask questions. If the developer specified nonexistent pattern, just say it's unknown pattern.

# If the developer is giving you a new mission

Read the project's index in `main/` first. If an existing or finished investigation already covers or answers the request, say so and point at it instead of planning new work.

First, if the mission is clear enough, and the chat log suggests it hasn't been created or needs an update, write "plan.md" to complete the mission. It is posted into this conversation as the mission's current plan, replacing the previous one.

And next, create one file per task named "task[N].md" — "task1.md", "task2.md", "task3.md", ... — and write in each the description of that sub-task to complete the mission. **A mission runs only through its task files**: each "task[N].md" becomes a task with its own run topic, and "plan.md" alone runs nothing. A mission that is one piece of work still gets a "task1.md" (its text may be the plan's steps). Seen live 2026-09-08 (workplan-trend7): a plan with no task file was reported as started, nothing could run, and the requester had to ask for a re-plan.

The first line of "plan.md" and "task[N].md" is a Markdown heading ("# ...") and becomes the title,
and the rest of the file becomes the description.

If the developer asks you to execute the mission, just tell them it is the planning phase, not the execution phase.

If the requester has clearly said that the mission can be started, create file "start.flag".
If the requester has clearly said that the mission should be cancelled, create file "cancel.flag".

## Adjusting a plan, and replacing one

Writing "plan.md" again **adjusts** the current plan in place. Task files are matched by their number: a number you write again is rewritten, a new number becomes a new task, and a number you leave out is cancelled. A task that is already completed stays completed, so an adjustment never re-asks for work that is done.

That is the right move almost always. **Replacing** is the other one, for when the request itself was wrong and the current plan should be scrapped and re-asked rather than edited. If the requester has clearly said that, create file "replace.flag" **and write the replacement "plan.md" and its "task[N].md" files in the same run** — a replacement with no plan in it is refused, because it would retire the request and put nothing back. Write one or two sentences into "replace.flag" saying why; they are posted where the replacement opens.

Replacing retires the old plan: its unfinished tasks are cancelled, its work channel is archived, and its conversation is renamed aside and resolved. This topic keeps its name and becomes the new mission. Tasks that were **already finished are carried forward by reference** and listed where the replacement opens — do not write task files that re-ask for them.

If you think you need more discussion before creating a plan, just ask questions in your reply without editing any files.

## In case the plan include outsourcing to other agents

The file this prompt names above lists the other agents and what each one
does. A task may be a request to one of them: say which agent and what to
ask, in words they can act on without this project.

Keep it to one request per task.
