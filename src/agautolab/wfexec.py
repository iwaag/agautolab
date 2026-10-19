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

Execution nodes (project_migration p7). This loop is one worker of the
coordinator (``worker`` in the settings: its id in the coordinator's
workers.json; empty: the coordinator's local worker). It claims only runs
assigned to it, into its own slot, and names itself in every report. On a
remote worker its ``wfe`` is the coordinator's client (WFE
``cli/remote.ts``): the claim materializes the run's copy on this host, the
end publishes it. The launch gate records how the agent's process ended in
``<log>.exit``, and a restarted loop takes back a process it left running
(``wfe exec adopt``) instead of waiting for it to be declared lost.

Worker lanes (project_migration p8). A worker may declare extra lanes in the
coordinator's workers.json (``"lanes": {"conversation": 1}``), so a
conversational run is answered while a long attempt holds the default slot.
``lanes = ["conversation"]`` in the settings makes this process serve them:
one ``Executor`` per lane, each with its own slot and its own tick loop (the
extra lanes' in threads), claiming with ``--lane <name>`` and carrying out
its own attempt's stops and end. Heartbeat, reconcile (``--holding`` with
the attempts of every lane), delivery and intake stay with the default
lane's executor, and ``adopt`` hands each live attempt back to the executor
of its lane. Without ``lanes`` this is exactly the one slot of before.

Every state rule lives in the workflow editor (``shared/run.ts``,
``server/exec.ts``); this module only launches, watches and reports.

Since workflow_editor p5 the same process delivers each run's coordination
to Zulip on a thread of its own (``agautolab.wfnotify``), so delivery goes
on while a run waits for a person and while the queue is idle, and a slow
Zulip never holds up the slot.

Run: ``uv run python -m agautolab.wfexec [--once]``. Settings:
``.local/wfexec.toml`` (``wfe`` = the launcher argv; optional
``agent_command`` = an argv that replaces the harness, for trials;
``worker`` = this executor's node id; ``lanes`` = the extra worker lanes
it claims in;
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
# How long a harness may stay alive after writing its session's final result
# before the processes it left in the background are stopped
# (project_migration p8 ex1: front converse/run-029 ended its turn with a
# backgrounded `find /` still running, and the attempt held its slot).
SESSION_END_GRACE_SECONDS = 30
RECONCILE_SECONDS = 60
POLL_SECONDS = 2.0
# The lane every worker has (project_migration p8): its one slot of before.
DEFAULT_LANE = "default"
# How long a failed launch waits for its process group after SIGTERM and
# again after SIGKILL before it reports the group as not confirmed gone.
ABANDON_WAIT_SECONDS = 5.0
# The launch gate: argv[1] is the read end of the executor's pipe, argv[2]
# the exit file. Anything but `go` (EOF: the executor is gone) exits before
# the harness is started. After `go` the gate runs the harness as its child,
# waits for it and writes how it ended (`exit <code>` / `signal <name>`) to
# the exit file before ending the same way (project_migration p7): an
# executor restarted meanwhile reads there how its attempt ended instead of
# only finding the process gone. The gate ignores the group's SIGTERM so it
# outlives the harness; SIGKILL ends both, and then there is no exit file.
GATE = (
    "import os,sys,signal\n"
    "fd=int(sys.argv[1]);ex=sys.argv[2]\n"
    "go=os.read(fd,2)\n"
    "os.close(fd)\n"
    "go==b'go' or sys.exit(75)\n"
    "pid=os.fork()\n"
    "pid or os.execvp(sys.argv[3],sys.argv[3:])\n"
    "[signal.signal(s,signal.SIG_IGN) for s in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP)]\n"
    "st=os.waitpid(pid,0)[1]\n"
    "w='signal %s'%signal.Signals(os.WTERMSIG(st)).name if os.WIFSIGNALED(st) else 'exit %d'%os.WEXITSTATUS(st)\n"
    "open(ex+'.tmp','w').write(w+'\\n');os.replace(ex+'.tmp',ex)\n"
    "os.WIFSIGNALED(st) and (signal.signal(os.WTERMSIG(st),signal.SIG_DFL),os.kill(os.getpid(),os.WTERMSIG(st)))\n"
    "sys.exit(os.WEXITSTATUS(st))\n"
)


def _profile_names() -> list[str]:
    from agag.agent import load_config
    config, overlay = load_config(SPEC.agents_config, SPEC.agents_local_config)
    names = set((config.get("profiles") or {}).keys()) | set(((overlay or {}).get("profiles") or {}).keys())
    return sorted(names)


@dataclass
class Settings:
    wfe: list[str]
    # The execution node this executor is (project_migration p7): the id in
    # the coordinator's workers.json. Empty: the coordinator's local worker.
    worker: str = ""
    # The extra worker lanes (project_migration p8) this process claims in,
    # one executor each, beside the default lane. Empty: the default lane only.
    lanes: list[str] = field(default_factory=list)
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
            worker=str(data.get("worker") or ""),
            lanes=[str(x) for x in (data.get("lanes") or []) if str(x) != DEFAULT_LANE],
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
    # When the harness's final result event was first seen while its
    # process lived on (monotonic), whether that result was an error, and
    # whether leftovers were stopped because of it (p8 ex1).
    session_ended: float | None = None
    session_error: bool = False
    leftover: bool = False


def session_result(log: Path) -> dict | None:
    """The harness's final `result` event of its stream log, if written
    (claude_code stream-json): the session has ended."""
    try:
        with log.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 262144))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if '"type":"result"' not in line.replace(" ", ""):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return None


class Adopted:
    """A process this executor left running before a restart and takes back
    (``wfe exec adopt``, project_migration p7). It is not this process's
    child: ``poll`` looks at the pid and, once it is gone, at the exit file
    the launch gate wrote. Gone without one, the end is not known (``lost``)."""

    def __init__(self, pid: int, exit_file: Path):
        self.pid = pid
        self.exit_file = exit_file
        self.returncode: int | None = None
        self.lost = False
        self.stdin = None

    def poll(self) -> int | None:
        if self.returncode is not None or self.lost:
            return self.returncode
        try:
            os.kill(self.pid, 0)
            return None
        except ProcessLookupError:
            pass
        except PermissionError:
            return None
        ended = exit_of(self.exit_file)
        if ended is None:
            self.lost = True
            return None
        self.returncode = ended
        return ended


def exit_of(file: Path) -> int | None:
    """The end the launch gate recorded: an exit code, or minus a signal number."""
    try:
        kind, value = file.read_text().split()
    except (OSError, ValueError):
        return None
    if kind == "signal":
        return -int(getattr(signal, value, signal.SIGKILL))
    return int(value)


class Executor:
    def __init__(self, settings: Settings, *, name: str = "autolab", log=print, lane: str = DEFAULT_LANE):
        self.s = settings
        self.name = name
        self.lane = lane
        self.log = log
        self.current: Running | None = None
        self.last_reconcile = 0.0
        # The default lane's executor holds one executor per extra lane
        # (project_migration p8); each owns its slot, its stops and its end.
        self.lanes: dict[str, Executor] = {}
        if lane == DEFAULT_LANE:
            for extra in dict.fromkeys(settings.lanes):
                self.lanes[extra] = Executor(settings, name=name, lane=extra, log=lambda m, extra=extra: log(f"[{extra}] {m}"))

    def executors(self) -> list["Executor"]:
        """Every lane's executor of this process, the default lane's first."""
        return [self, *self.lanes.values()]

    # ---- wfe ---------------------------------------------------------------

    def wfe(self, *args: str, check: bool = True) -> dict | list:
        # Every exec report names this executor's node (project_migration p7).
        if args and args[0] == "exec" and self.s.worker:
            args = (*args, "--worker", self.s.worker)
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
                proc = subprocess.Popen([sys.executable, "-I", "-c", GATE, str(gate_r), f"{log}.exit", *argv], cwd=cwd, env=env,
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
        except PermissionError as error:  # macOS: a group left with only an unreaped process
            self.log(f"attempt {r.attempt}: signal refused ({error}); waiting for the process to be gone")

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
        # The worker's heartbeat and the reconcile are the default lane's
        # (project_migration p8): they speak for every lane of this process,
        # and every lane's attempt is held, so none is judged gone meanwhile.
        if self.lane == DEFAULT_LANE:
            def doing(ex: Executor) -> str:
                return f"executing {ex.current.attempt}" if ex.current else "idle"
            detail = "; ".join([doing(self), *(f"{lane}: {doing(ex)}" for lane, ex in self.lanes.items())])
            self.wfe("exec", "heartbeat", "--pid", str(os.getpid()), "--detail", detail)
            if time.monotonic() - self.last_reconcile > RECONCILE_SECONDS:
                holding = [arg for ex in self.executors() if (held := ex.current) is not None for arg in ("--holding", held.attempt)]
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
                self.watch_session_end(r)
                return "executing"
            self.attach(r)
            if r.abandon:
                return self.abandon(r, r.abandon)
            return self.finish(r)
        profiles, contract, digest, sessions = self.offer()
        claim = self.wfe("exec", "claim", "--backend", self.backend_label(),
                         "--profiles-json", json.dumps(profiles), "--contract", f"{CONTRACT_NAME}={digest}",
                         "--sessions-json", json.dumps(sessions),
                         *(("--lane", self.lane) if self.lane != DEFAULT_LANE else ()))
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

    def watch_session_end(self, r: Running) -> None:
        """A harness that wrote its session's final result but lives on is
        kept alive by what it left in the background; nothing wakes an ended
        session, so after a grace those processes are stopped and the
        attempt ends as the session did (project_migration p8 ex1)."""
        if r.log is None:
            return
        if r.session_ended is None:
            result = session_result(r.log)
            if result is None:
                return
            r.session_ended = time.monotonic()
            r.session_error = bool(result.get("is_error"))
            return
        if time.monotonic() - r.session_ended > SESSION_END_GRACE_SECONDS:
            r.leftover = True
            self.stop_group(r, f"the session ended {SESSION_END_GRACE_SECONDS}+ s ago and only processes it left in the background keep it alive")

    def finish(self, r: Running) -> str:
        """The attempt's leader is gone: clear its group and record the end.
        A failure to record keeps the attempt current; the next tick retries."""
        code = r.proc.poll()
        try:  # the leader is gone; the group may still hold children
            os.killpg(r.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        args = ["exec", "finished", r.attempt]
        if getattr(r.proc, "lost", False):
            args += ["--lost"]  # an adopted process gone without an exit file
        elif r.leftover:
            # Stopped for leftovers after the session's own end: it ended as its result says.
            args += ["--exit-code", "1" if r.session_error else "0"]
        elif code is not None and code < 0:
            args += ["--signal", signal.Signals(-code).name]
        else:
            args += ["--exit-code", str(code)]
        if r.abandon:
            args += ["--interrupted", r.abandon]
        if r.reported and not r.attached:
            args += ["--attached", r.reported]  # recorded with the end, or named in its detail
        result = self.wfe(*args)
        how = "lost" if getattr(r.proc, "lost", False) else f"signal {signal.Signals(-code).name}" if code is not None and code < 0 else f"exit {code}"
        how = f"{r.abandon}; {how}" if r.abandon else how
        how = f"session ended, leftover background processes stopped; {how}" if r.leftover else how
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

    def adopt(self) -> None:
        """Takes back the attempts this executor's node left running before a
        restart (project_migration p7), so their stops and ends are handled
        here again: each by the executor of its lane (p8; an answer without a
        lane is the default lane's). Not done: the next ticks' reconcile
        judges the process."""
        try:
            live = self.wfe("exec", "adopt")
        except Exception as error:  # noqa: BLE001 - the reconcile handles it later
            self.log(f"adopt: not checked: {error}")
            return
        for a in live if isinstance(live, list) else []:
            lane = a.get("lane") or DEFAULT_LANE
            ex = self if lane == DEFAULT_LANE else self.lanes.get(lane)
            if ex is None:
                # Not served here any more: its process is judged by the
                # coordinator's reconcile when it is gone.
                self.log(f"attempt {a['id']}: left running in lane {lane}, which this executor does not serve; not adopted")
                continue
            if ex.current is not None:
                continue
            log = Path(a["log"]) if a.get("log") else None
            ex.current = Running(a["id"], Adopted(int(a["pid"]), Path(f"{log}.exit") if log else Path("/nonexistent")), time.monotonic(), log=log)  # type: ignore[arg-type]
            ex.log(f"attempt {a['id']}: adopted pid {a['pid']} left running before the restart")

    def loop(self, halt: threading.Event, once: bool = False) -> None:
        """Ticks until halted (or, with ``once``, until nothing is left to do)."""
        while not halt.is_set():
            try:
                state = self.tick()
            except Exception as error:  # noqa: BLE001 - keep serving; the next tick retries
                self.log(f"tick failed: {error}")
                state = "error"
            if once and state in ("idle", "error"):
                break
            halt.wait(POLL_SECONDS)
        if self.current is not None:
            # The agent keeps running in its own session; a restarted executor
            # finds it alive and leaves the slot to it (reconcile).
            self.log(f"leaving attempt {self.current.attempt} (pid {self.current.proc.pid}) running")

    def serve(self, once: bool = False) -> None:
        slots = f"one slot and lanes {', '.join(self.lanes)}" if self.lanes else "one slot"
        self.log(f"{self.name} workflow-run executor{f' (worker {self.s.worker})' if self.s.worker else ''}: {slots}, polling every {POLL_SECONDS} s")
        self.adopt()
        halt = threading.Event()

        def leave(signum, frame):  # noqa: ARG001
            halt.set()

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
        # Each extra lane ticks in a thread of its own (p8), so a long launch
        # or end in one lane never delays another; they end with this loop.
        workers = [threading.Thread(target=ex.loop, args=(halt, once), name=f"wfexec-{lane}") for lane, ex in self.lanes.items()]
        for t in workers:
            t.start()
        self.loop(halt, once)
        if not once:
            halt.set()
        for t in workers:
            t.join()
        quiet.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="return when nothing is left to do (trials)")
    parser.add_argument("--settings", type=Path, default=SETTINGS)
    args = parser.parse_args(argv)
    Executor(Settings.load(args.settings), log=lambda m: print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {m}", flush=True)).serve(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
