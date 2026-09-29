"use strict";

/* BP Engine front end: plain JavaScript, no build step.
   Every request sends X-User-Id so the server knows who you are acting as. */

const state = { users: [], user: null, jobProfiles: [], orgs: [], workers: [] };

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const view = () => document.getElementById("view");

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
const money = (n) =>
  n == null ? "" : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(n);

function fmtDate(value) {
  if (!value) return "";
  if (value.length === 10) {
    const [y, m, d] = value.split("-").map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }
  return new Date(value).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
const todayIso = () => new Date().toISOString().slice(0, 10);
const addDays = (days) => new Date(Date.now() + days * 86400000).toISOString().slice(0, 10);

function safeGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function safeSet(key, value) { try { localStorage.setItem(key, value); } catch { /* storage unavailable */ } }

const STATUS_CLASS = {
  "In Progress": "wait",
  "Awaiting Action": "wait",
  Open: "wait",
  "Successfully Completed": "ok",
  Completed: "ok",
  Denied: "bad",
  Canceled: "idle",
  "Not Required": "idle",
  Rescinded: "warn",
  "Sent Back": "warn",
  Active: "ok",
  Terminated: "bad",
};
const statusPill = (s) => `<span class="status ${STATUS_CLASS[s] || "idle"}">${esc(s)}</span>`;

const STEP_TYPE_LABEL = {
  initiation: "Initiation",
  approval: "Approval",
  approval_chain: "Approval Chain",
  to_do: "To Do",
  subprocess: "Sub-process",
  integration: "Integration",
};
const TASK_KIND_LABEL = {
  approval: "Approval",
  approval_chain: "Approval (management chain)",
  to_do: "To Do",
  initiation: "Sent back to you",
};

/* ---------------------------------------------------------------- API */
async function api(method, path, body) {
  const headers = { Accept: "application/json" };
  if (state.user) headers["X-User-Id"] = state.user.id;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) {
    let message = data && data.detail;
    if (Array.isArray(message)) {
      message = message.map((d) => `${(d.loc || []).slice(1).join(".") || "request"}: ${d.msg}`).join("; ");
    }
    const error = new Error(message || `Request failed (${res.status})`);
    error.status = res.status;
    throw error;
  }
  return data;
}

function toast(message, isError = false) {
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), isError ? 7000 : 3500);
}

async function run(button, action) {
  if (button) button.disabled = true;
  try {
    await action();
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (button && document.body.contains(button)) button.disabled = false;
  }
}

/* ------------------------------------------------------------- users */
function renderUserSelect() {
  const select = $("#user-select");
  select.innerHTML = state.users
    .map((u) => `<option value="${esc(u.id)}">${esc(u.name)} (${esc(u.title)})</option>`)
    .join("");
  select.value = state.user.id;
  select.onchange = async () => {
    state.user = state.users.find((u) => u.id === select.value);
    safeSet("bp-user", state.user.id);
    renderRoleChips();
    await loadReference();
    router();
  };
  renderRoleChips();
}

function renderRoleChips() {
  const u = state.user;
  const chips = [
    ...u.roles.map((r) => `<span class="chip role" title="Role-based security group">${esc(r.role)} · ${esc(r.org_name)}</span>`),
    ...u.groups.map((g) => `<span class="chip" title="User-based security group">${esc(g)}</span>`),
  ];
  $("#user-roles").innerHTML = chips.length ? chips.join("") : `<span class="chip">No security roles</span>`;
}

const isAdmin = () => state.user.groups.some((g) => g === "HR Administrator" || g === "Business Process Administrator");

async function loadReference() {
  [state.jobProfiles, state.orgs, state.workers] = await Promise.all([
    api("GET", "/api/job-profiles"),
    api("GET", "/api/orgs"),
    api("GET", "/api/workers"),
  ]);
}

async function refreshInboxCount() {
  try {
    const tasks = await api("GET", "/api/tasks");
    const badge = $("#inbox-count");
    badge.textContent = tasks.length;
    badge.hidden = tasks.length === 0;
  } catch { /* ignore */ }
}

/* ------------------------------------------------------------ router */
const routes = {
  inbox: renderInbox,
  start: renderStart,
  events: renderEvents,
  event: renderEvent,
  definitions: renderDefinitions,
  people: renderPeople,
  payroll: renderPayroll,
};

function parseHash() {
  const [path] = location.hash.replace(/^#\/?/, "").split("?");
  const parts = path.split("/");
  return { name: parts[0] || "inbox", arg: parts[1] };
}

async function router() {
  const { name, arg } = parseHash();
  const tab = name === "event" ? "events" : name;
  $$(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.route === tab));
  const render = routes[name] || renderInbox;
  view().innerHTML = `<div class="empty">Loading…</div>`;
  try {
    await render(arg);
  } catch (error) {
    view().innerHTML = `<div class="card empty">${esc(error.message)}</div>`;
  }
  refreshInboxCount();
}

/* ------------------------------------------------------------- inbox */
async function renderInbox() {
  const tasks = await api("GET", "/api/tasks");
  const today = todayIso();
  const items = tasks.map((t) => {
    const overdue = t.due_date && t.due_date < today;
    const subprocess = t.event.parent_event_id
      ? ` · part of <a href="#/event/${t.root_event_id}">#${t.root_event_id}</a>`
      : "";
    return `
      <div class="task ${overdue ? "overdue" : ""}">
        <div>
          <div class="title">${esc(t.step.name)}</div>
          <div class="sub">${esc(t.event.title)} · ${esc(t.event.org.name)} · initiated by ${esc(t.event.initiator.name)}${subprocess}</div>
          <div class="row small" style="margin-top:6px">
            <span class="chip">${esc(TASK_KIND_LABEL[t.kind] || t.kind)}</span>
            ${t.due_date ? `<span class="chip" style="${overdue ? "color:var(--bad)" : ""}">Due ${fmtDate(t.due_date)}</span>` : ""}
            <span class="chip">Effective ${fmtDate(t.event.effective_date)}</span>
          </div>
        </div>
        <div><a class="btn" href="#/event/${t.event.id}">Open</a></div>
      </div>`;
  });
  view().innerHTML = `
    <div class="page-head">
      <div>
        <h1>My Tasks</h1>
        <p>Approvals, To Dos and sent-back items assigned to ${esc(state.user.name)}.</p>
      </div>
      <a class="btn" href="#/start">Start a process</a>
    </div>
    ${
      items.length
        ? `<div class="task-list">${items.join("")}</div>`
        : `<div class="card empty">Nothing is waiting for you.<br>
             <span class="small">Switch the "Acting as" person at the top to see someone else's inbox, or start a new business process.</span></div>`
    }`;
}

/* ------------------------------------------------------ event detail */
async function renderEvent(id) {
  const e = await api("GET", `/api/events/${id}`);
  const actions = [];
  if (e.available_actions.cancel) actions.push(`<button class="danger" id="btn-cancel">Cancel</button>`);
  if (e.available_actions.rescind) actions.push(`<button class="danger" id="btn-rescind">Rescind</button>`);

  view().innerHTML = `
    <div class="page-head">
      <div>
        <div class="muted small">#${e.id} · ${esc(e.definition_name)}</div>
        <h1>${esc(e.title)}</h1>
      </div>
      <div class="row">${statusPill(e.status)}${actions.join("")}</div>
    </div>
    ${e.parent_event_id ? `<div class="callout info">This is a sub-process of <a href="#/event/${e.parent_event_id}">#${e.parent_event_id}</a>.</div>` : ""}
    ${e.my_open_tasks.map((t) => actionPanel(t, e)).join("")}
    ${
      e.warnings.length
        ? `<div class="callout warn"><strong>Validation warnings</strong><ul>${e.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>`
        : ""
    }
    <div class="card">
      <dl class="meta">
        <div><dt>Subject</dt><dd>${esc(e.subject.name)}${e.subject.id ? ` <span class="muted small">${esc(e.subject.id)}</span>` : ""}</dd></div>
        <div><dt>Organization</dt><dd>${esc(e.org.name)}</dd></div>
        <div><dt>Initiated by</dt><dd>${esc(e.initiator.name)}</dd></div>
        <div><dt>Effective date</dt><dd>${fmtDate(e.effective_date)}</dd></div>
        <div><dt>Started</dt><dd>${fmtDate(e.created_at)}</dd></div>
        <div><dt>${e.status === "In Progress" ? "Awaiting" : "Finished"}</dt><dd>${
          e.status === "In Progress" ? esc(e.awaiting.join(", ") || "—") : fmtDate(e.completed_at)
        }</dd></div>
      </dl>
    </div>
    <div class="card"><h2>Proposed data</h2>${proposedDataHtml(e)}</div>
    <div class="card">
      <h2>Process</h2>
      ${flowHtml(e)}
    </div>
    <div class="card">
      <h2>Process history</h2>
      <div class="table-wrap"><table>
        <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Step</th><th>Detail</th></tr></thead>
        <tbody>${e.history
          .map(
            (h) => `<tr><td class="small">${fmtDate(h.at)}</td><td>${esc(h.actor.name)}</td><td>${esc(h.action)}</td>
                     <td>${esc(h.step_name || "")}</td><td class="small">${esc(h.detail || "")}</td></tr>`
          )
          .join("")}</tbody>
      </table></div>
    </div>
    ${
      e.children.length
        ? `<div class="card"><h2>Sub-processes</h2><div class="table-wrap"><table><tbody>${e.children
            .map((c) => `<tr class="clickable" data-href="#/event/${c.id}"><td>#${c.id}</td><td>${esc(c.title)}</td><td>${statusPill(c.status)}</td></tr>`)
            .join("")}</tbody></table></div></div>`
        : ""
    }
    ${
      e.data_changes.length
        ? `<div class="card"><h2>Data changes</h2>${e.data_changes
            .map(
              (c) => `<p class="small muted">${esc(c.change_type)} on ${esc(c.worker_id)} at ${fmtDate(c.applied_at)}${
                c.reverted_at ? ` · reverted ${fmtDate(c.reverted_at)}` : ""
              }</p><div class="grid-2"><div><h3>Before</h3><pre class="json">${esc(JSON.stringify(c.before, null, 2))}</pre></div>
                 <div><h3>After</h3><pre class="json">${esc(JSON.stringify(c.after, null, 2))}</pre></div></div>`
            )
            .join("")}</div>`
        : ""
    }
    ${
      e.outbound.length
        ? `<div class="card"><h2>Integration records</h2>${e.outbound
            .map((m) => `<p class="small"><strong>${esc(m.record_type)}</strong> · ${esc(m.integration)} · ${fmtDate(m.created_at)}</p><pre class="json">${esc(JSON.stringify(m.payload, null, 2))}</pre>`)
            .join("")}</div>`
        : ""
    }
    <div class="card">
      <h2>Why this definition?</h2>
      <ol class="trace small">${e.definition_trace.map((t) => `<li>${esc(t)}</li>`).join("")}</ol>
    </div>`;

  bindRowLinks();
  bindEventActions(e);
}

function proposedDataHtml(e) {
  const d = e.data;
  const jobName = (id) => state.jobProfiles.find((j) => j.id === id)?.name || id;
  const orgName = (id) => state.orgs.find((o) => o.id === id)?.name || id;
  const rows = [];
  if (e.bp_type === "hire" || e.bp_type === "propose_compensation") {
    rows.push(["Name", esc(d.name)], ["Email", esc(d.email)], ["Job profile", esc(jobName(d.job_profile_id))],
      ["Organization", esc(orgName(d.org_id))], ["Salary", money(d.salary)], ["Hire date", fmtDate(d.effective_date)],
      ["Worker ID", `${esc(d.worker_id)} <span class="muted small">(assigned at initiation, created on completion)</span>`]);
    const jp = state.jobProfiles.find((j) => j.id === d.job_profile_id);
    if (jp) rows.push(["Pay range", `${money(jp.pay_range.min)} – ${money(jp.pay_range.max)} (mid ${money(jp.pay_range.mid)})`]);
    return kvTable(rows);
  }
  if (e.bp_type === "change_job") {
    const before = e.current || e.data_changes[0]?.before || {};
    const pct = before.salary ? (((d.salary - before.salary) / before.salary) * 100).toFixed(1) : null;
    const line = (label, from, to) => [label, from === to ? esc(to) : `${esc(from)} → <strong>${esc(to)}</strong>`];
    return kvTable([
      ["Reason", esc(d.reason)],
      line("Job profile", jobName(before.job_profile_id), jobName(d.job_profile_id)),
      line("Organization", orgName(before.org?.id || before.org_id), orgName(d.org_id)),
      [
        "Salary",
        before.salary === d.salary
          ? money(d.salary)
          : `${money(before.salary)} → <strong>${money(d.salary)}</strong>${pct !== null ? ` <span class="chip">${pct > 0 ? "+" : ""}${pct}%</span>` : ""}`,
      ],
      ["Effective date", fmtDate(d.effective_date)],
    ]);
  }
  return kvTable([["Reason", esc(d.reason)], ["Termination date", fmtDate(d.effective_date)]]);
}

function kvTable(rows) {
  return `<div class="table-wrap"><table><tbody>${rows
    .map(([k, v]) => `<tr><th style="width:180px">${esc(k)}</th><td>${v}</td></tr>`)
    .join("")}</tbody></table></div>`;
}

function flowHtml(e) {
  const groups = {};
  e.definition_steps.forEach((d) => (groups[d.order] ||= []).push(d));
  const orders = Object.keys(groups).sort();
  const finished = e.status !== "In Progress";

  const latest = (index) => {
    const instances = e.steps.filter((s) => s.step_index === index);
    return instances.filter((s) => s.active).pop() || instances.pop();
  };

  const card = (d) => {
    const inst = latest(d.index);
    let status = inst ? inst.status : finished ? "Not reached" : "Pending";
    if (inst && !inst.active && inst.status === "Completed") status = "Superseded";
    const cls = inst ? STATUS_CLASS[inst.status] || "idle" : "idle pending";
    let who = "";
    if (inst && inst.type === "approval_chain" && inst.chain.length) who = inst.chain.map((p) => p.name).join(" → ");
    else if (inst && inst.assignees.length && d.type !== "subprocess") who = inst.assignees.map((p) => p.name).join(", ");
    else if (d.type === "approval_chain") who = "Management chain";
    else if (d.groups.length) who = d.groups.join(", ");
    const link = inst && inst.child_event_id ? ` <a href="#/event/${inst.child_event_id}">#${inst.child_event_id}</a>` : "";
    const title = [d.entry_condition && `Entry: ${d.entry_condition}`, d.while_running_condition && `While running: ${d.while_running_condition}`, inst?.note]
      .filter(Boolean)
      .join("\n");
    return `<div class="flow-step ${cls}" title="${esc(title)}">
        <div class="type">${esc(STEP_TYPE_LABEL[d.type] || d.type)}</div>
        <div class="name">${esc(d.name)}${link}</div>
        ${who ? `<div class="who">${esc(who)}</div>` : ""}
        <div style="margin-top:6px">${statusPill(status)}</div>
        ${inst?.note && d.type !== "subprocess" ? `<div class="who">${esc(inst.note)}</div>` : ""}
      </div>`;
  };

  const columns = orders.map(
    (o, i) => `${i ? `<div class="flow-arrow" aria-hidden="true">&rarr;</div>` : ""}
      <div class="flow-group"><div class="flow-order">step ${esc(o)}${groups[o].length > 1 ? " · parallel" : ""}</div>${groups[o].map(card).join("")}</div>`
  );
  return `<div class="flow">${columns.join("")}</div>
    <p class="small muted" style="margin:10px 0 0">Steps with the same letter run in parallel; the next letter starts only when all of them are done. Hover a step to see its conditions.</p>`;
}

function actionPanel(task, e) {
  const id = task.id;
  const due = task.due_date ? `Due ${fmtDate(task.due_date)}.` : "";
  const comment = (placeholder) => `
    <div class="field" style="margin-top:10px"><label for="comment-${id}">Comment</label>
      <textarea id="comment-${id}" placeholder="${esc(placeholder)}"></textarea></div>`;

  if (task.kind === "approval" || task.kind === "approval_chain") {
    const targets = task.send_back_targets
      .map((t) => `<option value="${t.id}">${esc(t.name)} (${esc(t.completed_by?.name || "")})</option>`)
      .join("");
    return `<div class="card action-panel">
      <h2>Your action: ${esc(task.step.name)}</h2>
      <p class="muted small" style="margin:6px 0 0">${due} Review the proposed data and process below, then decide.</p>
      ${comment("Optional for Approve; required for Send Back and Deny")}
      <div class="buttons">
        <button class="primary" data-act="approve" data-task="${id}">Approve</button>
        ${targets ? `<span class="send-back"><select id="target-${id}" aria-label="Send back to">${targets}</select>
          <button data-act="send-back" data-task="${id}">Send Back</button></span>` : ""}
        <button class="danger" data-act="deny" data-task="${id}">Deny</button>
      </div>
    </div>`;
  }
  if (task.kind === "to_do") {
    return `<div class="card action-panel">
      <h2>To Do: ${esc(task.step.name)}</h2>
      <p style="margin:6px 0 0">${esc(task.instructions || "")}</p>
      <p class="muted small" style="margin:4px 0 0">${due}</p>
      ${comment("What did you do?")}
      <div class="buttons"><button class="primary" data-act="done" data-task="${id}">Mark Done</button></div>
    </div>`;
  }
  // initiation: sent back to the initiator
  return `<div class="card action-panel">
    <h2>Sent back to you: ${esc(task.step.name)}</h2>
    <p class="muted small" style="margin:6px 0 0">Check the comment in the process history, update the data and resubmit. The routing starts again from the next step.</p>
    <div class="form-grid" style="margin-top:12px">${resubmitFields(e, id)}</div>
    ${comment("What did you change?")}
    <div class="buttons"><button class="primary" data-act="resubmit" data-task="${id}">Resubmit</button></div>
  </div>`;
}

function resubmitFields(e, id) {
  const d = e.data;
  const jobOptions = (selected) =>
    state.jobProfiles.map((j) => `<option value="${esc(j.id)}" ${j.id === selected ? "selected" : ""}>${esc(j.name)}</option>`).join("");
  const orgOptions = (selected) =>
    state.orgs.map((o) => `<option value="${esc(o.id)}" ${o.id === selected ? "selected" : ""}>${esc(o.name)}</option>`).join("");
  const f = (name, label, input) => `<div class="field"><label for="rs-${name}-${id}">${label}</label>${input}</div>`;
  const fields = [];
  if (e.bp_type === "hire") {
    fields.push(
      f("name", "Name", `<input id="rs-name-${id}" data-rs="name" value="${esc(d.name)}">`),
      f("email", "Email", `<input id="rs-email-${id}" data-rs="email" value="${esc(d.email)}">`),
      f("job_profile_id", "Job profile", `<select id="rs-job_profile_id-${id}" data-rs="job_profile_id">${jobOptions(d.job_profile_id)}</select>`)
    );
  }
  if (e.bp_type === "change_job") {
    fields.push(
      f("job_profile_id", "Job profile", `<select id="rs-job_profile_id-${id}" data-rs="job_profile_id">${jobOptions(d.job_profile_id)}</select>`),
      f("org_id", "Organization", `<select id="rs-org_id-${id}" data-rs="org_id">${orgOptions(d.org_id)}</select>`)
    );
  }
  if (e.bp_type === "hire" || e.bp_type === "change_job") {
    fields.push(f("salary", "Salary", `<input id="rs-salary-${id}" data-rs="salary" type="number" min="1" step="1000" value="${esc(d.salary)}">`));
  }
  fields.push(f("effective_date", "Effective date", `<input id="rs-effective_date-${id}" data-rs="effective_date" type="date" value="${esc(d.effective_date)}">`));
  return fields.join("");
}

function bindEventActions(e) {
  const reload = () => renderEvent(e.id).then(refreshInboxCount);
  const commentOf = (id) => ($(`#comment-${id}`)?.value || "").trim() || null;

  $$("[data-act]").forEach((button) => {
    const id = button.dataset.task;
    button.addEventListener("click", () =>
      run(button, async () => {
        const act = button.dataset.act;
        const comment = commentOf(id);
        let result;
        if (act === "approve") result = await api("POST", `/api/tasks/${id}/approve`, { comment });
        if (act === "deny") result = await api("POST", `/api/tasks/${id}/deny`, { comment });
        if (act === "send-back") {
          result = await api("POST", `/api/tasks/${id}/send-back`, { comment, to_step_id: Number($(`#target-${id}`).value) });
        }
        if (act === "done") result = await api("POST", `/api/tasks/${id}/submit`, { comment });
        if (act === "resubmit") {
          const data = {};
          $$("[data-rs]").forEach((input) => {
            const value = input.value.trim();
            if (value) data[input.dataset.rs] = input.dataset.rs === "salary" ? Number(value) : value;
          });
          result = await api("POST", `/api/tasks/${id}/submit`, { comment, data });
        }
        toast({ approve: "Approved", deny: "Denied", "send-back": "Sent back", done: "Marked done", resubmit: "Resubmitted" }[act]);
        // A send back from a sub-process lands in the parent: follow it there.
        if (result && result.id !== e.id) {
          location.hash = `#/event/${result.id}`;
          return;
        }
        await reload();
      })
    );
  });

  const cancel = $("#btn-cancel");
  if (cancel) {
    cancel.addEventListener("click", () =>
      run(cancel, async () => {
        const reason = prompt("Cancel this business process? It stops immediately and cannot be restarted.\n\nReason:");
        if (reason === null) return;
        await api("POST", `/api/events/${e.id}/cancel`, { comment: reason || null });
        toast("Canceled");
        await reload();
      })
    );
  }
  const rescind = $("#btn-rescind");
  if (rescind) {
    rescind.addEventListener("click", () =>
      run(rescind, async () => {
        const reason = prompt("Rescind this completed business process? Worker data is restored to what it was before.\n\nReason:");
        if (reason === null) return;
        await api("POST", `/api/events/${e.id}/rescind`, { comment: reason || null });
        toast("Rescinded");
        await loadReference();
        await reload();
      })
    );
  }
}

function bindRowLinks() {
  $$("tr[data-href]").forEach((tr) => tr.addEventListener("click", () => (location.hash = tr.dataset.href)));
}

/* ------------------------------------------------------ start a process */
let startKind = "hire";

async function renderStart() {
  const kinds = [["hire", "Hire"], ["change_job", "Change Job"], ["termination", "Termination"]];
  view().innerHTML = `
    <div class="page-head"><div><h1>Start a Process</h1>
      <p>Initiate a business process as ${esc(state.user.name)}. The panel on the right shows which definition will be used and whether you are allowed to start it.</p></div></div>
    <div class="segmented" role="tablist">${kinds
      .map(([k, label]) => `<button role="tab" data-kind="${k}" class="${k === startKind ? "on" : ""}" aria-selected="${k === startKind}">${label}</button>`)
      .join("")}</div>
    <div class="grid-2">
      <form class="card" id="start-form" novalidate>${startFormHtml(startKind)}
        <div class="buttons" style="margin-top:16px"><button class="primary" type="submit">Submit</button></div>
      </form>
      <div class="card" id="preview"><div class="muted">Choose an organization or worker to preview routing.</div></div>
    </div>`;

  $$(".segmented button").forEach((b) => b.addEventListener("click", () => { startKind = b.dataset.kind; renderStart(); }));
  const form = $("#start-form");
  form.addEventListener("change", () => updateStartPreview());
  form.addEventListener("input", (ev) => { if (ev.target.name === "salary") updateStartPreview(); });
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    run($("button[type=submit]", form), submitStartForm);
  });
  prefillFromWorker();
  updateStartPreview();
}

function startFormHtml(kind) {
  const orgOptions = state.orgs.map((o) => `<option value="${esc(o.id)}">${esc(o.name)}</option>`).join("");
  const jobOptions = state.jobProfiles
    .map((j) => `<option value="${esc(j.id)}">${esc(j.name)} (${money(j.pay_range.min)}–${money(j.pay_range.max)})</option>`)
    .join("");
  const workerOptions = state.workers
    .filter((w) => w.id !== state.user.id)
    .map((w) => `<option value="${esc(w.id)}">${esc(w.name)} · ${esc(w.job_profile.name)} · ${esc(w.org.name)}</option>`)
    .join("");
  const field = (label, input, hint = "", full = false) => {
    const name = /name="([^"]+)"/.exec(input)[1];
    const control = input.replace(/^<(\w+)/, `<$1 id="f-${name}"`);
    return `<div class="field ${full ? "full" : ""}"><label for="f-${name}">${label}</label>${control}${
      hint ? `<span class="hint">${hint}</span>` : ""
    }</div>`;
  };

  if (kind === "hire") {
    return `<h2>Hire</h2><div class="form-grid" style="margin-top:12px">
      ${field("Full name", `<input name="name" required placeholder="Jordan Blake">`)}
      ${field("Email", `<input name="email" type="email" required placeholder="jordan.blake@summit-analytics.example">`)}
      ${field("Supervisory organization", `<select name="org_id">${orgOptions}</select>`)}
      ${field("Job profile", `<select name="job_profile_id">${jobOptions}</select>`)}
      ${field("Annual salary (USD)", `<input name="salary" type="number" min="1" step="1000" value="120000">`, "Above the pay range midpoint needs Compensation Partner approval.")}
      ${field("Hire date", `<input name="effective_date" type="date" value="${addDays(21)}">`)}
    </div>`;
  }
  if (kind === "change_job") {
    return `<h2>Change Job</h2><div class="form-grid" style="margin-top:12px">
      ${field("Worker", `<select name="worker_id">${workerOptions}</select>`, "", true)}
      ${field("Reason", `<select name="reason"><option>Promotion</option><option>Transfer</option><option>Merit Increase</option><option>Market Adjustment</option><option>Other</option></select>`)}
      ${field("Effective date", `<input name="effective_date" type="date" value="${addDays(14)}">`)}
      ${field("Job profile", `<select name="job_profile_id">${jobOptions}</select>`)}
      ${field("Supervisory organization", `<select name="org_id">${orgOptions}</select>`)}
      ${field("Annual salary (USD)", `<input name="salary" type="number" min="1" step="1000">`, `<span id="pay-change"></span>`, true)}
    </div>`;
  }
  return `<h2>Termination</h2><div class="form-grid" style="margin-top:12px">
    ${field("Worker", `<select name="worker_id">${workerOptions}</select>`, "", true)}
    ${field("Reason", `<select name="reason"><option>Resignation</option><option>Retirement</option><option>Layoff</option><option>Performance</option><option>Other</option></select>`)}
    ${field("Termination date", `<input name="effective_date" type="date" value="${addDays(14)}">`)}
  </div>`;
}

function formValues() {
  const values = {};
  new FormData($("#start-form")).forEach((v, k) => (values[k] = String(v).trim()));
  return values;
}

function prefillFromWorker() {
  if (startKind !== "change_job") return;
  const form = $("#start-form");
  const select = form.elements.worker_id;
  const apply = () => {
    const w = state.workers.find((x) => x.id === select.value);
    if (!w) return;
    form.elements.job_profile_id.value = w.job_profile.id;
    form.elements.org_id.value = w.org.id;
    form.elements.salary.value = w.salary;
    updateStartPreview();
  };
  select.addEventListener("change", apply);
  apply();
}

async function updateStartPreview() {
  const values = formValues();
  let orgId = values.org_id;
  if (startKind !== "hire") {
    const w = state.workers.find((x) => x.id === values.worker_id);
    orgId = w?.org.id;
    const payChange = $("#pay-change");
    if (payChange && w && w.salary && values.salary) {
      const pct = ((Number(values.salary) - w.salary) / w.salary) * 100;
      payChange.textContent = `Current ${money(w.salary)} · change ${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%. Over 10% needs Compensation Partner approval; over 15% also goes up the management chain.`;
    }
  }
  const preview = $("#preview");
  if (!orgId) return;
  try {
    const r = await api("GET", `/api/business-processes/${startKind}/definition?org_id=${encodeURIComponent(orgId)}`);
    preview.innerHTML = `
      <h2>Routing preview</h2>
      <p style="margin:8px 0">${
        r.you_can_initiate
          ? `<span class="status ok">You can initiate this</span>`
          : `<span class="status bad">You cannot initiate this</span> <span class="small muted">Needs: ${esc(r.security_policy.initiate.join(" or "))}</span>`
      }</p>
      <p style="margin:0 0 4px"><strong>${esc(r.definition.definition_name)}</strong></p>
      <ol class="trace small muted">${r.trace.map((t) => `<li>${esc(t)}</li>`).join("")}</ol>
      <div style="margin-top:10px">${r.definition.steps.map(defStepHtml).join("")}</div>`;
  } catch (error) {
    preview.innerHTML = `<div class="muted">${esc(error.message)}</div>`;
  }
}

async function submitStartForm() {
  const v = formValues();
  let path;
  let body;
  if (startKind === "hire") {
    path = "/api/events/hire";
    body = { name: v.name, email: v.email, org_id: v.org_id, job_profile_id: v.job_profile_id, salary: Number(v.salary), effective_date: v.effective_date };
  } else if (startKind === "change_job") {
    path = "/api/events/change-job";
    body = { worker_id: v.worker_id, reason: v.reason, effective_date: v.effective_date, job_profile_id: v.job_profile_id, org_id: v.org_id, salary: Number(v.salary) };
  } else {
    path = "/api/events/termination";
    body = { worker_id: v.worker_id, reason: v.reason, effective_date: v.effective_date };
  }
  const event = await api("POST", path, body);
  toast(`${event.title} started (#${event.id})`);
  location.hash = `#/event/${event.id}`;
}

/* ------------------------------------------------------ events list */
async function renderEvents() {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const status = params.get("status") || "";
  const events = await api("GET", `/api/events${status ? `?status=${encodeURIComponent(status)}` : ""}`);
  const statuses = ["", "In Progress", "Successfully Completed", "Denied", "Canceled", "Rescinded"];
  view().innerHTML = `
    <div class="page-head">
      <div><h1>Business Processes</h1><p>Events you can see as ${esc(state.user.name)}. Visibility follows your roles on each organization.</p></div>
      <label class="row small">Status
        <select id="status-filter" style="width:auto">${statuses
          .map((s) => `<option value="${esc(s)}" ${s === status ? "selected" : ""}>${s || "All"}</option>`)
          .join("")}</select></label>
    </div>
    <div class="card">${
      events.length
        ? `<div class="table-wrap"><table>
            <thead><tr><th>#</th><th>Business process</th><th>Organization</th><th>Initiated by</th><th>Effective</th><th>Status</th><th>Awaiting</th></tr></thead>
            <tbody>${events
              .map(
                (e) => `<tr class="clickable" data-href="#/event/${e.id}"><td>${e.id}</td><td>${esc(e.title)}</td><td>${esc(e.org.name)}</td>
                   <td>${esc(e.initiator.name)}</td><td>${fmtDate(e.effective_date)}</td><td>${statusPill(e.status)}</td>
                   <td class="small">${esc(e.awaiting.join(", "))}</td></tr>`
              )
              .join("")}</tbody></table></div>`
        : `<div class="empty">No business processes yet. <a href="#/start">Start one</a>.</div>`
    }</div>`;
  $("#status-filter").addEventListener("change", (ev) => {
    location.hash = ev.target.value ? `#/events?status=${encodeURIComponent(ev.target.value)}` : "#/events";
  });
  bindRowLinks();
}

/* ------------------------------------------------------ definitions */
let defType = "hire";
let defOrg = "ORG-210";

function defStepHtml(s) {
  const bits = [];
  if (s.groups?.length) bits.push(`Assigned to: ${s.groups.join(", ")}`);
  if (s.type === "approval_chain") bits.push("Assigned to: managers up the supervisory org hierarchy");
  if (s.alternate_groups?.length) bits.push(`Alternate: ${s.alternate_groups.join(", ")}`);
  if (s.routing_restrictions?.length) bits.push(`Restrictions: ${s.routing_restrictions.map((r) => r.replaceAll("_", " ")).join(", ")}`);
  if (s.subprocess) bits.push(`Runs sub-process: ${s.subprocess}`);
  if (s.integration) bits.push(`Sends to: ${s.integration}`);
  if (s.due_in_days != null) bits.push(`Due in ${s.due_in_days} day(s)`);
  return `<div class="def-step">
      <div class="def-order">${esc(s.order)}</div>
      <div>
        <div><strong>${esc(s.name)}</strong> <span class="chip">${esc(STEP_TYPE_LABEL[s.type] || s.type)}</span></div>
        ${bits.length ? `<div class="small muted">${esc(bits.join(" · "))}</div>` : ""}
        ${s.entry_condition ? `<code class="cond">entry: ${esc(s.entry_condition.trim())}</code>` : ""}
        ${s.while_running_condition ? `<code class="cond">while running: ${esc(s.while_running_condition.trim())}</code>` : ""}
      </div>
    </div>`;
}

async function renderDefinitions() {
  const types = await api("GET", "/api/business-processes");
  const r = await api("GET", `/api/business-processes/${defType}/definition?org_id=${encodeURIComponent(defOrg)}`);
  const d = r.definition;
  const current = types.find((t) => t.bp_type === defType);
  view().innerHTML = `
    <div class="page-head">
      <div><h1>Definitions</h1><p>How each business process type is routed. Edit the YAML files in <code>definitions/</code> and restart to change them.</p></div>
      <div class="row">
        <select id="def-type" style="width:auto">${types.map((t) => `<option value="${t.bp_type}" ${t.bp_type === defType ? "selected" : ""}>${esc(t.label)}</option>`).join("")}</select>
        <select id="def-org" style="width:auto">${state.orgs.map((o) => `<option value="${o.id}" ${o.id === defOrg ? "selected" : ""}>${esc(o.name)}</option>`).join("")}</select>
      </div>
    </div>
    <div class="grid-2">
      <div class="card">
        <h2>${esc(d.definition_name)} ${d.org_id ? `<span class="chip role">organization-specific</span>` : `<span class="chip">default</span>`}</h2>
        ${d.description ? `<p class="muted small" style="margin-top:0">${esc(d.description)}</p>` : ""}
        ${d.steps.map(defStepHtml).join("")}
      </div>
      <div>
        <div class="card">
          <h2>Definition lookup</h2>
          <ol class="trace small">${r.trace.map((t) => `<li>${esc(t)}</li>`).join("")}</ol>
          <p class="small muted" style="margin-bottom:0">All definitions for this type: ${current.definitions
            .map((x) => `${esc(x.definition_name)}${x.org_id ? ` (${esc(x.org_id)})` : ""}`)
            .join("; ")}</p>
        </div>
        <div class="card">
          <h2>Security policy</h2>
          <p class="small muted" style="margin-top:0">One policy per business process type, shared by all its definitions.</p>
          ${kvTable([
            ["Initiate", esc(r.security_policy.initiate.join(", ") || "Nobody (sub-process only)")],
            ["Cancel", esc(r.security_policy.cancel.join(", ") || "Nobody")],
            ["Rescind", esc(r.security_policy.rescind.join(", ") || "Nobody")],
          ])}
        </div>
        <div class="card">
          <h2>Validations</h2>
          ${
            d.validations.length
              ? d.validations
                  .map(
                    (v) => `<div style="margin-bottom:10px"><span class="status ${v.severity === "critical" ? "bad" : "warn"}">${esc(v.severity)}</span>
                      <strong>${esc(v.name)}</strong><div class="small">${esc(v.message)}</div><code class="cond">error when: ${esc(v.error_when.trim())}</code></div>`
                  )
                  .join("")
              : `<p class="muted small">None.</p>`
          }
        </div>
      </div>
    </div>`;
  $("#def-type").addEventListener("change", (ev) => { defType = ev.target.value; renderDefinitions(); });
  $("#def-org").addEventListener("change", (ev) => { defOrg = ev.target.value; renderDefinitions(); });
}

/* ------------------------------------------------------ people & orgs */
async function renderPeople() {
  const workers = await api("GET", "/api/workers?include_terminated=true");
  const children = {};
  state.orgs.forEach((o) => (children[o.superior_id || "root"] ||= []).push(o));
  const tree = (parent) =>
    (children[parent] || [])
      .map(
        (o) => `<li><div class="org-node"><strong>${esc(o.name)}</strong> <span class="muted small">${esc(o.id)} · ${o.members.length} member(s)</span>
          <div class="roles">${o.roles.map((r) => `<span class="chip role">${esc(r.role)}: ${esc(r.worker.name)}</span>`).join("") || `<span class="chip">Roles inherited</span>`}</div></div>
          ${children[o.id] ? `<ul>${tree(o.id)}</ul>` : ""}</li>`
      )
      .join("");
  view().innerHTML = `
    <div class="page-head">
      <div><h1>People &amp; Orgs</h1><p>Supervisory organizations and role assignments. Roles on an organization are inherited by its sub-organizations unless they have their own.</p></div>
      ${isAdmin() ? `<button id="btn-reset">Reset demo data</button>` : ""}
    </div>
    <div class="card"><h2>Supervisory organizations</h2><ul class="org-tree">${tree("root")}</ul></div>
    <div class="card"><h2>Workers</h2><div class="table-wrap"><table>
      <thead><tr><th>ID</th><th>Name</th><th>Job profile</th><th>Organization</th><th class="num">Salary</th><th>Hire date</th><th>Status</th></tr></thead>
      <tbody>${workers
        .map(
          (w) => `<tr><td>${esc(w.id)}</td><td>${esc(w.name)}</td><td>${esc(w.job_profile.name)}</td><td>${esc(w.org.name)}</td>
            <td class="num">${w.salary == null ? `<span class="muted" title="Only managers, HR and administrators can see pay">hidden</span>` : money(w.salary)}</td><td>${fmtDate(w.hire_date)}</td>
            <td>${statusPill(w.status)}</td></tr>`
        )
        .join("")}</tbody></table></div></div>`;
  const reset = $("#btn-reset");
  if (reset) {
    reset.addEventListener("click", () =>
      run(reset, async () => {
        if (!confirm("Delete all business processes and restore the original demo data?")) return;
        await api("POST", "/api/admin/reset");
        state.users = await api("GET", "/api/users");
        await loadReference();
        toast("Demo data reset");
        router();
      })
    );
  }
}

/* ------------------------------------------------------ payroll feed */
async function renderPayroll() {
  let messages;
  try {
    messages = await api("GET", "/api/integrations/outbound");
  } catch (error) {
    if (error.status !== 403) throw error;
    view().innerHTML = `<div class="page-head"><div><h1>Payroll Feed</h1></div></div>
      <div class="card empty">${esc(error.message)}.<br><span class="small">Switch to Sofia Martins, Daniel Kim or Chen Wei to view it.</span></div>`;
    return;
  }
  view().innerHTML = `
    <div class="page-head"><div><h1>Payroll Feed</h1>
      <p>Records sent by Integration steps to a simulated third-party payroll system. The format is loosely inspired by effective-dated change feeds such as Workday's Payroll Effective Change Interface (PECI); it is not the real PECI format.</p></div></div>
    <div class="card">${
      messages.length
        ? `<div class="table-wrap"><table>
            <thead><tr><th>#</th><th>Record</th><th>Worker</th><th>Effective</th><th>Business process</th><th>Sent</th><th>Payload</th></tr></thead>
            <tbody>${messages
              .map(
                (m) => `<tr><td>${m.id}</td><td><strong>${esc(m.record_type)}</strong></td><td>${esc(m.payload.worker_name || m.worker_id || "")}</td>
                  <td>${fmtDate(m.effective_date)}</td><td><a href="#/event/${m.event_id}">#${m.event_id}</a></td><td class="small">${fmtDate(m.created_at)}</td>
                  <td><details><summary class="small">JSON</summary><pre class="json">${esc(JSON.stringify(m.payload, null, 2))}</pre></details></td></tr>`
              )
              .join("")}</tbody></table></div>`
        : `<div class="empty">No records yet. They appear when a business process reaches its "Send to Payroll" step.</div>`
    }</div>`;
}

/* ------------------------------------------------------------ start */
async function init() {
  try {
    state.users = await api("GET", "/api/users");
    const saved = safeGet("bp-user");
    state.user = state.users.find((u) => u.id === saved) || state.users.find((u) => u.id === "W1005") || state.users[0];
    renderUserSelect();
    await loadReference();
    window.addEventListener("hashchange", router);
    await router();
  } catch (error) {
    view().innerHTML = `<div class="card empty">Could not reach the API: ${esc(error.message)}</div>`;
  }
}

init();
