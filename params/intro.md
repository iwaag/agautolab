# autolab

This instance develops software projects. Give it a mission in words and it
reads the project — its source, its concept documents, its past work logs —
and writes back a plan: what the mission is, and the tasks it splits into.
It carries those tasks out too, but only after the requester says so.

## Where to write

**Development work goes in the project's own channel, not in mine.** Each
project has a Zulip channel named `pj-<slug>`. Open a topic named
`workplan-<something short about the mission>` there and describe what you
want, in your own words. That is the whole request; I answer in the same
topic.

The channel is how I know which project the work is for — there is no other
place that says it. So a `workplan-…` topic anywhere else has no project to
plan against, and I will not act on it.

**Questions about me go in `{instance}`**, my own channel: what I can
do, whether I am working on something, why something did not happen. Nothing
is ever started from there.

## Choosing the project

Usually the person asking names the project, and the channel is `pj-` plus
that name — a request about a project called `zoo` goes in `pj-zoo`.

If you do not know which project is meant, look at what `pj-…` channels
exist, or ask whoever asked you. Do not guess a channel: a `pj-` channel I am
not subscribed to is a project nobody has asked me to work on, and a topic
there is a message into an empty room.

## Planning is not execution

A `workplan-…` topic **plans only**. I may reply with questions instead of a
plan if the mission is not clear enough yet — answer them in the same topic.
Nothing runs until the requester clearly says the mission can be started.

When it is started, I open the execution surfaces myself: a `work-<label>`
channel holding one `workrun-task<N>-<label>` topic per task. Those are mine
to open and mine to work in. You never need to create one, and posting into
one starts real work.

## While a task runs, somebody has to be there

Whoever posts into a `workrun-…` topic is that task's supervisor until it is
finished, and it is not a fire-and-forget request.

**One topic is one task, and the worker answering there knows only that
task.** A post into `workrun-task1-…` starts task 1 and nothing else; telling
that worker to "go on with tasks 2 and 3" reaches nobody who could act on it.
To run three tasks, post into three topics, one after another, and supervise
each in its own topic.

- I post progress into the topic as the work happens, and the outcome when
  that stretch of work ends. Reading the topic again is how you see it.
- I may ask you something — a decision I cannot make from the project alone.
  Answer in the same topic; nothing moves until you do.
- **I do not close a task until you say it is done.** When the work looks
  finished to you, post that you agree it is complete — in those words, or
  close to them. That message is what makes me write the report, mark the
  task Done and resolve the topic with a `✔`. Without it the topic simply
  stays open, however good the work was.
- Saying yes to a step is not saying the task is done. "Yes, commit it"
  answers the question I asked; it does not tell me the task is complete.
  Keep the two apart: approve steps as they come, and say "this task is
  complete" once, when it is.
- Tasks are done in order. Ask for task 2 before task 1 is closed and you get
  "Please complete previous work" and nothing else — that is a queue, not a
  failure.

A stretch of work can take many minutes, so expect to wait rather than to be
answered at once.

**A task may be a delegation.** If the plan says a task's job is to ask
another agent for something — a media asset, say — then that task's whole
work is the conversation with that agent, and its result is posted back into
its own `workrun-…` topic. Those run longest of all, because two agents are
talking; the topic is still where you watch it, and it still does not close
until you say it is done.
