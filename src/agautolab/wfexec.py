"""autolab as the executor of workflow runs (workflow_editor p4).

A person requests a run through agdevworld (the workflow editor service);
the request becomes a run record in the project's devdocs and a job in the
execution host's queue. This loop is autolab's side of it: one execution
slot, one agent process at a time.

- It takes the next job (``wfe exec claim``), which records the attempt's
  beginning in the run record, and launches the agent in the project's
  workspace in a process group of its own, with ``WFE_ATTEMPT`` set so its
  ``wfe run`` reports carry the attempt id.
- When the process ends it reports how (``wfe exec finished``); the run
  record decides what that means (a talk question ends the process; an
  exit with work left running is an interruption) and the checkpoint is
  published.
- A stop or cancellation requested by a person is carried out here: the
  process group gets SIGTERM, then SIGKILL; the record calls it stopped only
  after the process is gone.
- On start, and every minute, ``wfe exec reconcile`` turns processes that
  vanished into ``unknown`` and re-queues requests and answers whose queue
  entry was lost. Nothing is rerun or completed by itself.

Every state rule lives in the workflow editor (``shared/run.ts``,
``server/exec.ts``); this module only launches, watches and reports.

Since workflow_editor p5 the same process delivers each run's coordination
to Zulip on a thread of its own (``agautolab.wfnotify``), so delivery goes
on while a run waits for a person and while the queue is idle, and a slow
Zulip never holds up the slot.

Run: ``uv run python -m agautolab.wfexec [--once]``. Settings:
``.local/wfexec.toml`` (``wfe`` = the launcher argv; optional
``agent_command`` = an argv that replaces the harness, for trials;
``[notify] enabled = true`` with optional ``zulip_env`` / ``admin_env``
turns Zulip delivery on).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from agag.agent import resolve_spec_role
from agag.harness import build_argv

from agautolab.instance import AGAUTOLAB_ROOT, SPEC

ROLE = "wfrun"
GUIDE = AGAUTOLAB_ROOT / "agent" / "guides" / "wfrun" / "guide.md"
SETTINGS = AGAUTOLAB_ROOT / ".local" / "wfexec.toml"
TERM_GRACE_SECONDS = 20
RECONCILE_SECONDS = 60
POLL_SECONDS = 2.0


@dataclass
class Settings:
    wfe: list[str]
    agent_command: list[str] | None = None
    profile: str | None = None
    timeout_seconds: float = 4 * 3600
    env: dict[str, str] = field(default_factory=dict)
    notify: dict = field(default_factory=dict)

    @staticmethod
    def load(path: Path = SETTINGS) -> "Settings":
        data = tomllib.loads(path.read_text()) if path.exists() else {}
        wfe = data.get("wfe")
        if not wfe:
            raise SystemExit(f"{path} needs `wfe = [...]`: the argv of the wfe launcher for the agdev registry")
        return Settings(
            wfe=list(wfe) if isinstance(wfe, list) else [str(wfe)],
            agent_command=list(data["agent_command"]) if data.get("agent_command") else None,
            profile=data.get("profile"),
            timeout_seconds=float(data.get("timeout_seconds", 4 * 3600)),
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            notify=dict(data.get("notify") or {}),
        )


@dataclass
class Running:
    attempt: str
    proc: subprocess.Popen
    started: float
    term_sent: float | None = None


class Executor:
    def __init__(self, settings: Settings, *, name: str = "autolab", log=print):
        self.s = settings
        self.name = name
        self.log = log
        self.current: Running | None = None
        self.last_reconcile = 0.0

    # ---- wfe ---------------------------------------------------------------

    def wfe(self, *args: str, check: bool = True) -> dict | list:
        proc = subprocess.run([*self.s.wfe, *args, "--json"], capture_output=True, text=True, timeout=300, env={**os.environ, **self.s.env})
        out = proc.stdout.strip()
        if proc.returncode != 0 and check:
            raise RuntimeError(f"wfe {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip() or out}")
        try:
            return json.loads(out) if out else {}
        except json.JSONDecodeError:
            raise RuntimeError(f"wfe {' '.join(args)} printed no JSON: {out[:300]} {proc.stderr[:300]}")

    # ---- the agent -----------------------------------------------------------

    def agent_argv(self, cwd: Path) -> tuple[list[str], str, dict[str, str]]:
        """The argv, the backend it names (Agent ≠ Model), and its environment."""
        if self.s.agent_command:
            return list(self.s.agent_command), "trial command", {}
        agent = resolve_spec_role(SPEC, ROLE, profile_override=self.s.profile)
        argv = build_argv(
            agent, allowed_tools=agent.allowed_tools, skip_permissions=agent.harness in ("claude_code", "agy"),
            stream=True, cwd=cwd, timeout=self.s.timeout_seconds,
        )
        return argv, f"{agent.harness} / {agent.model}", dict(agent.environment)

    def prompt(self, claim: dict) -> str:
        guide = GUIDE.read_text()
        return "\n".join([
            guide.rstrip(), "", "# This attempt", "",
            f"Attempt: {claim['attempt']['id']} (in your environment as WFE_ATTEMPT; every `wfe run` report carries it).",
            claim["brief"], "",
            "Begin by reading the run: `wfe run show <run> --history`.",
        ])

    def launch(self, claim: dict, argv: list[str], extra_env: dict[str, str]) -> None:
        attempt = claim["attempt"]["id"]
        cwd = Path(claim["workspace"]["path"])
        log = Path(claim["attempt"]["log"])
        log.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, **extra_env, **self.s.env, "WFE_ATTEMPT": attempt, "PWD": str(cwd), "NO_COLOR": "1"}
        wfe_dir = str(Path(self.s.wfe[0]).parent) if "/" in self.s.wfe[0] else ""
        if wfe_dir:
            env["PATH"] = f"{wfe_dir}:{env.get('PATH', '')}"
        out = log.open("ab")
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        out.close()
        assert proc.stdin is not None
        try:
            proc.stdin.write(self.prompt(claim).encode())
            proc.stdin.close()
        except BrokenPipeError:
            pass
        self.wfe("exec", "started", attempt, "--pid", str(proc.pid))
        self.current = Running(attempt, proc, time.monotonic())
        self.log(f"attempt {attempt}: launched pid {proc.pid} for {claim['ref']['workflow']}/{claim['ref']['run']} in {claim['workspace']['id']}")

    # ---- one tick ----------------------------------------------------------------

    def stop_group(self, r: Running, why: str) -> None:
        now = time.monotonic()
        try:
            if r.term_sent is None:
                os.killpg(r.proc.pid, signal.SIGTERM)
                r.term_sent = now
                self.log(f"attempt {r.attempt}: SIGTERM ({why})")
            elif now - r.term_sent > TERM_GRACE_SECONDS:
                os.killpg(r.proc.pid, signal.SIGKILL)
                self.log(f"attempt {r.attempt}: SIGKILL ({why}; still alive after {TERM_GRACE_SECONDS} s)")
        except ProcessLookupError:
            pass

    def tick(self) -> str:
        self.wfe("exec", "heartbeat", "--name", self.name, "--pid", str(os.getpid()),
                 "--detail", f"executing {self.current.attempt}" if self.current else "idle")
        if time.monotonic() - self.last_reconcile > RECONCILE_SECONDS:
            notes = self.wfe("exec", "reconcile").get("notes", [])
            for n in notes:
                self.log(f"reconcile: {n}")
            self.last_reconcile = time.monotonic()
        r = self.current
        if r is not None:
            due = {a["id"]: a for a in self.wfe("exec", "stops")}
            if r.attempt in due:
                self.stop_group(r, "stop or cancel requested")
            elif time.monotonic() - r.started > self.s.timeout_seconds:
                self.stop_group(r, "the attempt's time budget ran out")
            code = r.proc.poll()
            if code is None:
                return "executing"
            # The leader is gone; the group may still hold children.
            try:
                os.killpg(r.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            args = ["exec", "finished", r.attempt]
            if code < 0:
                args += ["--signal", signal.Signals(-code).name]
            else:
                args += ["--exit-code", str(code)]
            result = self.wfe(*args)
            self.log(f"attempt {r.attempt}: ended ({'signal ' + signal.Signals(-code).name if code < 0 else f'exit {code}'}) → {result.get('outcome')}; checkpoint {(result.get('checkpoint') or {}).get('state')}")
            self.current = None
            return "finished"
        claim = self.wfe("exec", "claim", "--backend", self.backend_label())
        if "idle" in claim:
            return "idle"
        cwd = Path(claim["workspace"]["path"])
        argv, _, extra_env = self.agent_argv(cwd)
        try:
            self.launch(claim, argv, extra_env)
        except Exception as error:  # the launch failed: the attempt ends as interrupted
            self.log(f"attempt {claim['attempt']['id']}: launch failed: {error}")
            self.wfe("exec", "finished", claim["attempt"]["id"], "--exit-code", "127")
            return "launch-failed"
        return "launched"

    def backend_label(self) -> str:
        if self.s.agent_command:
            return "trial command"
        try:
            agent = resolve_spec_role(SPEC, ROLE, profile_override=self.s.profile, check_available=False)
            return f"{agent.harness} / {agent.model}"
        except Exception as error:  # noqa: BLE001 - recorded, not fatal
            return f"unresolved ({error})"

    def serve(self, once: bool = False) -> None:
        self.log(f"{self.name} workflow-run executor: one slot, polling every {POLL_SECONDS} s")
        stopping = False

        def leave(signum, frame):  # noqa: ARG001
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, leave)
        signal.signal(signal.SIGINT, leave)
        quiet = threading.Event()
        if not once:
            try:
                from agautolab.wfnotify import from_settings
                notifier = from_settings(self.s.wfe, self.s.notify, env=self.s.env, log=lambda m: self.log(f"notify: {m}"))
            except Exception as error:  # noqa: BLE001 - execution goes on without delivery, said once
                notifier = None
                self.log(f"notify: not started: {error}")
            if notifier is not None:
                threading.Thread(target=notifier.run_forever, args=(quiet,), daemon=True, name="wfnotify").start()
        while not stopping:
            try:
                state = self.tick()
            except Exception as error:  # noqa: BLE001 - keep serving; the next tick retries
                self.log(f"tick failed: {error}")
                state = "error"
            if once and state in ("idle", "error"):
                break
            time.sleep(POLL_SECONDS)
        quiet.set()
        if self.current is not None:
            # The agent keeps running in its own session; a restarted executor
            # finds it alive and leaves the slot to it (reconcile).
            self.log(f"leaving attempt {self.current.attempt} (pid {self.current.proc.pid}) running")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="return when nothing is left to do (trials)")
    parser.add_argument("--settings", type=Path, default=SETTINGS)
    args = parser.parse_args(argv)
    Executor(Settings.load(args.settings), log=lambda m: print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {m}", flush=True)).serve(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
