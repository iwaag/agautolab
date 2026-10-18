"""Zulip intake of workflow requests (workflow_editor p6).

The workflow editor decides what a post means — which workflow a channel is,
whether a post is a request, an answer, an instruction or nothing — and keeps
it durably (`wfe intake …`, `server/intake.ts`, docs/intake.md). This module
is only the transport: it reads the routed channels with the existing Zulip
client and hands every post over, with the two facts the rules need and a
post does not carry:

- whether its sender is a bot (from the realm's user list);
- the first message of its topic, which is the topic's identity across
  renames (`topicFirst`).

**Cursor.** `wfe intake due` names each routed channel and the highest
message id already received there. Posts after it are read page by page
(oldest first) and handed over with `wfe intake receive`, which stores them
and moves the cursor in one transaction. A post read and not stored is read
again; one stored twice is stored once. The same reading recovers whatever
was posted while this process was down: there is no separate catch-up.

**Failures.** A Zulip error (network, authorization, rate limit) stops the
pass where it is — the cursor stays at the last stored post — and is recorded
with `wfe intake transport --error`, so it is visible in `wfe intake status`
and is retried by the next pass. Then `wfe intake process` settles what was
received (it needs no Zulip).

**Separation.** Only channels the editor routes are read: workflow channels
whose workflow declares `intake.zulip: requests` in a project the host
enables. Nothing here posts; replies to the person are the notifier's.

It runs on a thread of `agautolab.wfexec` (`[intake] enabled = true` in
`.local/wfexec.toml`), beside the notifier, so requests are received while
an agent runs or waits; or alone: `uv run python -m agautolab.wfintake [--once]`.
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

from agag.zulip import ZulipClient, ZulipError

from agautolab.instance import SPEC

INTERVAL_SECONDS = 5.0
PAGE = 100
PROTOCOL = 1  # `wfe intake protocol` must say the same
PAGES_PER_CHANNEL = 50  # per pass; the rest is read by the next pass


class Intake:
    def __init__(self, wfe: list[str], client: ZulipClient, *, env: dict[str, str] | None = None, log=print):
        self.wfe_argv = wfe
        self.client = client
        self.env = env or {}
        self.log = log
        self._bots: dict[int, bool] = {}
        self._ready = False
        self._last_error: str | None = None

    def wfe(self, *args: str, stdin: str | None = None) -> dict:
        proc = subprocess.run([*self.wfe_argv, *args, "--json"], input=stdin, capture_output=True, text=True, timeout=300, env={**os.environ, **self.env})
        out = proc.stdout.strip()
        if proc.returncode != 0:
            raise RuntimeError(f"wfe {' '.join(args[:2])} failed ({proc.returncode}): {proc.stderr.strip() or out}")
        return json.loads(out) if out else {}

    def prepare(self) -> None:
        if self._ready:
            return
        spoken = self.wfe("intake", "protocol").get("protocol")
        if spoken != PROTOCOL:
            raise RuntimeError(f"the workflow editor speaks intake protocol {spoken}, this transport {PROTOCOL}; deploy matching releases (nothing is received meanwhile)")
        self._ready = True

    # ---- the facts a post does not carry -------------------------------------------

    def is_bot(self, user_id: int) -> bool:
        if user_id not in self._bots:
            self._bots = {int(u["user_id"]): bool(u.get("is_bot")) for u in self.client.users()}
        # A sender the realm does not list (deactivated, or another realm's
        # mirror) is not treated as a person.
        return self._bots.get(user_id, True)

    def topic_first(self, stream_id: int, topic: str, cache: dict) -> int:
        key = (stream_id, topic)
        if key not in cache:
            page = self.client.messages_page([{"operator": "channel", "operand": stream_id}, {"operator": "topic", "operand": topic}],
                                             anchor="oldest", num_before=0, num_after=1)
            rows = page.get("messages") or []
            if not rows:
                raise ZulipError(f"topic {topic!r} in #{stream_id} has no first message")
            cache[key] = int(rows[0]["id"])
        return cache[key]

    def inbound(self, m: dict, realm: str, stream_id: int, cache: dict) -> dict:
        topic = str(m.get("subject", ""))
        return {
            "realm": realm, "id": int(m["id"]), "streamId": stream_id, "topic": topic,
            "sender": {"id": int(m["sender_id"]), "name": str(m.get("sender_full_name", "")), "email": str(m.get("sender_email", "")), "bot": self.is_bot(int(m["sender_id"]))},
            "content": str(m.get("content", "")), "timestamp": int(m.get("timestamp", 0)),
            "topicFirst": self.topic_first(stream_id, topic, cache),
        }

    # ---- one pass ------------------------------------------------------------------------

    def fetch(self, realm: str, channel: dict) -> int:
        """Reads one channel after its cursor and hands the posts over."""
        stream_id, cursor = int(channel["streamId"]), int(channel["cursor"])
        received = 0
        cache: dict = {}
        for _ in range(PAGES_PER_CHANNEL):
            page = self.client.messages_page([{"operator": "channel", "operand": stream_id}], anchor=cursor, num_before=0, num_after=PAGE, include_anchor=False)
            rows = sorted((m for m in page.get("messages") or [] if int(m["id"]) > cursor), key=lambda m: int(m["id"]))
            if rows:
                posts = [self.inbound(m, realm, stream_id, cache) for m in rows]
                through = int(rows[-1]["id"])
                result = self.wfe("intake", "receive", "--stream", str(stream_id), "--through", str(through), "--messages-json", "-", stdin=json.dumps(posts))
                received += int(result.get("stored", 0))
                cursor = through
            if page.get("found_newest") or not rows:
                break
        return received

    def pass_once(self) -> dict:
        self.prepare()
        due = self.wfe("intake", "due")
        if due.get("disabled"):
            return {"disabled": due["disabled"]}
        received, error = 0, None
        try:
            for channel in due.get("channels", []):
                received += self.fetch(str(due["realm"]), channel)
        except (ZulipError, OSError) as failure:
            error = str(failure)
        if error:
            self.wfe("intake", "transport", "--error", error)
            if error != self._last_error:
                self.log(f"intake: reading Zulip failed (kept, retried next pass): {error}")
        else:
            self.wfe("intake", "transport", "--ok", "--detail", f"{len(due.get('channels', []))} channel(s), {received} new post(s)")
        self._last_error = error
        notes = self.wfe("intake", "process").get("notes", [])
        for note in notes:
            self.log(f"intake: {note}")
        return {"received": received, "notes": notes, "problems": due.get("problems", []), **({"error": error} if error else {})}

    def run_forever(self, stop: threading.Event | None = None, interval: float = INTERVAL_SECONDS) -> None:
        stop = stop or threading.Event()
        self.log(f"workflow requests: reading routed Zulip channels every {interval:g} s")
        last = None
        while not stop.is_set():
            try:
                self.pass_once()
                last = None
            except Exception as error:  # noqa: BLE001 - keep receiving; the next pass retries
                if str(error) != last:
                    self.log(f"intake pass failed: {error}")
                last = str(error)
            stop.wait(interval)


def from_settings(wfe: list[str], intake: dict, env: dict[str, str] | None = None, log=print) -> Intake | None:
    """The intake `.local/wfexec.toml`'s `[intake]` table asks for, or None."""
    if not intake or not intake.get("enabled"):
        return None
    client = ZulipClient.from_env(Path(intake.get("zulip_env") or SPEC.zulip_env))
    return Intake(wfe, client, env=env, log=log)


def main(argv: list[str] | None = None) -> int:
    from agautolab.wfexec import SETTINGS, Settings
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--settings", type=Path, default=SETTINGS)
    args = parser.parse_args(argv)
    s = Settings.load(args.settings)
    log = lambda m: print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {m}", flush=True)  # noqa: E731
    intake = from_settings(s.wfe, s.intake or {"enabled": True}, env=s.env, log=log)
    if intake is None:
        print("intake is not enabled", file=sys.stderr)
        return 1
    if args.once:
        print(json.dumps(intake.pass_once()))
        return 0
    intake.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
