"""Zulip delivery of workflow-run coordination (workflow_editor p5).

The workflow editor decides what is owed — which events of which runs, in
what order, with what words, to which folder, channel and topic — and keeps
it durably (`wfe notify …`, `server/notify.ts`). This module is only the
transport: it asks `wfe notify due`, does the Zulip calls with the existing
client, and reports each result back. It decides nothing about runs.

- **Provisioning.** A project's channel folder and a workflow's channel are
  created when the editor asks for them, with the realm owners and this bot
  subscribed. An existing folder or channel of the same name is adopted only
  when its description is the one this integration writes; anything else is
  reported as a problem with its repair path, never taken over by name.
- **Delivery.** One topic per run. Before posting into a topic it has used,
  the transport looks up its last message there and follows a rename or a
  move. A send whose answer was lost — or that was in flight when the
  process died (`.local/wfnotify/inflight.json`) — is looked for by its
  marker line before anything is posted again, so a lost response does not
  become a duplicate. Exactly-once is not claimed: a message deleted by hand
  between the post and the search would be posted again.
- **Separation.** Messages mention nobody. Run topics (`run-…`) in `wf-…`
  channels match no listener's prefixes, so no legacy listener serves
  them; Zulip replies answer nothing — answers are recorded in agdevworld.

It runs on a thread of `agautolab.wfexec` (independent of the agent and of
the execution slot, so it keeps delivering while a run waits for a person
and while the queue is idle), or alone: `uv run python -m agautolab.wfnotify
[--once]`.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from agag.zulip import ZulipClient, ZulipError, ZulipRejected

from agautolab.instance import AGAUTOLAB_ROOT, PROVISIONER_ENV, SPEC

INTERVAL_SECONDS = 5.0
INFLIGHT = AGAUTOLAB_ROOT / ".local" / "wfnotify" / "inflight.json"


def owns(kind: str, description: str, key: str) -> bool:
    """Whether a folder or channel description is the one `wfe notify`
    asked for — the same words, so a channel this integration made but did
    not record (a crash between the two) is recognized again."""
    project, _, workflow = key.split(":", 1)[1].partition("/")
    if kind == "folder":
        return description.startswith(f"[wfe] project {project}:") or description.startswith(f"[wfe] project {project} (")
    return description.startswith(f"[wfe] project {project} · workflow {workflow}:")


class Notifier:
    def __init__(self, wfe: list[str], client: ZulipClient, admin: ZulipClient | None = None, *,
                 env: dict[str, str] | None = None, inflight: Path = INFLIGHT, log=print):
        self.wfe_argv = wfe
        self.client = client
        self.admin = admin
        self.env = env or {}
        self.inflight = inflight
        self.log = log
        self._self_id: int | None = None

    # ---- wfe ------------------------------------------------------------------

    def wfe(self, *args: str) -> dict:
        proc = subprocess.run([*self.wfe_argv, *args, "--json"], capture_output=True, text=True, timeout=300, env={**os.environ, **self.env})
        out = proc.stdout.strip()
        if proc.returncode != 0:
            raise RuntimeError(f"wfe {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip() or out}")
        return json.loads(out) if out else {}

    @property
    def self_id(self) -> int:
        if self._self_id is None:
            self._self_id = int(self.client.whoami()["user_id"])
        return self._self_id

    # ---- one pass ----------------------------------------------------------------

    def pass_once(self, rounds: int = 20) -> dict:
        """Asks what is due and does it, again while that makes progress:
        the editor hands out one message per run at a time (order), and a
        channel only after its folder."""
        done = {"provisioned": 0, "sent": 0, "failed": 0, "problems": []}
        for _ in range(rounds):
            due = self.wfe("notify", "due")
            if due.get("disabled"):
                return {"disabled": due["disabled"]}
            done["problems"] = due.get("problems", [])
            progress = 0
            for task in due.get("provision", []):
                try:
                    if self.provision(task):
                        done["provisioned"] += 1
                        progress += 1
                except ZulipError as error:
                    self.log(f"provisioning {task['key']} failed (retried next pass): {error}")
            for delivery in due.get("deliveries", []):
                if self.deliver(delivery):
                    done["sent"] += 1
                    progress += 1
                else:
                    done["failed"] += 1
            if not progress:
                break
        return done

    # ---- provisioning -------------------------------------------------------------

    def provision(self, task: dict) -> bool:
        key, name, description = task["key"], task["name"], task["description"]
        if task["kind"] == "folder":
            client = self.admin or self.client
            existing = [f for f in client.channel_folders() if f.get("name") == name]
            if existing:
                folder = existing[0]
                if not owns("folder", str(folder.get("description", "")), key):
                    return self._problem(key, f"the folder name {name!r} is taken by folder #{folder.get('id')} that this integration did not create; rename that folder (the next pass creates ours) or file this project by hand and run `wfe notify reset {key}`")
                self.wfe("notify", "target", key, "--zulip-id", str(folder["id"]))
                self.log(f"{key}: adopted folder #{folder['id']} {name!r} (its description is ours)")
                return True
            folder_id = client.create_channel_folder(name, description)
            self.wfe("notify", "target", key, "--zulip-id", str(folder_id))
            self.log(f"{key}: created folder #{folder_id} {name!r}")
            return True
        rows = [c for c in self.client.channels(include_archived=True) if c.get("name") == name]
        if rows:
            channel = rows[0]
            if not owns("channel", str(channel.get("description", "")), key):
                return self._problem(key, f"the channel name {name!r} is taken by channel #{channel.get('stream_id')} that this integration did not create; rename that channel (the next pass creates ours), then nothing else is needed")
            if channel.get("is_archived"):
                return self._problem(key, f"our channel #{channel.get('stream_id')} {name!r} is archived; unarchive it in Zulip (the next pass uses it again)")
            stream_id = int(channel["stream_id"])
            if channel.get("folder_id") != task["folderId"]:
                (self.admin or self.client).set_channel_folder(stream_id, int(task["folderId"]))
            self.wfe("notify", "target", key, "--zulip-id", str(stream_id))
            self.log(f"{key}: adopted channel #{stream_id} {name!r} (its description is ours)")
            return True
        # The people who own the realm see it, and so does this bot.
        principals = sorted({*(self.admin or self.client).realm_owners(), self.self_id})
        self.client.create_channel(name, description, principals, folder_id=int(task["folderId"]))
        made = [c for c in self.client.channels() if c.get("name") == name]
        if not made:
            return self._problem(key, f"channel {name!r} was requested but is not visible afterwards")
        stream_id = int(made[0]["stream_id"])
        self.wfe("notify", "target", key, "--zulip-id", str(stream_id))
        self.log(f"{key}: created channel #{stream_id} {name!r}")
        return True

    def _problem(self, key: str, problem: str) -> bool:
        self.wfe("notify", "target", key, "--problem", problem)
        self.log(f"{key}: {problem}")
        return False

    # ---- delivery ---------------------------------------------------------------------

    def _read_inflight(self) -> dict | None:
        try:
            return json.loads(self.inflight.read_text())
        except (OSError, ValueError):
            return None

    def _write_inflight(self, value: dict | None) -> None:
        if value is None:
            self.inflight.unlink(missing_ok=True)
            return
        self.inflight.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.inflight.with_suffix(".tmp")
        tmp.write_text(json.dumps(value))
        tmp.replace(self.inflight)

    def _sent(self, d: dict, message_id: int, stream_id: int, channel: str, topic: str) -> None:
        self.wfe("notify", "sent", *[a for i in d["ids"] for a in ("--id", i)], "--message", str(message_id),
                 "--stream", str(stream_id), "--channel", channel, "--topic", topic)
        self._write_inflight(None)

    def deliver(self, d: dict) -> bool:
        ids, run = d["ids"], d["run"]
        stream_id, channel, topic = int(d["streamId"]), str(d["channel"]), str(d["topic"])
        # Follow the conversation if a person renamed the topic or moved it.
        if d.get("lastMessageId"):
            try:
                last = self.client.message(int(d["lastMessageId"]), strict=True)
            except ZulipError as error:
                return self._failed(ids, f"cannot look up the run's last message: {error}", uncertain=bool(d.get("uncertain")))
            if last and last.get("type") == "stream":
                now = (int(last["stream_id"]), str(last.get("display_recipient")), str(last.get("subject")))
                if (now[0], now[2]) != (stream_id, topic):
                    self.wfe("notify", "moved", run, "--stream", str(now[0]), "--channel", now[1], "--topic", now[2])
                    self.log(f"{run}: the topic moved to {now[1]!r} › {now[2]!r}; following it")
                    stream_id, channel, topic = now
        inflight = self._read_inflight()
        uncertain = bool(d.get("uncertain")) or bool(inflight and inflight.get("ids") == ids)
        if uncertain:
            # The last attempt may have posted: look before posting again.
            try:
                history = self.client.topic_history(channel, topic, num_before=100)
            except ZulipError as error:
                return self._failed(ids, f"cannot check whether the earlier attempt posted: {error}", uncertain=True)
            for message in reversed(history):
                if int(message.get("sender_id", 0)) == self.self_id and str(message.get("content", "")).rstrip().endswith(d["marker"]):
                    self._sent(d, int(message["id"]), stream_id, channel, topic)
                    self.log(f"{run}: {', '.join(ids)} had been posted (message {message['id']}); recorded, not posted again")
                    return True
        self._write_inflight({"ids": ids, "marker": d["marker"], "at": time.time()})
        try:
            result = self.client.call("POST", "messages", {"type": "stream", "to": stream_id, "topic": topic, "content": d["content"]})
            message_id = int(result["id"])
        except ZulipRejected as error:
            self._write_inflight(None)  # refused: nothing was posted
            text = str(error)
            if any(w in text.lower() for w in ("channel", "stream")):
                key = f"channel:{run.rsplit('/', 1)[0]}"
                self.wfe("notify", "target", key, "--problem", f"channel #{stream_id} refused a message ({text}); restore it or the bot's access, or run `wfe notify reset {key}` to open a new one")
            return self._failed(ids, f"refused: {text}", uncertain=False)
        except (ZulipError, OSError) as error:
            return self._failed(ids, f"no answer: {error}", uncertain=True)
        self._sent(d, message_id, stream_id, channel, topic)
        self.log(f"{run}: sent {', '.join(ids)} as message {message_id}")
        return True

    def _failed(self, ids: list[str], error: str, *, uncertain: bool) -> bool:
        try:
            self.wfe("notify", "failed", *[a for i in ids for a in ("--id", i)], "--error", error, *(["--uncertain"] if uncertain else []))
        except RuntimeError as failure:
            self.log(f"could not record the failure of {ids}: {failure}")
        self.log(f"{', '.join(ids)}: {error}{' (it may have been posted; checked before the retry)' if uncertain else ''}")
        return False

    # ---- running ---------------------------------------------------------------------

    def run_forever(self, stop: threading.Event | None = None, interval: float = INTERVAL_SECONDS) -> None:
        stop = stop or threading.Event()
        self.log(f"workflow-run coordination: delivering to Zulip every {interval:g} s")
        while not stop.is_set():
            try:
                result = self.pass_once()
                if result.get("sent") or result.get("provisioned") or result.get("failed"):
                    self.log(f"notify pass: {result}")
            except Exception as error:  # noqa: BLE001 - keep delivering; the next pass retries
                self.log(f"notify pass failed: {error}")
            stop.wait(interval)


def from_settings(wfe: list[str], notify: dict, env: dict[str, str] | None = None, log=print) -> Notifier | None:
    """The notifier `.local/wfexec.toml`'s `[notify]` table asks for, or None."""
    if not notify or not notify.get("enabled"):
        return None
    zulip_env = Path(notify.get("zulip_env") or SPEC.zulip_env)
    admin_env = Path(notify.get("admin_env") or PROVISIONER_ENV)
    client = ZulipClient.from_env(zulip_env)
    admin = ZulipClient.from_env(admin_env) if admin_env.is_file() else None
    return Notifier(wfe, client, admin, env=env, log=log)


def main(argv: list[str] | None = None) -> int:
    from agautolab.wfexec import SETTINGS, Settings
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--settings", type=Path, default=SETTINGS)
    args = parser.parse_args(argv)
    s = Settings.load(args.settings)
    log = lambda m: print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {m}", flush=True)  # noqa: E731
    notifier = from_settings(s.wfe, s.notify or {"enabled": True}, env=s.env, log=log)
    if notifier is None:
        print("notify is not enabled", file=sys.stderr)
        return 1
    if args.once:
        print(json.dumps(notifier.pass_once()))
        return 0
    notifier.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
