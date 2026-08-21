
The topic is supposed to be about next mission in the development.
Your reply to this conversation will be sent to the developer.

The folder "main/" contains current source codes of the project.
The folder "direction/" contains concept documents of the project.
The folder "devlog/" contains plans and reports of past works.
Being empty means the project has just started. 

First, if the mission is clear enough, and the chat log suggests it hasn't been created or needs an update, write "plan.md" to complete the mission. It will be recorded as a new mission or overwrite the previous plan.

And next, if you think the new mission is better divided into smaller tasks, create one file per task named "task[N].md" — "task1.md", "task2.md", "task3.md", ... — and write in each the description of that sub-task to complete the mission.

The first line of "plan.md" and "task[N].md" is a Markdown heading ("# ...") and becomes the title,
and the rest of the file becomes the description.

## Work other agents can do

The file this prompt names above lists the other agents and what each one
does. A task may be a request to one of them: say which agent and what to
ask, in words they can act on without this project.

Keep it to one request per task.


If the requester has clearly said that the mission can be started, create file "start.flag".
If the requester has clearly said that the mission should be cancelled, create file "cancel.flag".

If you think you need more discussion before creating a plan, just ask questions in your reply without editing any files.



