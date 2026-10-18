"""autolab as the executor of workflow runs (workflow_editor p4).

A person requests a run through agdevworld (the workflow editor service);
the request becomes a run record in the project's devdocs and a job in the
execution host's queue. This loop is autolab's side of it: one execution
slot, one agent process at a time.

- It takes the next job (``wfe exec claim``), offering the execution
  profiles it can launch and the hash of the common workflow contract. The
  editor records the attempt's beginning in the run record with the settings
  the run's workflow asks for (the agent identity, the profile, the
  context bundle by snapshot id and hashes) and hands them back. The agent is launched in the
  run's own working copy (``cwd`` of the claim; workflow_editor p9: each run
  has its own copy of the project, on its own branches) in a process group of its own, with the workflow's
  profile, ``WFE_ATTEMPT`` set so its ``wfe run`` reports carry the attempt
  id, and a prompt composed of the common contract, the run's scoped context
  (workflow_editor p13: the snapshot the claim prepared) and the run's brief
  (workflow_editor p6). The executing agent is whoever the
  workflow names; autolab is the host that launches and publishes it.
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

Sessions (agent_session p1). A run's workflow chooses its session policy:
``fresh`` (a new harness conversation every attempt) or ``run`` (later
attempts resume the run's retained conversation). The editor decides at
claim which session an attempt uses — new under an id it mints, or the
run's current one by id — and records it in ``attempt.begin`` before
anything is launched. This loop offers which profiles can keep such
conversations (``agag.harness.SESSION_HARNESSES``), passes the id to the
harness (``--session-id`` / ``--resume``), checks before a resume that the
conversation is still in this host's store (a missing one is replaced
through ``wfe exec session --missing`` before launch), gives a resumed
session the editor's compact continuation instead of the whole bootstrap
(the whole bootstrap again when the editor found it compacted, agent_session
p1 ex2), and records
the id the harness reports in its init event (``wfe exec session
--attached``; a failed record is retried and carried by the end report). An
id other than the intended one stops the attempt, which ends interrupted
(``--interrupted "session mismatch: …"``); WFE then refuses to continue that
conversation until it is retired (agent_session p1 ex1). A process still
lives only as long as its attempt: a wait for a person ends it, and the
conversation waits in the store.

Launch ownership (agent_session p1 ex1). The agent is spawned behind a launch
gate: a tiny wrapper that waits on a pipe this loop holds and execs the
harness only when it reads ``go``, written after ``wfe exec started``
registered its process. An executor that dies before that closes the pipe,
and the wrapper exits without doing any agent work, so an unregistered
process is never a writer. From the spawn on, the loop owns the process: a
launch that fails after it (registration, the gate, the prompt) terminates
and reaps the group before the attempt is finalized; if that cannot be
confirmed, the loop keeps the process and the slot (``reconcile
--holding``) and says so, rather than letting another writer start.

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
turns Zulip delivery on; ``[intake] enabled = true`` with optional
``zulip_env`` turns on requests from Zulip, ``agautolab.wfintake``).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from agag.agent import resolve_spec_role
from agag.harness import SESSION_HARNESSES, Session, build_argv, session_stored, stream_session_id

from agautolab.instance import AGAUTOLAB_ROOT, SPEC

ROLE = "wfrun"
# The common workflow contract every run's prompt starts with; the run's
# scoped context (from the claim) and the run's brief follow it.
GUIDE = AGAUTOLAB_ROOT / "agent" / "guides" / "wfrun" / "guide.md"
CONTRACT_NAME = "wfrun/guide.md"
SETTINGS = AGAUTOLAB_ROOT / ".local" / "wfexec.toml"
TERM_GRACE_SECONDS = 20
RECONCILE_SECONDS = 60
POLL_SECONDS = 2.0
# How long a failed launch waits for its process group after SIGTERM and
# again after SIGKILL before it reports the group as not confirmed gone.
ABANDON_WAIT_SECONDS = 5.0
# The launch gate: argv[1] is the read end of the executor's pipe. Anything
# but `go` (EOF: the executor is gone) exits before the harness is started.
GATE = (
    "import os,sys\n"
    "fd=int(sys.argv[1])\n"
    "go=os.read(fd,2)\n"
    "os.close(fd)\n"
    "sys.exit(75) if go!=b'go' else os.execvp(sys.argv[2],sys.argv[2:])\n"
)


def _profile_names() -> list[str]:
    from agag.agent import load_config
    config, overlay = load_config(SPEC.agents_config, SPEC.agents_local_config)
    names = set((config.get("profiles") or {}).keys()) | set(((overlay or {}).get("profiles") or {}).keys())
    return sorted(names)


@dataclass
class Settings:
    wfe: list[str]
    agent_command: list[str] | None = None
    profile: str | None = None
    timeout_seconds: float = 4 * 3600
    env: dict[str, str] = field(default_factory=dict)
    notify: dict = field(default_factory=dict)
    intake: dict = field(default_factory=dict)
    # Trials with `agent_command`: offer explicit sessions for every profile
    # and hand the session to the command as WFE_SESSION_MODE / WFE_SESSION_ID;
    # a conversation counts as stored when `<trial_session_store>/<id>` exists.
    trial_sessions: bool = False
    trial_session_store: Path | None = None

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
            intake=dict(data.get("intake") or {}),
            trial_sessions=bool(data.get("trial_sessions", False)),
            trial_session_store=Path(data["trial_session_store"]) if data.get("trial_session_store") else None,
        )


@dataclass
class Running:
    attempt: str
    proc: subprocess.Popen
    started: float
    term_sent: float | None = None
    log: Path | None = None
    # The session this attempt was launched in (agent_session p1), until the
    # harness's init event says which id it actually uses.
    session_id: str | None = None
    attached: bool = True
    # The id the harness's init event reported, once read; recorded when
    # `attached` turns True (a failed record is retried, and carried by the
    # end report: agent_session p1 ex1).
    reported: str | None = None
    # Why the execution host ends this attempt itself although its process
    # may exit 0: a launch that failed after the spawn, or a session
    # mismatch (agent_session p1 ex1). Its end is recorded, as interrupted
    # for this reason, only once the process group is confirmed gone.
    abandon: str | None = None


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

    def agent_argv(self, cwd: Path, profile: str | None = None, session: Session | None = None) -> tuple[list[str], str, dict[str, str]]:
        """The argv, the backend it names (Agent ≠ Model), and its environment.
        `profile` is the run's workflow's; without one, the host's default.
        `session`: the conversation to create or resume (agent_session p1)."""
        if self.s.agent_command:
            env = {"WFE_SESSION_MODE": session.mode, "WFE_SESSION_ID": session.id} if session else {}
            return list(self.s.agent_command), "trial command", env
        agent = resolve_spec_role(SPEC, ROLE, profile_override=profile or self.s.profile)
        argv = build_argv(
            agent, allowed_tools=agent.allowed_tools, skip_permissions=agent.harness in ("claude_code", "agy"),
            stream=True, cwd=cwd, timeout=self.s.timeout_seconds, session=session,
        )
        return argv, f"{agent.harness} / {agent.model}", dict(agent.environment)

    def session_stored(self, profile: str | None, session_id: str) -> bool | None:
        """Whether this host still holds the conversation to resume."""
        if self.s.agent_command:
            if self.s.trial_session_store is None:
                return None
            return (self.s.trial_session_store / session_id).exists()
        agent = resolve_spec_role(SPEC, ROLE, profile_override=profile or self.s.profile, check_available=False)
        return session_stored(agent, session_id)

    def prompt(self, claim: dict, contract: str | None = None) -> str:
        """A new session: the common contract, then the scoped context the
        claim prepared (its snapshot's text), then the brief. A resumed session
        already holds the contract and context (the editor resumes only under
        the same configuration, context fingerprint included), so it gets the editor's compact continuation
        instead (agent_session p1 ex2). A resumed session the editor could
        not show to still hold them (compacted, or a compaction that cannot
        be ruled out: ``session.reconstruct``) gets the whole bootstrap again,
        in the same conversation, and is told why."""
        session = (claim.get("execution") or {}).get("session") or {}
        if session.get("mode") == "resume" and claim.get("continuation"):
            return self.continuation(claim)
        attempt = claim["attempt"]["id"]
        parts: list[str] = []
        if session.get("mode") == "resume":
            parts += [
                f"# Attempt {attempt}: the run's instructions again", "",
                f"This is a new attempt of the same run, continuing your conversation, but {session.get('reconstruct') or 'it may no longer hold its beginning'}: "
                "what it said earlier may be summarized or missing. So the run's instructions follow in full, as a new "
                "session gets them. Where they or the record differ from what you remember, they decide. Every earlier "
                "attempt id in this conversation is ended, and processes you started in earlier attempts are gone.", "",
            ]
        parts += [(contract if contract is not None else GUIDE.read_text()).rstrip()]
        # The scoped context the editor resolved and saved for this attempt
        # (workflow_editor p13): system, project and workflow defaults and
        # overrides, as the exact text of its snapshot.
        context = ((claim.get("execution") or {}).get("context") or {}).get("text")
        if context:
            parts += ["", str(context).rstrip()]
        parts += [
            "", "# This attempt", "",
            f"Attempt: {attempt} (in your environment as WFE_ATTEMPT; every `wfe run` report carries it).",
            claim["brief"], "",
            "Begin by reading the run: `wfe run show <run> --history`.",
        ]
        return "\n".join(parts)

    def continuation(self, claim: dict) -> str:
        """What a resumed session is told at the start of a new attempt: the
        new attempt identity, then the editor's continuation — why it runs,
        what was recorded since its previous attempt began, the compact
        state and the pending input in full (agent_session p1 ex2). The
        contract, context and stable run facts are already in the
        conversation, unchanged; nothing asks it to read the whole run."""
        attempt = claim["attempt"]["id"]
        return "\n".join([
            f"# This attempt: {attempt}", "",
            f"This is a new attempt of the same run, continuing your conversation. Your attempt is now {attempt} "
            "(WFE_ATTEMPT in your environment); every earlier attempt id in this conversation is ended, and a "
            "report carrying one is refused. Processes you started in earlier attempts are gone.",
            "What you remember is context, not record: what was answered, accepted, held or completed is what "
            "the run's record says now.", "",
            claim["continuation"],
        ])
    def offer(self) -> tuple[dict[str, str], str, str, dict[str, str]]:
        """The profiles this host can launch (name → backend label), the
        common contract with its hash, and which profiles keep explicit
        sessions ('' = the host's default profile), as offered at claim."""
        contract = GUIDE.read_text()
        digest = hashlib.sha256(contract.encode()).hexdigest()
        if self.s.agent_command:
            names = _profile_names()
            sessions = {name: "explicit" for name in [*names, ""]} if self.s.trial_sessions else {}
            return {name: "trial command" for name in names}, contract, digest, sessions
        profiles: dict[str, str] = {}
        sessions: dict[str, str] = {}
        for name in _profile_names():
            try:
                agent = resolve_spec_role(SPEC, ROLE, profile_override=name, check_available=False)
            except Exception:  # noqa: BLE001 - a profile that does not resolve is not offered
                continue
            profiles[name] = f"{agent.harness} / {agent.model}"
            if agent.harness in SESSION_HARNESSES:
                sessions[name] = SESSION_HARNESSES[agent.harness]
        try:
            default = resolve_spec_role(SPEC, ROLE, profile_override=self.s.profile, check_available=False)
            if default.harness in SESSION_HARNESSES:
                sessions[""] = SESSION_HARNESSES[default.harness]
        except Exception:  # noqa: BLE001 - no default profile: none offered for ''
            pass
        return profiles, contract, digest, sessions

    def launch(self, claim: dict, argv: list[str], extra_env: dict[str, str], contract: str | None = None) -> None:
        """Spawns the agent behind the launch gate, takes ownership of it
        (``self.current``) at once, registers it, opens the gate and sends
        the prompt. Raises after the spawn only with ``self.current`` set:
        the caller then stops what was spawned before ending the attempt."""
        attempt = claim["attempt"]["id"]
        cwd = Path(claim["cwd"])
        log = Path(claim["attempt"]["log"])
        log.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, **extra_env, **self.s.env, "WFE_ATTEMPT": attempt, "PWD": str(cwd), "NO_COLOR": "1"}
        wfe_dir = str(Path(self.s.wfe[0]).parent) if "/" in self.s.wfe[0] else ""
        if wfe_dir:
            env["PATH"] = f"{wfe_dir}:{env.get('PATH', '')}"
        prompt = self.prompt(claim, contract).encode()
        if shutil.which(argv[0], path=env.get("PATH")) is None:
            raise FileNotFoundError(f"{argv[0]}: no such command")
        gate_r, gate_w = os.pipe()
        try:
            with log.open("ab") as out:
                proc = subprocess.Popen([sys.executable, "-I", "-c", GATE, str(gate_r), *argv], cwd=cwd, env=env,
                                        stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT,
                                        start_new_session=True, pass_fds=(gate_r,))
        except BaseException:
            os.close(gate_w)
            raise
        finally:
            os.close(gate_r)
        sid = ((claim.get("execution") or {}).get("session") or {}).get("id")
        self.current = Running(attempt, proc, time.monotonic(), log=log, session_id=sid, attached=not sid)
        try:
            self.wfe("exec", "started", attempt, "--pid", str(proc.pid))
            self.open_gate(gate_w)
        finally:
            os.close(gate_w)  # before `go`: EOF, and the gate exits without starting the harness
        assert proc.stdin is not None
        delivered, why = True, ""
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:
            delivered, why = False, "the harness closed its input before the prompt was written"  # its end is recorded as it ended
        self.report_context(claim, delivered, why)
        self.log(f"attempt {attempt}: launched pid {proc.pid} for {claim['ref']['workflow']}/{claim['ref']['run']} in {claim['workspace']['id']}")

    def report_context(self, claim: dict, delivered: bool, detail: str = "") -> None:
        """Records whether the prompt carrying the prepared context reached
        the launched process (workflow_editor p13). Delivery is not proof the
        agent read it. A failed record is logged; the attempt goes on."""
        if not ((claim.get("execution") or {}).get("context") or {}).get("snapshot"):
            return
        attempt = claim["attempt"]["id"]
        try:
            self.wfe("exec", "context", attempt, "--delivered" if delivered else "--undelivered", *(["--detail", detail] if detail else []))
        except Exception as error:  # noqa: BLE001 - a bookkeeping record; the attempt itself is unaffected
            self.log(f"attempt {attempt}: context delivery not recorded: {error}")

    @staticmethod
    def open_gate(fd: int) -> None:
        os.write(fd, b"go")

    @staticmethod
    def group_gone(r: Running) -> bool:
        """The attempt's process group has no member left (its leader reaped)."""
        r.proc.poll()
        try:
            os.killpg(r.proc.pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    def terminate(self, r: Running) -> bool:
        """SIGTERM, then SIGKILL, to the attempt's group, each followed by
        a bounded wait; True once the group is confirmed gone."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(r.proc.pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError as error:
                self.log(f"attempt {r.attempt}: {sig.name} refused: {error}")
            end = time.monotonic() + ABANDON_WAIT_SECONDS
            while time.monotonic() < end:
                if self.group_gone(r):
                    return True
                time.sleep(0.05)
        return self.group_gone(r)

    def abandon(self, r: Running, why: str) -> str:
        """The execution host ends the attempt itself (a launch that failed
        after the spawn, a session mismatch): stop the process group, and
        end the attempt only once the group is confirmed gone. Otherwise the
        loop keeps it (the slot stays held) and the next ticks go on."""
        r.abandon = r.abandon or why
        if r.term_sent is None:
            r.term_sent = time.monotonic()
        if not self.terminate(r):
            self.log(f"attempt {r.attempt}: process group {r.proc.pid} is not confirmed gone ({r.abandon}); "
                     "the executor keeps it and the slot until it is")
            return "stop-unconfirmed"
        return self.finish(r)

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

    def attach(self, r: Running) -> None:
        """Record the session id the harness announced in its init event
        (agent_session p1). The intended id is already recorded; this
        confirms it, or records that the harness used another — and then the
        attempt is stopped: work does not go on in a conversation nobody
        chose (p1 ex1). A failed record stays pending and is retried."""
        if r.attached or r.log is None:
            return
        if r.reported is None:
            try:
                with r.log.open("r", errors="replace") as f:
                    for _ in range(200):
                        line = f.readline()
                        if not line:
                            return
                        sid = stream_session_id(line)
                        if sid:
                            break
                    else:
                        return
            except OSError:
                return
            r.reported = sid
            if r.session_id and sid != r.session_id:
                self.log(f"attempt {r.attempt}: the harness reports session {sid}, not the intended {r.session_id}; stopping the attempt")
                r.abandon = r.abandon or f"session mismatch: launched in session {r.session_id}, the harness reported {sid}"
        try:
            self.wfe("exec", "session", r.attempt, "--attached", r.reported)
            r.attached = True
        except RuntimeError as error:
            self.log(f"attempt {r.attempt}: session {r.reported} not recorded yet (retrying): {error}")

    def tick(self) -> str:
        self.wfe("exec", "heartbeat", "--name", self.name, "--pid", str(os.getpid()),
                 "--detail", f"executing {self.current.attempt}" if self.current else "idle")
        if time.monotonic() - self.last_reconcile > RECONCILE_SECONDS:
            holding = ["--holding", self.current.attempt] if self.current else []
            notes = self.wfe("exec", "reconcile", *holding).get("notes", [])
            for n in notes:
                self.log(f"reconcile: {n}")
            self.last_reconcile = time.monotonic()
        r = self.current
        if r is not None:
            if r.abandon:
                # A failed launch not yet confirmed stopped: keep stopping it.
                return self.abandon(r, r.abandon)
            due = {a["id"]: a for a in self.wfe("exec", "stops")}
            if r.attempt in due:
                self.stop_group(r, "stop or cancel requested")
            elif time.monotonic() - r.started > self.s.timeout_seconds:
                self.stop_group(r, "the attempt's time budget ran out")
            self.attach(r)
            if r.abandon:
                return self.abandon(r, r.abandon)
            if r.proc.poll() is None:
                return "executing"
            self.attach(r)
            if r.abandon:
                return self.abandon(r, r.abandon)
            return self.finish(r)
        profiles, contract, digest, sessions = self.offer()
        claim = self.wfe("exec", "claim", "--backend", self.backend_label(),
                         "--profiles-json", json.dumps(profiles), "--contract", f"{CONTRACT_NAME}={digest}",
                         "--sessions-json", json.dumps(sessions))
        if "idle" in claim:
            return "idle"
        cwd = Path(claim["cwd"])
        execution = claim.get("execution") or {}
        attempt = claim["attempt"]["id"]
        try:
            session = self.session_of(claim)
            argv, _, extra_env = self.agent_argv(cwd, execution.get("profile"), session)
            self.launch(claim, argv, extra_env, contract)
        except Exception as error:  # the launch failed: the attempt ends as interrupted
            self.log(f"attempt {attempt}: launch failed: {error}")
            self.report_context(claim, False, f"launch failed: {error}")
            if self.current is not None and self.current.attempt == attempt:
                return self.abandon(self.current, f"launch failed: {error}")  # something was spawned
            self.wfe("exec", "finished", attempt, "--exit-code", "127")
            return "launch-failed"
        return "launched"

    def finish(self, r: Running) -> str:
        """The attempt's leader is gone: clear its group and record the end.
        A failure to record keeps the attempt current; the next tick retries."""
        code = r.proc.poll()
        try:  # the leader is gone; the group may still hold children
            os.killpg(r.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        args = ["exec", "finished", r.attempt]
        if code is not None and code < 0:
            args += ["--signal", signal.Signals(-code).name]
        else:
            args += ["--exit-code", str(code)]
        if r.abandon:
            args += ["--interrupted", r.abandon]
        if r.reported and not r.attached:
            args += ["--attached", r.reported]  # recorded with the end, or named in its detail
        result = self.wfe(*args)
        how = f"signal {signal.Signals(-code).name}" if code is not None and code < 0 else f"exit {code}"
        how = f"{r.abandon}; {how}" if r.abandon else how
        self.log(f"attempt {r.attempt}: ended ({how}) → {result.get('outcome')}; checkpoint {(result.get('checkpoint') or {}).get('state')}")
        self.current = None
        return "finished"

    def session_of(self, claim: dict) -> Session | None:
        """The conversation this attempt is launched in. A resume first checks
        that the conversation is still in this host's store: a missing one
        is replaced before launch (nothing ran in it in this attempt), and
        the claim is updated to the new session so the prompt is the whole
        bootstrap."""
        execution = claim.get("execution") or {}
        ss = execution.get("session") or {}
        if not ss.get("id"):
            return None
        if ss.get("mode") == "resume":
            stored = self.session_stored(execution.get("profile"), ss["id"])
            if stored is False:
                attempt = claim["attempt"]["id"]
                got = self.wfe("exec", "session", attempt, "--missing", "--detail", "no transcript in the harness store")
                self.log(f"attempt {attempt}: session {ss['id']} is missing on this host; replaced by {got.get('id')}")
                ss = {**ss, "mode": "new", "id": got.get("id"), "replaces": {"id": ss["id"], "reason": "missing on this host"}}
                execution["session"] = ss
                if not ss["id"]:
                    return None
        return Session(ss["mode"], ss["id"])

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
            # Requests, answers and instructions from Zulip (workflow_editor
            # p6), received while an agent runs or waits, like delivery.
            try:
                from agautolab.wfintake import from_settings as intake_from_settings
                intake = intake_from_settings(self.s.wfe, self.s.intake, env=self.s.env, log=lambda m: self.log(f"intake: {m}"))
            except Exception as error:  # noqa: BLE001 - execution goes on without intake, said once
                intake = None
                self.log(f"intake: not started: {error}")
            if intake is not None:
                threading.Thread(target=intake.run_forever, args=(quiet,), daemon=True, name="wfintake").start()
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
