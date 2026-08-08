/* autolab monitor — vanilla, no build step, no framework.
 *
 * Polls the gateway every POLL_MS and re-renders. Everything this system knows
 * changes at a session or iteration boundary except the drive log, which is
 * the only genuinely realtime stream — so polling is enough, and the log is
 * the thing worth watching between refreshes.
 */

const POLL_MS = 3000;
const LOG_TAIL = 200;

// Job whose evidence timeline is expanded. Kept in the URL hash so a reload
// (and a screenshot, and a link pasted to someone else) lands on the same job.
let selectedJob = new URLSearchParams(location.hash.slice(1)).get("job") || null;

function selectJob(name) {
  selectedJob = selectedJob === name ? null : name;
  history.replaceState(null, "", selectedJob ? "#job=" + selectedJob : "#");
  refresh();
}

const $ = (id) => document.getElementById(id);
const text = (el, s) => { el.textContent = s; };

function fmtUsd(v) {
  return typeof v === "number" ? "$" + v.toFixed(4) : "—";
}
function fmtSecs(ms) {
  return typeof ms === "number" ? Math.round(ms / 1000) + "s" : "—";
}
function fmtClock(epoch) {
  return typeof epoch === "number"
    ? new Date(epoch * 1000).toLocaleTimeString()
    : "—";
}

async function getJSON(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(path + " → HTTP " + r.status);
  return r.json();
}

function el(tag, cls, txt) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt !== undefined) n.textContent = txt;
  return n;
}

function statusCell(status) {
  const s = status || "unknown";
  return el("span", "st st-" + s, s);
}

/* ---------- header ---------- */

function renderHeader(st) {
  const d = st.driver || {};
  const pill = $("driver");
  if (d.running) {
    pill.className = "pill live";
    text(pill, "driver running" + (d.current ? " · run " + d.current.run : ""));
  } else if (d.exit_code === 0) {
    pill.className = "pill done";
    text(pill, "driver finished (exit 0)");
  } else if (typeof d.exit_code === "number") {
    pill.className = "pill bad";
    text(pill, "driver finished (exit " + d.exit_code + ")");
  } else {
    pill.className = "pill";
    text(pill, "driver idle");
  }

  text($("status-line"), st.notes_status || "");
  text($("mission"), st.mission_headline || "(no mission on disk)");

  const c = st.cost || {};
  // Cumulative mediator cost gets top billing: agentify measured it at 3.7x
  // implementation cost, and it is the number a human decides on.
  let cost = "cost " + fmtUsd(c.sessions_usd) + " (all sessions)";
  if (typeof c.current_run_sessions_usd === "number") {
    cost += " · " + fmtUsd(c.current_run_sessions_usd) + " this run";
  }
  text($("cost"), cost);

  const ds = $("devstyle");
  if (st.devstyle) {
    ds.classList.remove("hidden");
    ds.replaceChildren();
    for (const [label, key] of [
      ["Style chosen", "style_chosen"],
      ["Why", "why"],
      ["Was it right in hindsight", "hindsight"],
    ]) {
      const line = el("div");
      line.append(el("b", null, label + ": "), document.createTextNode(st.devstyle[key] || "—"));
      ds.append(line);
    }
  } else {
    ds.classList.add("hidden");
  }
}

/* ---------- sessions ---------- */

function renderSessions(st) {
  const rows = st.sessions || [];
  const total = st.sessions_total_on_disk;
  text(
    $("sessions-note"),
    rows.length === total ? "" : `showing ${rows.length} of ${total} on disk (this run)`
  );

  const body = el("tbody");
  body.append(headerRow(["session", "result", "turns", "cost", "duration"]));
  if (!rows.length) body.append(emptyRow(5, "no sessions yet"));
  for (const s of rows) {
    const tr = el("tr");
    tr.append(el("td", null, s.file));
    const res = el("td");
    res.append(
      s.is_error === false
        ? el("span", "gate-ok", "ok")
        // "in progress" is the live session, not a failed one.
        : el("span", s.is_error === "in progress" ? "live" : "fail", String(s.is_error))
    );
    tr.append(res);
    tr.append(el("td", "num", s.turns ?? "—"));
    tr.append(el("td", "num", fmtUsd(s.cost_usd)));
    tr.append(el("td", "num", s.duration_s != null ? s.duration_s + "s" : "—"));
    body.append(tr);
  }
  $("sessions").replaceChildren(spacerColumn(body));
}

function headerRow(labels) {
  const tr = el("tr");
  for (const l of labels) tr.append(el("th", null, l));
  return tr;
}
function spacerColumn(body) {
  // Empty trailing cell per row; `.grow` gives it all the leftover width.
  for (const r of body.rows) {
    r.append(el(r.firstChild && r.firstChild.tagName === "TH" ? "th" : "td", "grow"));
  }
  return body;
}

function emptyRow(span, msg) {
  const tr = el("tr");
  const td = el("td", "dim", msg);
  td.colSpan = span;
  tr.append(td);
  return tr;
}

/* ---------- jobs ---------- */

function renderJobs(doc) {
  const jobs = doc.jobs || [];
  const body = el("tbody");
  body.append(headerRow(["job", "status", "iter", "gates", "cost", "evidence"]));
  if (!jobs.length) body.append(emptyRow(6, "no jobs on disk"));

  for (const j of jobs) {
    const tr = el("tr", "job" + (j.name === selectedJob ? " selected" : ""));
    tr.onclick = () => selectJob(j.name);

    tr.append(el("td", null, j.name));

    const st = el("td");
    st.append(statusCell(j.status));
    if (j.error || j.state_error) {
      st.append(el("div", "fail", j.error || j.state_error));
    }
    tr.append(st);

    tr.append(
      el("td", "num", (j.iteration ?? "—") + " / " + (j.max_iterations ?? "—"))
    );

    tr.append(gatesCell(j.last_gate_summary));
    tr.append(el("td", "num", fmtUsd(j.cost_usd)));

    const ev = el("td");
    if (j.last_evidence) {
      ev.append(el("span", null, j.last_evidence.replace("evidence/", "")));
      ev.append(el("span", "dim", ` (${j.iterations_on_disk} on disk)`));
    } else {
      ev.append(el("span", "dim", "none"));
    }
    tr.append(ev);

    body.append(tr);

    // The failing gate command is the single most useful string on this page
    // when something is wrong, so it is inline rather than a click away — on
    // its own full-width row, because gate commands are long enough to
    // squeeze every other column to nothing if they share a cell.
    for (const g of (j.last_gate_summary || {}).failing || []) {
      const fail = el("tr", "job");
      fail.onclick = tr.onclick;
      fail.append(el("td"));
      const td = el("td", "fail wrap", "failing: " + g);
      td.colSpan = 5;  // 1 indent + 5 + the spacer column = the 7 above
      fail.append(td);
      body.append(fail);
    }
  }
  $("jobs").replaceChildren(spacerColumn(body));
}

function gatesCell(summary) {
  const td = el("td");
  if (!summary) {
    td.append(el("span", "dim", "not run"));
    return td;
  }
  const total = summary.total ?? 0;
  const failing = summary.failing || [];
  const passed = total - failing.length;
  td.append(
    el("span", failing.length ? "fail" : "gate-ok", `${passed}/${total}`)
  );
  return td;
}

/* ---------- evidence ---------- */

async function renderEvidence() {
  const host = $("evidence");
  if (!selectedJob) {
    host.className = "dim";
    text(host, "select a job above");
    return;
  }
  host.className = "";
  let doc;
  try {
    doc = await getJSON("/jobs/" + encodeURIComponent(selectedJob));
  } catch (e) {
    host.replaceChildren(el("div", "fail", String(e)));
    return;
  }
  const iters = (doc.job && doc.job.evidence) || [];
  host.replaceChildren();
  host.append(el("div", "dim", selectedJob + " — click again to collapse"));
  if (!iters.length) {
    host.append(el("div", "dim", "no evidence directories yet"));
    return;
  }
  // Newest first: the iteration a human is waiting on is the last one.
  for (const it of [...iters].reverse()) {
    const row = el("div", "iter");
    row.append(el("span", "name", it.iter));
    row.append(
      el(
        "span",
        it.is_error || it.timed_out || it.exit_code ? "fail" : "dim",
        [
          fmtClock(it.mtime),
          fmtUsd(it.cost_usd),
          (it.num_turns ?? "—") + " turns",
          fmtSecs(it.duration_ms),
          it.timed_out ? "TIMED OUT" : "exit " + (it.exit_code ?? "—"),
        ].join("  ")
      )
    );
    if (it.gates && it.gates.length) {
      const bad = it.gates.filter((g) => g.exit_code !== 0);
      row.append(
        el(
          "span",
          bad.length ? "fail" : "gate-ok",
          `gates ${it.gates.length - bad.length}/${it.gates.length}`
        )
      );
    }
    const files = el("span", "files");
    for (const f of it.files || []) {
      const a = el("a", null, f);
      a.href = `/jobs/${encodeURIComponent(selectedJob)}/evidence/${it.iter}/${f}`;
      a.target = "_blank";
      a.rel = "noopener";
      files.append(a);
    }
    row.append(files);
    host.append(row);
  }
}

/* ---------- log ---------- */

async function renderLog() {
  const pre = $("log");
  // Only follow the tail while the user is already at the bottom, so reading
  // scrollback is not yanked away by the next poll.
  const pinned = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  try {
    const r = await fetch("/log?tail=" + LOG_TAIL, { cache: "no-store" });
    if (r.status === 404) {
      pre.className = "log dim";
      text(pre, "no drive run yet (POST /mission starts one)");
      return;
    }
    pre.className = "log";
    text(pre, await r.text());
  } catch (e) {
    pre.className = "log";
    text(pre, String(e));
    return;
  }
  if (pinned) pre.scrollTop = pre.scrollHeight;
}

/* ---------- loop ---------- */

async function refresh() {
  const err = $("error");
  try {
    const [st, jobs] = await Promise.all([getJSON("/status"), getJSON("/jobs")]);
    renderHeader(st);
    renderSessions(st);
    renderJobs(jobs);
    err.classList.add("hidden");
  } catch (e) {
    text(err, "gateway unreachable: " + e);
    err.classList.remove("hidden");
  }
  await Promise.all([renderEvidence(), renderLog()]);
  text($("tick"), "updated " + new Date().toLocaleTimeString());
}

refresh();
setInterval(refresh, POLL_MS);
