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
- **Delivery** (protocol 2, p5/ex1). One topic per run. The editor freezes
  each message — its events, text, marker line and destination — when it is
  first claimed, and never changes it; events that arrive later wait for the
  next message. The transport records `begin` before every POST, so a crash
  after posting cannot leave a message looking unattempted. A message that
  may have been posted (answer lost, a begun send never finished, a failed
  acknowledgment) is looked for — the sender's own messages in every place
  it was sent to and where its topic moved, page by page back to before its
  first send — and is acknowledged when found. Only a search that covered
  all of that lets the same payload go out again; a search that could not
  (an error, a channel the bot cannot read) keeps it uncertain and visible.
  Exactly-once is not claimed: a message deleted by hand between the post
  and the search would be posted again.
- **One sender.** Claims carry this process's identity; while it lives,
  another notifier is refused (`wfe notify claim`), and it takes over only
  once this process is gone. The pre-p5/ex1 `.local/wfnotify/inflight.json`
  is handed to the editor once (`wfe notify import-inflight`) and kept as
  `inflight.json.retired-<time>`; the store is the only record of uncertainty.
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
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from agag.zulip import ZulipClient, ZulipError, ZulipRejected

from agautolab.instance import AGAUTOLAB_ROOT, PROVISIONER_ENV, SPEC

INTERVAL_SECONDS = 5.0
# Pre-p5/ex1 record of one send in flight; read once, then retired.
INFLIGHT = AGAUTOLAB_ROOT / ".local" / "wfnotify" / "inflight.json"
PROTOCOL = 2  # `wfe notify protocol` must say the same
SEARCH_PAGE = 200
# Clock difference allowed between this host and Zulip when the search
# stops at messages older than a delivery's first send.
SEARCH_SLACK_SECONDS = 15 * 60
SEARCH_PAGES_MAX = 500


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
                 env: dict[str, str] | None = None, inflight: Path = INFLIGHT, log=print, pid: int | None = None):
        self.wfe_argv = wfe
        self.client = client
        self.admin = admin
        self.env = env or {}
        self.inflight = inflight
        self.log = log
        # The sender identity: this process. Another process is refused while
        # this one lives, whatever it is doing (a request may be in flight).
        self.pid = pid or os.getpid()
        self.owner = f"{socket.gethostname()}:{self.pid}"
        self._self_id: int | None = None
        self._ready = False
        self._refused: str | None = None

    # ---- wfe ------------------------------------------------------------------

    def wfe(self, *args: str) -> dict:
        proc = subprocess.run([*self.wfe_argv, *args, "--json"], capture_output=True, text=True, timeout=300, env={**os.environ, **self.env})
        out = proc.stdout.strip()
        if proc.returncode != 0:
            raise RuntimeError(f"wfe {' '.join(args[:2])} failed ({proc.returncode}): {proc.stderr.strip() or out}")
        return json.loads(out) if out else {}

    @property
    def self_id(self) -> int:
        if self._self_id is None:
            self._self_id = int(self.client.whoami()["user_id"])
        return self._self_id

    # ---- start ----------------------------------------------------------------------

    def prepare(self) -> None:
        """Once per process: the editor speaks this protocol, and the old
        transport's in-flight record is in the store, not in a file."""
        if self._ready:
            return
        spoken = self.wfe("notify", "protocol").get("protocol")
        if spoken != PROTOCOL:
            raise RuntimeError(f"the workflow editor speaks notify protocol {spoken}, this transport {PROTOCOL}; deploy matching releases (nothing is sent meanwhile)")
        try:
            legacy = json.loads(self.inflight.read_text())
        except FileNotFoundError:
            legacy = None
        except (OSError, ValueError) as error:
            raise RuntimeError(f"{self.inflight} cannot be read ({error}); move it aside after checking it, nothing is sent until then") from error
        if legacy is not None:
            ids = [str(i) for i in legacy.get("ids") or []]
            if ids:
                result = self.wfe("notify", "import-inflight", *[a for i in ids for a in ("--id", i)], *(["--marker", str(legacy["marker"])] if legacy.get("marker") else []))
                self.log(f"inflight.json handed to the store: {'; '.join(result.get('notes', []))}")
            retired = self.inflight.with_name(f"{self.inflight.name}.retired-{time.strftime('%Y%m%dT%H%M%S')}")
            self.inflight.replace(retired)
            self.log(f"{self.inflight.name} retired as {retired.name}")
        self._ready = True

    # ---- one pass ----------------------------------------------------------------

    def pass_once(self, rounds: int = 20) -> dict:
        """Provisions what is due and delivers what it claims, again while
        that makes progress: the editor hands out one message per run at a
        time (order), and a channel only after its folder."""
        self.prepare()
        done = {"provisioned": 0, "sent": 0, "failed": 0, "problems": []}
        for _ in range(rounds):
            due = self.wfe("notify", "due")
            if due.get("disabled"):
                return {"disabled": due["disabled"]}
            progress = 0
            for task in due.get("provision", []):
                try:
                    if self.provision(task):
                        done["provisioned"] += 1
                        progress += 1
                except ZulipError as error:
                    self.log(f"provisioning {task['key']} failed (retried next pass): {error}")
            claimed = self.wfe("notify", "claim", "--sender", self.owner, "--pid", str(self.pid))
            done["problems"] = claimed.get("problems", [])
            if claimed.get("refused"):
                if claimed["refused"] != self._refused:
                    self.log(f"not sending: {claimed['refused']}")
                self._refused = claimed["refused"]
                done["refused"] = claimed["refused"]
                return done
            self._refused = None
            for delivery in claimed.get("deliveries", []):
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

    @staticmethod
    def _dest_args(dest: dict) -> list[str]:
        return ["--stream", str(dest["streamId"]), "--channel", str(dest["channel"]), "--topic", str(dest["topic"])]

    def _held(self, d: dict) -> list[str]:
        return [str(d["delivery"]), "--claim", d["claim"], *[a for e in d["events"] for a in ("--event", e)]]

    def deliver(self, d: dict) -> bool:
        run, label = d["run"], f"delivery {d['delivery']} ({' '.join(e[e.index('#'):] for e in d['events'])})"
        dest = dict(d["destination"])
        # Follow the conversation if a person renamed the topic or moved it.
        if d.get("lastMessageId"):
            try:
                last = self.client.message(int(d["lastMessageId"]), strict=True)
            except ZulipError as error:
                return self._failed(d, f"cannot look up the run's last message: {error}", uncertain=bool(d.get("reconcile")))
            if last and last.get("type") == "stream":
                now = {"streamId": int(last["stream_id"]), "channel": str(last.get("display_recipient")), "topic": str(last.get("subject"))}
                if (now["streamId"], now["topic"]) != (int(dest["streamId"]), dest["topic"]):
                    self.wfe("notify", "moved", run, *self._dest_args(now))
                    self.log(f"{run}: the topic moved to {now['channel']!r} › {now['topic']!r}; following it")
                    dest = now
        if d["kind"] == "legacy":
            return self._legacy(d, dest)
        evidence = None
        if d.get("reconcile"):
            # The last attempt may have posted: look before posting again.
            found, evidence, error = self.search(d, dest)
            if error:
                return self._failed(d, f"cannot establish whether it was posted: {error}", uncertain=True)
            if found:
                where = {"streamId": int(found["stream_id"]), "channel": str(found.get("display_recipient")), "topic": str(found.get("subject"))}
                self._sent(d, int(found["id"]), where, found=True)
                self.log(f"{run}: {label} had been posted (message {found['id']}); recorded, not posted again")
                return True
        try:
            self.wfe("notify", "begin", *self._held(d), *self._dest_args(dest), *(["--checked-absent", evidence] if evidence else []))
        except RuntimeError as error:
            self.log(f"{run}: {label} not sent: {error}")
            return False
        try:
            result = self.client.call("POST", "messages", {"type": "stream", "to": int(dest["streamId"]), "topic": dest["topic"], "content": d["content"]})
            message_id = int(result["id"])
        except ZulipRejected as error:
            text = str(error)
            if any(w in text.lower() for w in ("channel", "stream")):
                key = f"channel:{run.rsplit('/', 1)[0]}"
                self.wfe("notify", "target", key, "--problem", f"channel #{dest['streamId']} refused a message ({text}); restore it or the bot's access, or run `wfe notify reset {key}` to open a new one")
            return self._failed(d, f"refused: {text}", uncertain=False)
        except (ZulipError, OSError) as error:
            return self._failed(d, f"no answer: {error}", uncertain=True)
        if not self._sent(d, message_id, dest):
            return False
        self.log(f"{run}: sent {label} as message {message_id}")
        return True

    def _sent(self, d: dict, message_id: int, dest: dict, *, found: bool = False) -> bool:
        try:
            self.wfe("notify", "sent", *self._held(d), "--message", str(message_id), *self._dest_args(dest), *(["--found"] if found else []))
            return True
        except RuntimeError as error:
            # Posted, but not recorded: never posted again from here. The
            # delivery stays begun, so the next claim looks for it first.
            self.log(f"{d['run']}: delivery {d['delivery']} is message {message_id} but recording that failed ({error}); it will be found by its marker")
            try:
                self.wfe("notify", "failed", *self._held(d), "--error", f"posted as message {message_id}; recording it failed: {error}", "--uncertain")
            except RuntimeError:
                pass
            return False

    def search(self, d: dict, current: dict) -> tuple[dict | None, str | None, str | None]:
        """The sender's message whose last line is the delivery's marker, in
        every place the delivery may be. Returns (message, evidence of
        absence, error): an error means absence is not established."""
        rec = d["reconcile"]
        places = [current, *rec["searchIn"]]
        evidence = []
        seen: set[tuple[int, str]] = set()
        floor = _epoch(rec["since"]) - SEARCH_SLACK_SECONDS
        for place in places:
            key = (int(place["streamId"]), str(place["topic"]))
            if key in seen:
                continue
            seen.add(key)
            try:
                count, reached, match = self._scan(place, floor, lambda m: _last_line(m) == d["marker"])
            except ZulipError as error:
                return None, None, f"#{key[0]} › {key[1]}: {error}"
            if match:
                return match, None, None
            if not reached:
                return None, None, f"#{key[0]} › {key[1]}: history not covered after {count} message(s)"
            evidence.append(f"#{key[0]} › {key[1]}: {count} of the sender's message(s) back to {rec['since']} (−{SEARCH_SLACK_SECONDS // 60} min)")
        return None, "absent from " + "; ".join(evidence), None

    def _scan(self, place: dict, floor: float, want) -> tuple[int, bool, dict | None]:
        """Pages back through the sender's messages in one topic. Returns
        (messages read, whether the history before `floor` was reached, the
        first wanted message)."""
        narrow = [{"operator": "channel", "operand": int(place["streamId"])}, {"operator": "topic", "operand": str(place["topic"])},
                  {"operator": "sender", "operand": self.self_id}]
        anchor: int | str = "newest"
        count = 0
        for _ in range(SEARCH_PAGES_MAX):
            page = self.client.messages_page(narrow, anchor=anchor, num_before=SEARCH_PAGE, include_anchor=anchor == "newest")
            messages = page.get("messages", [])
            count += len(messages)
            for m in reversed(messages):
                if int(m.get("sender_id", 0)) == self.self_id and want(m):
                    return count, True, m
            if page.get("found_oldest") or not messages:
                return count, True, None
            if min(float(m.get("timestamp", 0)) for m in messages) < floor:
                return count, True, None
            anchor = min(int(m["id"]) for m in messages)
        return count, False, None

    def _legacy(self, d: dict, dest: dict) -> bool:
        """Pre-p5/ex1 uncertain events: the posts found name the events they
        carried. Every place searched completely → the rest were never posted."""
        rec = d["reconcile"]
        prefix = f"ref {rec['label']} "
        tokens = rec["tokens"]
        found: dict[str, dict] = {}
        floor = _epoch(rec["since"]) - SEARCH_SLACK_SECONDS
        error = None
        seen: set[tuple[int, str]] = set()

        def carried(m: dict) -> bool:
            line = _last_line(m)
            if not line.startswith(prefix):
                return False
            words = line[len(prefix):].split()
            for t in tokens:
                hit = t["token"] in words or (t["token"].endswith(":published") and "published" in words)
                if hit and t["event"] not in found:
                    found[t["event"]] = m
            return False  # read every message

        for place in [dest, *rec["searchIn"]]:
            key = (int(place["streamId"]), str(place["topic"]))
            if key in seen:
                continue
            seen.add(key)
            try:
                count, reached, _ = self._scan(place, floor, carried)
            except ZulipError as failure:
                error = f"#{key[0]} › {key[1]}: {failure}"
                break
            if not reached:
                error = f"#{key[0]} › {key[1]}: history not covered after {count} message(s)"
                break
        payload = [{"event": e, "message": int(m["id"]), "stream": int(m["stream_id"]), "channel": str(m.get("display_recipient")), "topic": str(m.get("subject"))} for e, m in found.items()]
        try:
            self.wfe("notify", "legacy", str(d["delivery"]), "--claim", d["claim"], "--found-json", json.dumps(payload),
                     *(["--complete"] if error is None else ["--error", f"cannot establish which events were posted: {error}"]))
        except RuntimeError as failure:
            self.log(f"{d['run']}: legacy reconciliation {d['delivery']} not recorded: {failure}")
            return False
        self.log(f"{d['run']}: legacy reconciliation {d['delivery']}: {len(found)} of {len(tokens)} event(s) found posted" + (f"; incomplete: {error}" if error else "; the rest go out again"))
        return error is None

    def _failed(self, d: dict, error: str, *, uncertain: bool) -> bool:
        try:
            self.wfe("notify", "failed", *self._held(d), "--error", error, *(["--uncertain"] if uncertain else []))
        except RuntimeError as failure:
            self.log(f"could not record the failure of delivery {d['delivery']}: {failure}")
        self.log(f"{d['run']}: delivery {d['delivery']}: {error}{' (it may have been posted; looked for before the retry)' if uncertain else ''}")
        return False

    # ---- running ---------------------------------------------------------------------

    def run_forever(self, stop: threading.Event | None = None, interval: float = INTERVAL_SECONDS) -> None:
        stop = stop or threading.Event()
        self.log(f"workflow-run coordination: delivering to Zulip every {interval:g} s as sender {self.owner}")
        last_error = None
        while not stop.is_set():
            try:
                result = self.pass_once()
                last_error = None
                if result.get("sent") or result.get("provisioned") or result.get("failed"):
                    self.log(f"notify pass: {result}")
            except Exception as error:  # noqa: BLE001 - keep delivering; the next pass retries
                if str(error) != last_error:  # a standing failure is logged once
                    self.log(f"notify pass failed: {error}")
                last_error = str(error)
            stop.wait(interval)


def _last_line(message: dict) -> str:
    return str(message.get("content", "")).rstrip().split("\n")[-1].strip()


def _epoch(iso: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


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
