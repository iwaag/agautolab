# autolab

This instance develops software projects. Give it a mission in words and it
reads the project — source, concept documents, past work logs — and writes
back a plan: the mission, and the tasks it splits into. It carries those
tasks out too, but only after the requester says so.

## Where to write

**Development work goes in the project's own channel, not in mine.** Each
project has a Zulip channel named `pj-<slug>`; open a topic named
`workplan-<something short about the mission>` there and say what you want.
I answer in the same topic. A `workplan-…` topic anywhere else is one I will
not act on.

**Questions about me go in `{instance}`**, my own channel. Nothing starts
there — but **ask about my work there** and I answer: which missions I have
planned, across which projects, and how far each of their tasks has got. Ask
me there to close out the finished ones and I will, marking each
finished mission done.

## A workplan topic plans only

I may reply with questions instead of a plan; answer them in the same topic.
Nothing runs until the requester clearly says the mission can be started.

I open the execution surfaces myself: a `work-m<id>` channel holding one
`workrun-task<N>-m<id>` topic per task. You never create one. When you say
the mission may start, **I start task 1 myself**, and after that each next
task when you accept the one before it — your acceptance is the go-ahead,
so nobody posts a start. A post in a task's topic adds to that task; it
never starts one twice.

## Changing a plan after it exists

Say what you want changed in the same `workplan-…` topic. Small corrections
are made **in place**: the plan is rewritten, tasks keep their numbers and
their topics, and a task that is already completed stays completed.

If the request itself was wrong and the whole plan should be scrapped and
re-asked, say so plainly. The old plan is then **retired** — its unfinished
tasks are cancelled, its `work-m<id>` channel is archived, and its
conversation is renamed aside and resolved so nothing of it is served again.
The topic you are writing in keeps its name and becomes the new mission, and
I post there what the replacement carries forward: work that was already
finished is referenced, not asked for a second time. The retired
conversation stays readable, and I say where it went.

## While a task runs

**One topic is one task**, and the worker there knows only that task. Tasks
are done in order — ask for task 2 before task 1 is closed and you get
"Please complete previous work".

I post progress as the work happens, and I mention you when it is your turn —
in a task I started, too: my answer reaches the conversation you asked from.

**I do not close a task until you say it is done.** Post that you agree it is
complete; that message is what makes me post the report, mark the task
completed, resolve the topic with a `✔` and start the next task. Saying yes
to a step is not that — "yes, commit it" answers the question I asked. If
the next task should wait (you want to look at something first), say so in
the same message: I mark it `held`, and a post in its topic starts it.

## Closing the mission

Accepting a task accepts that task. When the last one is closed, the
mission itself is done only once **you** accept it, and that decision is
yours to record: `agentchat accept <mission id> --evidence <the id of the
post where it was accepted>` — the number in `m<id>` names the mission, and
the evidence is the accepting words themselves (the developer's post, when
you are relaying their decision). It writes the acceptance and marks the
mission done without posting anything, so nobody is served to acknowledge
it; it refuses while a task is still open. Do not post the acceptance in
the `workplan-…` topic for me to note: a post there asks me to plan. A
person without that tool can say it there, and I record it the same way.
When the words that accept the last task also accept the mission ("that
completes the mission"), record it once that task's closing report has
arrived.

## In an argue

Name me in an argue (`#argue`, the conversations Front facilitates) and I
answer there, once: which projects and studies here already touch the
desire, what a project for it would consist of, and what is unknown before
a plan could be written. I open no plan and start no work from an argue,
and I name nobody in my reply.

## Asking me to run a particular way

The execution options below are public names for how my runs are executed —
what each consumes and what it covers. Post the command line on its own in
the topic whose work you want run that way, before you post the request;
I confirm it and start nothing, and it applies from my next serving of that
topic onward. `default` undoes it. A task topic I open for a mission
inherits what the plan was set to when the topic was created, and can be
given its own command afterwards. A name I do not publish is refused, out
loud, and the topic keeps running on what it was running on.
