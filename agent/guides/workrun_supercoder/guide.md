
The topic is supposed to be about work to do in this session.
Your reply to this conversation will be sent to the chat.

The folder "main/" contains current source codes of the project, which is likely your main interest to complete the work.
The folder "direction/" contains concept documents of the project,
 which you only read when you really need to check the concept of the project.
The folder "devlog/" contains plans and reports of past works, which you only read when you need to read log of the development.
Being empty means the project has just started. 

Do the work following developer's request.

## When the task is to ask another agent

If the task says to ask another agent for something, that request is the work
of this task. The file of introductions this prompt names above is what each
agent says about itself — how to reach it, what it needs to be told, and what
it calls finished. Read the one you need and follow it; nothing about that
agent is written down here.

Talk to them with `agentchat` (`agentchat --help` explains it). It speaks as
this instance, so what you post is attributable to it.

Delegating is a supervision, not a message you send and forget:

- Post the request where that agent's introduction says to, in plain words.
  Everything it has to know goes in what you post — it cannot read this
  project.
- Wait for the reply **in this run**, blocking (`agentchat wait`), not in the
  background. It can take many minutes.
- It will ask you things. Answer in the same topic and keep waiting. You are
  the one who decides, on this project's behalf, what the answer is.
- Read its introduction for how a request of that agent is actually finished —
  some need a further step from you before anything is produced — and do that
  step.
- If your run ends while you are still waiting, `agentchat read --since`
  recovers what you missed: the topic is the memory of the conversation.

When the result arrives, bring it back into this task's own topic — post the
URL, or whatever the deliverable was — and put it into the project if the task
asks for that.

Then report as any other task does. The developer is the one who says the task
is complete; delivering the result does not close it.

If the developer agreed that the task was done, create "report.md" in the workspace directory this prompt names above. Also, commit changes in "main/" after getting developer's approval, checking and editing "main/.gitignore" to avoid comitting unnecessary files.
