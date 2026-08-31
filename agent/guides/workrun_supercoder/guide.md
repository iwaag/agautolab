
The topic is supposed to be about work to do in this session.
Your reply to this conversation will be sent to the chat.

The file "README_PROJECT.md" explains how the folders inside the workspace are supposed to work.

Do the work following developer's request.

To create an agag agent, `agag init <name> --yes --provision --like <sibling-root>` generates it and provisions its Zulip identity; `agag --help` is the usage reference.

If the developer agreed that the task was done, create "report.md" in the workspace directory this prompt names above. Also, commit changes in the repository folders you edited after getting the developer's approval, checking and editing their ".gitignore" to avoid committing unnecessary files. "README_PROJECT.md" says which folders are repositories and whether any of them must not be pushed.


## When the task is to ask another agent

The introductions file this prompt names above says how to reach each agent
and what it calls finished. Talk with them using `agentchat` (`--help`
explains it).

Post the request or reply and finish. You will be called again when they answer, and
the result goes into this task's own topic.

When a ComfyUI generation takes minutes, do not wait for it. Submit it, post
`@**Comfy Notifier** watch <prompt_id>` **in this topic** as a normal message,
record in your report what is pending and what to do with its result, then
finish. The notifier reacts to your command, and posts back here when the job
ends — two lines naming the state and the `prompt_id`; read
`GET /history/<prompt_id>` yourself for the outputs. Public-channel topics
only. When *quoting* the command rather than issuing it, put it in a code
fence.
