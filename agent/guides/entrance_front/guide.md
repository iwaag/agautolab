autolab's work is not in this channel: it is in the project channels and
their execution channels.

- `agentchat channels --prefix pj-` are the projects and studies;
  `agentchat topics <pj-channel>` shows their missions as `workplan-` topics.
- `agentchat channels --prefix work-` are the execution channels; each
  description names the project and the mission it belongs to, and
  `agentchat topics <work-channel>` shows one `workrun-task<N>-…` topic per
  task.
- Asked where your plans stand, list **every** `pj-` channel and look in
  each: a project you did not look at is one you cannot report on
  (agent_standardize p10: an answer from `pj-simpleshooter` alone missed the
  finished mission in `pj-runsmoke1`).
- Development work is not started here: it goes in a `workplan-…` topic in
  the project's own `pj-<slug>` channel.
- Closing out finished work also marks its missions done:
  `uv run python -m agautolab.mission_done` (its `--help` says what it
  records), after the topics are resolved.
