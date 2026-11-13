// Franziska web UI: no build step, no dependencies. Views: chat, today, money, workflows, data, memory.
import { flowForm, runLabel } from "/static/flows.js";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
};
const store = {  // per-device conveniences only; the page works without them
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

let status = { labels: {}, modes: [], mode: "auto" };

// ---- server ----------------------------------------------------------------------------------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  if (res.status === 401) { location.reload(); throw new Error("not signed in"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

// POST (JSON, or a FormData with a file) and read NDJSON events as they arrive: status lines, then the reply.
async function stream(path, body, onEvent) {
  const form = body instanceof FormData;
  const res = await fetch(path, { method: "POST", headers: form ? {} : { "Content-Type": "application/json" }, body: form ? body : JSON.stringify(body) });
  if (res.status === 401) { location.reload(); return; }
  if (!res.ok) { onEvent({ type: "reply", text: (await res.json().catch(() => ({}))).detail || res.statusText, stopped: "error" }); return; }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) onEvent(JSON.parse(line));
    }
  }
}

// ---- formatting ------------------------------------------------------------------------------
const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// The agents write light markdown: **bold**, `code`, [text](url). Everything else stays as typed.
// ![name](/api/artifacts/…) is a chart run_python made (app.py, Web.event): shown inline, a tap opens it.
const rich = (s) => esc(s)
  .replace(/^(\s*)[*-]\s+/gm, "$1• ")
  .replace(/\n{3,}/g, "\n\n")
  .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
  .replace(/`([^`\n]+)`/g, "<code>$1</code>")
  .replace(/!\[([^\]\n]+)\]\((\/api\/artifacts\/[\w%.\/-]+)\)/g, '<a href="$2" target="_blank" rel="noopener"><img class="artifact" src="$2" alt="$1"></a>')
  .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+|\/api\/reports\/[\w.-]+|\/api\/artifacts\/[\w%.\/-]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
const routeClass = (route) => ["life", "money", "mail", "chat", "web", "cloud", "pc"].includes(route) ? route : "flow";
const dayKey = (iso) => iso.slice(0, 10);
const asDate = (iso) => new Date(iso.length === 10 ? iso + "T00:00" : iso);
const fmtDay = (iso, opts = { weekday: "long", day: "numeric", month: "long" }) => asDate(iso).toLocaleDateString("en-GB", opts);
const fmtTime = (iso) => iso.length === 10 ? "" : iso.slice(11, 16);
function relDay(iso) {
  const today = status.today || new Date().toISOString().slice(0, 10);
  const d = (asDate(dayKey(iso)) - asDate(today)) / 864e5;
  return d === 0 ? "Today" : d === 1 ? "Tomorrow" : d === -1 ? "Yesterday" : fmtDay(iso, { weekday: "long", day: "numeric", month: "short" });
}

let toastTimer;
function toast(text, action) {
  const el = $("#toast");
  el.replaceChildren(h("span", {}, text), action ? h("button", { onclick: () => { el.classList.remove("on"); action.run(); } }, action.label) : "");
  el.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("on"), action ? 6000 : 3500);
}

// ---- views -----------------------------------------------------------------------------------
const views = { chat: showChat, today: showToday, money: showMoney, flows: showFlows, code: showCode, data: showData, memory: showMemory, setup: showSetup, settings: showSettings };
const showing = (name) => $(`#view-${name}`).classList.contains("on");
function go() {  // '#data/ledger/movements': the view, then what it opens
  const [hash, ...rest] = location.hash.slice(1).split("/").map(decodeURIComponent);
  const saved = store.get("view");
  const name = hash in views ? hash : saved in views ? saved : "chat";
  store.set("view", name);
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("on", v.id === `view-${name}`));
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("on", a.dataset.view === name));
  views[name](...rest);
}

// ---- status: the desktop pill and the mode switch -------------------------------------------
async function refreshStatus() {
  const desk = $("#desk");
  try { status = await api("/api/status"); } catch {
    desk.classList.remove("up"); desk.classList.add("off");
    $(".desk-text", desk).textContent = "Can't reach the box";
    return;
  }
  desk.classList.toggle("up", status.desktop);
  desk.classList.toggle("off", !status.desktop);
  $(".desk-text", desk).textContent = status.desktop ? "Desktop on" : status.can_wake ? "Desktop off · wake" : "Desktop off";
  renderModes();
  showContext(status.tokens);
}
// "3.2k / 32k": what the model holds after the last turn; amber from 75 %
const kilo = (n) => (n >= 10000 ? Math.round(n / 1000) + "k" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n));
function showContext(tokens) {
  const el = $("#ctx"), win = status.window;
  el.classList.toggle("warm", !!tokens && !!win && tokens / win > 0.75);
  el.replaceChildren(...(tokens == null || !win ? [] : [`${kilo(tokens)} / ${kilo(win)}`,
    h("i", {}, h("b", { style: `width:${Math.min(100, (tokens / win) * 100).toFixed(0)}%` }))]));
}
// New chat (the button, /clear or /new): forget the recent turns and wipe them off the screen.
async function newChat() {
  if (busy) return;
  try { await api("/api/new", { method: "POST" }); } catch (e) { toast(e.message); return; }
  log.replaceChildren();
  lastDay = null;
  emptyChat();
  showContext(null);
}
$("#new-chat").addEventListener("click", newChat);
$("#desk").addEventListener("click", async () => {
  if (status.desktop || !status.can_wake) return;
  $(".desk-text", $("#desk")).textContent = "Waking…";
  try { const r = await api("/api/desktop/wake", { method: "POST" }); toast(r.up ? "The desktop is up." : "The desktop didn't come up."); }
  catch (e) { toast(e.message); }
  refreshStatus();
});

// Telemetry (Phoenix): a dot on its link while it runs. The link itself opens /phoenix/, which starts it if needed.
let telemetry = { state: "unknown" };
async function refreshTelemetry() {
  try { telemetry = await api("/api/telemetry"); } catch { telemetry = { state: "unknown" }; }
  const link = $("#tele");
  link.dataset.state = telemetry.state;
  link.title = { up: "Phoenix is running: open it", starting: "Phoenix is starting…", missing: "Phoenix isn't installed as a service (deploy/franziska-phoenix.service)" }[telemetry.state]
    || "Phoenix: every run's model calls and tool calls. Starts on the first click.";
  return telemetry;
}
$("#tele").addEventListener("click", () => setTimeout(refreshTelemetry, 4000));

const MODE_LABELS = { auto: "Auto", life: "📅 Life", money: "💶 Money" };
function renderModes() {
  $("#modes").replaceChildren(...status.modes.map((m) => h("button", {
    type: "button", role: "radio", "aria-checked": String(status.mode === m),
    title: m === "auto" ? "The router picks the assistant for each message" : `Every message goes to ${m}`,
    onclick: async () => { status = { ...status, ...(await api("/api/mode", { method: "POST", body: { mode: m } })) }; renderModes(); },
  }, MODE_LABELS[m])));
}

// ---- chat ------------------------------------------------------------------------------------
const log = $("#log");
let loaded = false, busy = false, lastDay = null;

async function showChat() {
  $('.nav a[data-view="chat"]').classList.remove("unread");
  if (!loaded) {
    loaded = true;
    const turns = await api("/api/chat").catch(() => []);
    log.replaceChildren();
    for (const t of turns) {
      daySep(t.at);
      addMine(t.text);
      addHers({ text: t.reply || "(no reply)", route: t.route, label: t.label, actions: [], at: t.at });
    }
    if (!turns.length) emptyChat();
    scrollDown();
  }
  renderGlance();
  $("#input").focus({ preventScroll: true });
}

function emptyChat() {
  const ask = (t) => { $("#input").value = t; send(); };
  log.append(h("div", { class: "empty-chat" },
    h("h2", {}, "Ciao, I'm Franziska."),
    h("div", {}, "Your schedule, notes, money and mail, on your own machines. Ask in Italian or English."),
    h("div", { class: "suggest" }, ["Cosa ho oggi?", "Quanto ho speso questo mese?", "Ricordami di chiamare la banca domani alle 10", "Bollette del condominio dell'ultimo mese"]
      .map((t) => h("button", { type: "button", onclick: () => ask(t) }, t))),
    h("div", { class: "hint" }, "Type ", h("code", {}, "/"), " for a workflow, or ", h("a", { href: "#flows" }, "run one with its settings"), ".")));
}

function daySep(at) {
  const day = dayKey(at);
  if (day !== lastDay) { lastDay = day; log.append(h("div", { class: "day-sep" }, relDay(day))); }
}
const scrollDown = () => requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });

function addMine(text) {
  $(".empty-chat", log)?.remove();
  log.append(h("div", { class: "msg me" }, h("div", { class: "bubble" }, text)));
}

function addHers(r) {
  const msg = herMsg(r);
  log.append(msg);
  return msg;
}

function herMsg(r) {
  const actions = h("div", { class: "actions" });
  const msg = h("div", { class: `msg her${r.stopped ? " stopped" : ""}` },
    h("div", { class: "bubble", html: rich(r.text) }),
    h("div", { class: "meta" },
      r.label ? h("span", { class: `chip ${routeClass(r.route)}` }, r.label) : "",
      r.stopped ? h("span", { class: "chip warn", title: r.stopped }, r.stopped === "busy" ? "busy" : "stopped") : "",
      h("span", { class: "at" }, fmtTime(r.at || new Date().toLocaleString("sv").slice(0, 16))),
      h("button", { class: "copy", type: "button", title: "Copy the reply", onclick: async (ev) => {
        const b = ev.currentTarget;
        try { await navigator.clipboard.writeText(r.text); b.textContent = "Copied"; setTimeout(() => (b.textContent = "Copy"), 1500); }
        catch { toast("The browser didn't allow copying here."); }
      } }, "Copy")),
    r.steps?.length ? h("details", { class: "steps" }, h("summary", {}, `${r.steps.length} step${r.steps.length > 1 ? "s" : ""}`),
      ...r.steps.map((t) => h("div", {}, t))) : "",
    actions);
  for (const a of r.actions || []) {
    actions.append(h("button", { type: "button", onclick: (ev) => tap(a.id, ev.currentTarget, msg) }, a.label));
  }
  return msg;
}

function addStatus(text) {
  const el = h("div", { class: "status" }, h("span", { class: "typing" }, h("i"), h("i"), h("i")), h("span", {}, text));
  log.append(el);
  return el;
}

async function converse(path, body) {
  busy = true;
  $("#send").disabled = true;
  const line = addStatus("…");
  scrollDown();
  try {
    await stream(path, body, (ev) => {
      if (ev.type === "status") { $("span:last-child", line).textContent = ev.text; }
      else if (ev.type === "applied") { applied(ev); }
      else if (ev.type === "reply") {
        line.remove(); addHers(ev);
        if (ev.window) status.window = ev.window;
        if (ev.tokens != null) showContext(ev.tokens);
        if (!showing("chat")) $('.nav a[data-view="chat"]').classList.add("unread");
      }
      scrollDown();
    });
  } catch (e) {
    line.remove();
    addHers({ text: `I couldn't reach the server (${e.message}).`, stopped: "error" });
  } finally {
    line.remove();
    busy = false;
    $("#send").disabled = false;
    if (showing("today")) showToday(); else renderGlance();
    if (showing("flows")) showFlows();
    scrollDown();
  }
}

let pendingTap = null;
function applied(ev) {
  if (!pendingTap) return;
  const { button, msg } = pendingTap;
  button.remove();
  msg.append(h("div", { class: "applied" }, "✓ " + ev.text));
}

async function tap(id, button, msg) {
  if (busy) return;
  button.disabled = true;
  if (id.startsWith("wf:")) pendingTap = { button, msg };
  else $(".actions", msg).replaceChildren(h("div", { class: "applied" }, button.textContent));  // a choice: one answer
  await converse("/api/action", { id });
  pendingTap = null;
}

function send() {
  const input = $("#input");
  const text = input.value.trim();
  if (!text || busy) return;
  input.value = "";
  grow();
  if (/^\/(clear|new)$/i.test(text)) { newChat(); return; }
  daySep(new Date().toLocaleDateString("sv"));
  addMine(text);
  converse("/api/chat", { text }).then(refreshStatus);
}

const grow = () => { const t = $("#input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 160) + "px"; };
$("#input").addEventListener("input", () => { grow(); slash(); });
$("#input").addEventListener("keydown", (e) => {
  const box = $("#slash");
  if (box.classList.contains("on") && (e.key === "Tab" || e.key === "Escape")) {
    e.preventDefault();
    if (e.key === "Tab") $("button", box).click(); else box.classList.remove("on");
    return;
  }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && !matchMedia("(max-width: 760px)").matches) { e.preventDefault(); send(); }
});
$("#composer").addEventListener("submit", (e) => { e.preventDefault(); $("#slash").classList.remove("on"); send(); });

// '/' at the start of the message: the workflows as commands, the same as in Telegram ('/mail_bills condominio').
async function slash() {
  const box = $("#slash"), input = $("#input"), m = /^\/(\w*)$/.exec(input.value);
  if (!m) { box.classList.remove("on"); return; }
  if (!flows) await loadFlows().catch(() => null);
  const found = (flows || []).filter((w) => w.ready && w.name.startsWith(m[1].toLowerCase()));
  const clear = "clear".startsWith(m[1].toLowerCase());
  box.classList.toggle("on", found.length > 0 || clear);
  box.replaceChildren(...(clear ? [h("button", { type: "button", role: "option", onclick: () => {
    input.value = ""; box.classList.remove("on"); grow(); newChat(); input.focus();
  } }, h("code", {}, "/clear"), h("span", {}, "New chat"), h("small", {}, "Forget the recent messages and wipe the screen"))] : []),
  ...found.map((w) => h("button", { type: "button", role: "option", onclick: () => {
    input.value = `/${w.name} `; box.classList.remove("on"); input.focus(); grow();
  } }, h("code", {}, "/" + w.name), h("span", {}, w.title), h("small", {}, w.about))));
}

// ---- today -----------------------------------------------------------------------------------
async function showToday() {
  const page = $("#today");
  let d;
  try { d = await api("/api/today"); } catch (e) { page.replaceChildren(h("div", { class: "note" }, e.message)); return; }
  $("#today-title").textContent = fmtDay(d.now);
  page.replaceChildren(...todayBlocks(d, { full: true }));
}

async function renderGlance() {
  const side = $("#glance");
  if (getComputedStyle(side).display === "none") return;
  try { side.replaceChildren(...todayBlocks(await api("/api/today"), { full: false })); } catch {}
}

function todayBlocks(d, { full }) {
  const out = [];
  const week = full ? d.week : d.week.filter((t) => dayKey(t.when) <= dayKey(d.now) || dayKey(t.when) === nextDay(d.now));
  out.push(h("div", { class: "section" }, h("h2", {}, full ? "This week" : "Today & tomorrow"),
    h("small", {}, `${d.week.length} item${d.week.length === 1 ? "" : "s"}`)));
  const card = h("div", { class: "card rows" });
  let day = null;
  for (const t of week) {
    if (dayKey(t.when) !== day) { day = dayKey(t.when); card.append(h("div", { class: "day-label" }, relDay(day))); }
    card.append(itemRow(t, d.now));
  }
  if (!week.length) card.append(h("div", { class: "empty" }, "Nothing scheduled."));
  out.push(card);

  const groups = [["high", "High priority"], ["normal", "Open tasks"], ["low", "Low priority"]];
  out.push(h("div", { class: "section" }, h("h2", {}, "Open tasks"), h("small", {}, `${d.open.length} undated or past their reminder`)));
  const tasks = h("div", { class: "card rows" });
  for (const [pri, label] of groups) {
    const group = d.open.filter((t) => t.priority === pri);
    if (!group.length) continue;
    if (full && d.open.some((t) => t.priority !== "normal")) tasks.append(h("div", { class: "day-label" }, label));
    group.forEach((t) => tasks.append(itemRow(t, d.now)));
  }
  if (!d.open.length) tasks.append(h("div", { class: "empty" }, "Nothing else open. 🎉"));
  out.push(tasks);

  if (d.done.length) {
    out.push(h("div", { class: "section" }, h("h2", {}, "Done today"), h("small", {}, String(d.done.length))));
    out.push(h("div", { class: "card done-list" }, d.done.map((t) => h("s", {}, t))));
  }
  if (full && d.calendar_synced && asDate(d.calendar_synced) < asDate(d.now) - 3600e3) {
    out.push(h("div", { class: "note" }, `Google Calendar last synced ${d.calendar_synced.replace("T", " ")}.`));
  }
  return out;
}
const nextDay = (iso) => { const d = asDate(dayKey(iso)); d.setDate(d.getDate() + 1); return d.toLocaleDateString("sv"); };

function itemRow(t) {
  const sub = [];
  if (t.overdue) sub.push(h("span", { class: "chip danger" }, "overdue"));
  if (t.priority === "high" && t.kind === "task") sub.push(h("span", { class: "chip warn" }, "high"));
  if (t.kind === "task" && t.due) sub.push(h("span", {}, `${t.soft ? "target" : "due"} ${fmtDay(t.due, { weekday: "short", day: "numeric", month: "short" })}${fmtTime(t.due) ? " " + fmtTime(t.due) : ""}`));
  if (t.location) sub.push(h("span", {}, "📍 " + t.location));
  if (t.recur) sub.push(h("span", {}, "↻ " + (t.recur === "yes" ? "repeats" : t.recur)));
  if (t.google) sub.push(h("span", {}, "Google"));
  // A task due on a bare day has no time (its `when` is 23:59 of that day); one with only a reminder shows that.
  const when = t.all_day ? "all day" : t.kind === "event" ? fmtTime(t.when) + (t.end ? "–" + t.end.slice(11, 16) : "")
    : fmtTime(t.due || t.when);
  const row = h("div", { class: "row" });
  const lead = t.kind === "task"
    ? h("button", { class: "check", type: "button", title: "Mark done", "aria-label": `Done: ${t.title}`, onclick: () => complete(t, row) })
    : h("span", { class: "event-dot" }, h("i", { class: t.google ? "google" : "" }));
  row.append(lead, h("div", { class: "what" }, h("div", { class: "title" }, t.title), sub.length ? h("div", { class: "sub" }, sub) : ""),
    h("div", { class: "when" }, when));
  return row;
}

async function complete(t, row) {
  $(".check", row).classList.add("done");
  row.classList.add("leaving");
  try {
    const r = await api(`/api/tasks/${encodeURIComponent(t.ref)}/done`, { method: "POST" });
    toast(r.result, { label: "Undo", run: undo });
  } catch (e) { toast(e.message); }
  setTimeout(() => ($("#view-today").classList.contains("on") ? showToday() : renderGlance()), 700);
}

async function undo() {
  try { toast((await api("/api/undo", { method: "POST" })).result); } catch (e) { toast(e.message); }
  $("#view-today").classList.contains("on") ? showToday() : renderGlance();
}
$("#undo").addEventListener("click", undo);

// ---- money -----------------------------------------------------------------------------------
let month = null;
async function showMoney() {
  const page = $("#money");
  let d;
  try { d = await api("/api/money" + (month ? `?month=${month}` : "")); }
  catch (e) { page.replaceChildren(h("div", { class: "note" }, e.message)); return; }
  month = d.month;
  $("#month").textContent = asDate(month + "-01").toLocaleDateString("en-GB", { month: "long", year: "numeric" });
  const report = (title, text) => [h("div", { class: "section" }, h("h2", {}, title)), h("div", { class: "card report" }, text)];
  page.replaceChildren(
    d.freshness ? h("div", { class: "note" }, d.freshness) : "",
    d.balances.length ? h("div", { class: "tiles" }, d.balances.map((b) => h("div", { class: "card tile" },
      h("div", { class: "label" }, b.account.replace(/_/g, " ")), h("div", { class: "value" }, b.eur),
      h("div", { class: "sub" }, "on " + fmtDay(b.on, { day: "numeric", month: "short", year: "numeric" }))))) : "",
    h("div", { class: "grid2" },
      h("div", {}, ...report("This month", d.summary)),
      h("div", {}, ...report("Budgets", d.budgets))),
    h("div", { class: "grid2" },
      h("div", {}, ...report("Monthly charges", d.subs)),
      h("div", {}, ...report("Bills", d.bills))),
  );
}
function shiftMonth(n) {
  const [y, m] = month.split("-").map(Number);
  const d = new Date(y, m - 1 + n, 1);
  month = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  showMoney();
}
$("#prev").addEventListener("click", () => shiftMonth(-1));
$("#next").addEventListener("click", () => shiftMonth(1));

// ---- workflows -------------------------------------------------------------------------------
// One card per workflow: what it does, its settings as a form (drawn from its Params), Run, the live status and the
// result with its buttons, and the last few runs (from any chat). Cards are built once so a run survives a refresh.
let flows = null;  // GET /api/workflows; also the chat's /command suggestions
const loadFlows = async () => (flows = await api("/api/workflows"));
const flowCards = new Map();

async function showFlows() {
  const page = $("#flows");
  try { await loadFlows(); } catch (e) { page.replaceChildren(h("div", { class: "note" }, e.message)); return; }
  if (!flowCards.size) {
    page.replaceChildren(...flows.map(flowCard),
      h("div", { class: "empty" }, "Also from the chat: ", h("code", {}, "/name"), " and what you want in words, e.g. ",
        h("code", {}, "/mail_bills condominio"), ", or just ask."));
  }
  for (const wf of flows) flowCards.get(wf.name)?.showRuns(wf);
}

function flowCard(wf) {
  const out = h("div", { class: "wf-out" }), runs = h("div", { class: "wf-runs" });
  const form = flowForm(wf, h, { disabled: !wf.ready, onRun: (params) => runFlow(wf, params, form, out) });
  const card = h("article", { class: `card wf${wf.ready ? "" : " off"}`, id: `flow-${wf.name}` },
    h("header", {}, h("h2", {}, wf.title), h("code", {}, "/" + wf.name),
      wf.ready ? "" : h("span", { class: "chip warn" }, "needs " + wf.missing.join(", "))),
    h("p", { class: "about" }, wf.about),
    form, out, runs);
  card.showRuns = (w) => runs.replaceChildren(w.runs.length
    ? h("details", {}, h("summary", {}, `Recent runs · latest ${when(w.runs[0].at)}`), w.runs.map((r) => runRow(w, r)))
    : h("div", { class: "never" }, "Not run yet."));
  card.showRuns(wf);
  flowCards.set(wf.name, card);
  return card;
}

const when = (at) => `${relDay(at)} ${fmtTime(at)}`.replace(/^Today /, "today ");
function runRow(wf, r) {
  const said = r.text.startsWith("/") ? r.text : `/${wf.name} ${r.text}`.trim();
  return h("div", { class: "run-row" },
    h("div", { class: "run-head" }, h("span", {}, when(r.at)), h("code", {}, said),
      r.ms ? h("span", {}, `${Math.max(1, Math.round(r.ms / 1000))} s`) : "",
      h("span", { class: `chip ${r.stopped ? "warn" : "ok"}`, title: r.stopped || "" }, r.stopped ? "failed" : "done")),
    r.reply ? h("div", { class: "run-reply", html: rich(r.reply.length > 900 ? r.reply.slice(0, 900) + "…" : r.reply) }) : "");
}

async function runFlow(wf, params, form, out) {
  if (busy) { toast("Franziska is still on the last message; run it when that's done."); return; }
  busy = true;
  form.running(true);
  $("#send").disabled = true;
  const line = addStatus("Starting…");  // appended to the chat log; moved into the card
  out.replaceChildren(line);
  let reply = null;
  try {
    await stream(`/api/workflows/${wf.name}`, { params }, (ev) => {
      if (ev.type === "status") $("span:last-child", line).textContent = ev.text;
      else if (ev.type === "reply") reply = ev;
    });
  } catch (e) { reply = { text: `I couldn't reach the server (${e.message}).`, stopped: "error" }; }
  reply ||= { text: "The run ended without a reply.", stopped: "error" };
  busy = false;
  form.running(false);
  $("#send").disabled = false;
  out.replaceChildren(herMsg(reply));
  if (loaded) {  // the run is a turn of the chat too; its buttons stay on the card
    daySep(new Date().toLocaleDateString("sv"));
    addMine(runLabel(wf, params));
    addHers({ ...reply, actions: [] });
  }
  if (!showing("flows")) toast(`${wf.title} ${reply.stopped ? "stopped" : "is done"}.`, { label: "Show", run: () => { location.hash = "flows"; } });
  showFlows();
}

// ---- code: coding jobs (franziska/code.py) -----------------------------------------------------
// A form to start a job, the latest jobs, and one job's review: its report, tests, diff and the taps (merge, change,
// discard; stop while it runs). The list refreshes itself while a job is queued or running.
let codeTimer = null;
const JOB_CHIP = { queued: "", running: "flow", ready: "ok", empty: "", failed: "danger", stopped: "warn",
  merged: "ok", discarded: "", replaced: "" };

async function showCode(open) {
  const page = $("#code");
  let d;
  try { d = await api("/api/code"); } catch (e) { page.replaceChildren(h("div", { class: "note" }, e.message)); return; }
  const repo = h("select", {}, d.repos.map((r) => h("option", { value: r }, r === "scratch" ? "scratch (new, empty)" : r)));
  const request = h("textarea", { rows: 3, placeholder: "What should it do? e.g. 'add a /uptime command that says how long the bot has been running; test it'" });
  const go = h("button", { class: "run", type: "submit" }, "Start job");
  const form = h("form", { class: "wf-form code-form", onsubmit: async (ev) => {
    ev.preventDefault();
    go.disabled = true;
    try {
      const job = await api("/api/code", { method: "POST", body: { repo: repo.value, request: request.value } });
      toast(`Job #${job.id} started.`);
      location.hash = `code/${job.id}`;
    } catch (e) { toast(e.message); }
    go.disabled = false;
  } }, h("div", { class: "field" }, h("label", {}, "Repository"), repo),
    h("div", { class: "field wide" }, h("label", {}, "Request"), request), go,
    h("small", { class: "hint" }, `Runs on the local model, at most ${d.minutes} min; the bot also tells you on Telegram when it's done.`));
  const list = h("div", { class: "jobs" }, d.jobs.length ? d.jobs.map((j) =>
    h("a", { class: `job-row${String(j.id) === open ? " on" : ""}`, href: `#code/${j.id}` },
      h("span", { class: `chip ${JOB_CHIP[j.status] || ""}` }, j.status), h("b", {}, `#${j.id}`),
      h("span", { class: "repo" }, j.repo), h("span", { class: "title" }, j.title),
      h("span", { class: "when" }, j.created ? when(j.created) : ""))) : h("div", { class: "empty" }, "No jobs yet."));
  const detail = h("div", { class: "job-detail" });
  page.replaceChildren(h("article", { class: "card wf" }, h("header", {}, h("h2", {}, "New job")), form),
    h("article", { class: "card wf" }, h("header", {}, h("h2", {}, "Jobs")), list), detail);
  clearTimeout(codeTimer);
  if (d.jobs.some((j) => j.running)) codeTimer = setTimeout(() => showing("code") && showCode(open), 5000);
  if (open) await showJob(detail, open);
}

async function showJob(box, id) {
  let j;
  try { j = await api(`/api/code/${id}`); } catch (e) { box.replaceChildren(h("div", { class: "note" }, e.message)); return; }
  const act = async (action, confirmText) => {
    if (confirmText && !window.confirm(confirmText)) return;
    try {
      const r = await api(`/api/code/${j.id}/${action}`, { method: "POST" });
      toast(r.text);
      if (action === "merge" && j.self && /merged/.test(r.text)) {
        box.prepend(h("div", { class: "note" }, "Merged into Franziska itself: the running bot and web UI use the old code until restarted. ",
          h("button", { class: "ghost", type: "button", onclick: () => act("restart") }, "Restart bot + web")));
        return;
      }
    } catch (e) { toast(e.message); }
    showCode(String(j.id));
  };
  const change = h("textarea", { rows: 2, placeholder: "What should change? A follow-up job continues on this branch." });
  const buttons = [];
  if (j.review && !j.scratch) buttons.push(h("button", { class: "run", type: "button", onclick: () => act("merge", `Merge job #${j.id} into ${j.base_branch}?`) }, "✅ Merge"));
  if (j.review) buttons.push(h("button", { class: "ghost", type: "button", onclick: () => act("discard", `Discard job #${j.id} and its branch?`) }, "🗑 Discard"));
  if (j.running) buttons.push(h("button", { class: "ghost", type: "button", onclick: () => act("stop") }, "⏹ Stop"));
  const changeForm = j.review ? h("form", { class: "wf-form", onsubmit: async (ev) => {
    ev.preventDefault();
    try {
      const job = await api("/api/code", { method: "POST", body: { request: change.value, parent: j.id } });
      toast(`Job #${job.id} started.`);
      location.hash = `code/${job.id}`;
    } catch (e) { toast(e.message); }
  } }, h("div", { class: "field wide" }, h("label", {}, "💬 Change"), change), h("button", { class: "run", type: "submit" }, "Start follow-up")) : "";
  const diffLines = (j.diff || "").split("\n").map((line) => h("div", { class:
    line.startsWith("+++") || line.startsWith("---") ? "d-file" : line.startsWith("+") ? "d-add" : line.startsWith("-") ? "d-del"
      : line.startsWith("@@") ? "d-hunk" : line.startsWith("diff ") ? "d-head" : "" }, line || " "));
  box.replaceChildren(h("article", { class: "card wf" },
    h("header", {}, h("h2", {}, `Job #${j.id} · ${j.repo}`), h("span", { class: `chip ${JOB_CHIP[j.status] || ""}` }, j.status),
      h("code", {}, j.branch), h("span", { class: "bar-note" }, `${j.steps} steps${j.parent ? ` · follows #${j.parent}` : ""}`)),
    h("p", { class: "about" }, j.request),
    j.summary ? h("div", { class: "run-reply", html: rich(j.summary) }) : "",
    j.error ? h("div", { class: "note" }, j.error) : "",
    j.tests ? h("details", {}, h("summary", {}, `Tests: ${j.tests.trim().split("\n").pop()}`), h("pre", { class: "out" }, j.tests)) : "",
    j.files.length ? h("div", { class: "files" }, "Files: ", j.files.map((f) => h("a", { href: `/api/code/${j.id}/files/${encodeURIComponent(f)}`, download: f }, f))) : "",
    buttons.length ? h("div", { class: "job-actions" }, buttons) : "", changeForm,
    j.stat ? h("pre", { class: "out" }, j.stat) : "",
    j.diff ? h("div", { class: "diff" }, diffLines) : ""));
}

// ---- data: every store's tables, read-only -----------------------------------------------------
// The stores on the left (tables and views, with row counts, filterable, each store folds); the open one as a grid:
// sort by a column, search all of them, 50 rows a page, ↑↓ between rows, a row opens in full (‹ › to the next).
// With nothing open: the stores, the tools (Datasette, Phoenix), the context files, the report PDFs, the backups.
// '@context' in place of a store: the files the agents work from (/api/context).
const kb = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`;
const fmtN = (n) => n.toLocaleString("en-GB");
const CONTEXT = "@context";
let data = null, grid = { store: null, table: null, q: "", sort: "", desc: true, offset: 0 }, contextFiles = null;
const folded = new Set((store.get("folded") || "").split(",").filter(Boolean));  // stores folded in the list
const sqlHref = (st, table) => `/sql/${encodeURIComponent(st)}${table ? "/" + encodeURIComponent(table) : ""}`;
const loadContext = async () => (contextFiles = await api("/api/context"));

async function showData(storeName, table) {
  try { data = await api("/api/data"); } catch (e) { $("#data-main").replaceChildren(h("div", { class: "note" }, e.message)); return; }
  if (storeName !== grid.store || table !== grid.table) grid = { store: storeName, table, q: "", sort: "", desc: true, offset: 0 };
  const files = storeName === CONTEXT;
  $("#dataq").value = grid.q;
  $("#data").classList.toggle("open", !!table || files);
  $("#data-tools").classList.toggle("on", !!table && !files);
  if (files) await loadContext().catch(() => (contextFiles = []));
  renderDataNav();
  if (files) showFile(table); else if (table) loadRows(); else dataHome();
}

function renderDataNav() {
  const q = $("#tableq").value.trim().toLowerCase();
  const hit = (...words) => !q || words.some((w) => w.toLowerCase().includes(q));
  const stores = data.stores.map((st) => {
    const tables = st.tables.filter((t) => hit(t.name, st.name));
    if (!tables.length) return "";
    const box = h("details", { class: "store", open: q || !folded.has(st.name) ? true : null,
      ontoggle: () => { if (q) return; box.open ? folded.delete(st.name) : folded.add(st.name); store.set("folded", [...folded].join(",")); } },
      h("summary", { class: "store-name" }, h("span", {}, st.name), h("small", {}, kb(st.bytes)),
        h("a", { class: "sql", href: sqlHref(st.name), target: "_blank", rel: "noopener", title: `Query ${st.name} with SQL (Datasette)`, onclick: (e) => e.stopPropagation() }, "SQL ↗")),
      tables.map((t) => h("a", {
        href: `#data/${st.name}/${encodeURIComponent(t.name)}`,
        class: `${grid.store === st.name && grid.table === t.name ? "on" : ""}${t.rows ? "" : " none"}`,
        title: t.kind === "view" ? "A view: computed from the tables" : t.columns.join(", "),
      }, h("span", {}, t.name, t.kind === "view" ? h("i", {}, " view") : ""), h("small", {}, fmtN(t.rows)))));
    return box;
  });
  const files = (contextFiles || []).filter((f) => hit(f.name, f.group, "context files"));
  $("#data-nav").replaceChildren(
    h("a", { href: "#data", class: `home${grid.table || grid.store === CONTEXT ? "" : " on"}` }, "Overview"),
    h("a", { href: `#data/${CONTEXT}`, class: `home${grid.store === CONTEXT ? " on" : ""}`, title: "What the agents learned, what each one is shown, their instructions" },
      "Context files", contextFiles ? h("small", {}, String(contextFiles.length)) : ""),
    ...(grid.store === CONTEXT || q ? groupBy(files, "group").map(([group, fs]) => h("div", { class: "store files" },
      h("div", { class: "store-name" }, h("span", {}, group)),
      fs.map((f) => h("a", { href: `#data/${CONTEXT}/${encodeURIComponent(f.id)}`, class: grid.store === CONTEXT && grid.table === f.id ? "on" : "" },
        h("span", {}, f.name))))) : []),
    ...stores,
    q && !stores.some(Boolean) && !files.length ? h("div", { class: "empty" }, `No table matches “${q}”.`) : "");
  $("#data-nav a.on:not(.home)")?.scrollIntoView({ block: "nearest" });  // a deep link to a table far down the list
}
const groupBy = (xs, key) => [...xs.reduce((m, x) => m.set(x[key], [...(m.get(x[key]) || []), x]), new Map())];
$("#tableq").addEventListener("input", () => data && renderDataNav());
$("#tableq").addEventListener("keydown", (e) => {  // Enter opens the first match
  if (e.key !== "Enter") return;
  const first = $("#data-nav details a:not(.sql), #data-nav .files a");
  if (first) location.hash = first.getAttribute("href");
});

function dataHome() {
  $("#data-title").textContent = "Data";
  const rows = (n) => n === 1 ? "1 row" : `${fmtN(n)} rows`;
  const tele = h("div", { class: "card tile tool-tile" });
  $("#data-main").replaceChildren(h("div", { class: "page-in" },
    h("p", { class: "lead" }, "Everything Franziska keeps, read-only. ", h("code", {}, data.data_dir)),
    h("div", { class: "tiles" }, data.stores.map((st) => h("a", { class: "card tile store-tile", href: `#data/${st.name}/${encodeURIComponent(st.tables[0]?.name || "")}` },
      h("div", { class: "label" }, st.name), h("div", { class: "value" }, kb(st.bytes)),
      h("div", { class: "sub" }, `${st.tables.length} tables · ${rows(st.tables.reduce((a, t) => a + (t.kind === "table" ? t.rows : 0), 0))}`),
      h("div", { class: "sub" }, "changed " + when(st.modified))))),
    h("div", { class: "section" }, h("h2", {}, "Tools"), h("small", {}, "open source, behind the same sign-in")),
    h("div", { class: "tiles tools" },
      h("a", { class: "card tile tool-tile", href: "/sql/", target: "_blank", rel: "noopener" },
        h("div", { class: "label" }, "SQL · Datasette"), h("div", { class: "value" }, "Query the stores ↗"),
        h("div", { class: "sub" }, "Any SELECT, facets, filters, joins by foreign key, CSV and JSON out.")),
      tele,
      h("a", { class: "card tile tool-tile", href: `#data/${CONTEXT}` },
        h("div", { class: "label" }, "Context files"), h("div", { class: "value" }, "What the agents read →"),
        h("div", { class: "sub" }, "Learned memories as files, the index each agent is shown, its instructions."))),
    h("div", { class: "section" }, h("h2", {}, "Reports"), h("small", {}, String(data.reports.length))),
    h("div", { class: "card rows" }, data.reports.length ? data.reports.map((r) => h("div", { class: "row" },
      h("div", { class: "what" }, h("a", { class: "title", href: `/api/reports/${encodeURIComponent(r.name)}`, target: "_blank", rel: "noopener" }, "📄 " + r.name)),
      h("div", { class: "when wide" }, `${when(r.modified)} · ${kb(r.bytes)}`))) : h("div", { class: "empty" }, "No market reports yet: run 📈 Markets.")),
    h("div", { class: "section" }, h("h2", {}, "Backups"), h("small", {}, data.backups.dir)),
    h("div", { class: "card rows" }, data.backups.nights.length ? data.backups.nights.map((b) => h("div", { class: "row" },
      h("div", { class: "what" }, h("div", { class: "title" }, fmtDay(b.name, { weekday: "short", day: "numeric", month: "short", year: "numeric" }))),
      h("div", { class: "when wide" }, kb(b.bytes)))) : h("div", { class: "empty" }, "No nightly backup yet (they run at 04:00 with the bot).")),
    h("div", { class: "section" }, h("h2", {}, "Memory"), h("small", {}, data.memory === 1 ? "1 entry" : `${data.memory} entries`)),
    h("div", { class: "card rows" }, h("div", { class: "row" }, h("div", { class: "what" },
      h("a", { class: "title", href: "#memory" }, "What the assistants learned →"))))));
  teleTile(tele);
}

// ---- setup: each piece, whether it works, what it adds, how to turn it on (web/onboarding.py) ----------------
const STEP_SAY = { done: "Done", todo: "Needed", off: "Optional", problem: "Not working" };
let setupOpen = new Set();  // the optional steps the user opened, kept across refreshes

// No configuration yet, or the language model isn't answering: open on Setup instead of an empty chat.
async function firstRun() {
  // The demo: Setup and Settings would show and change this machine's own configuration, not the demo's.
  if (status?.demo) { for (const v of ["setup", "settings"]) $(`.nav a[data-view="${v}"]`).hidden = true; return; }
  try {
    const d = await api("/api/setup");
    $("#nav-setup").classList.toggle("unread", !d.ready);
    if (!d.ready && !location.hash) location.hash = "#setup";
  } catch {}
}

async function showSetup() {
  const page = $("#setup");
  let d;
  try { d = await api("/api/setup"); } catch (e) { page.replaceChildren(h("div", { class: "note" }, e.message)); return; }
  $("#nav-setup").classList.toggle("unread", !d.ready);
  renderSetup(page, d);
}

function setupInput(f, note) {
  if (f.kind === "bool") return h("input", { type: "checkbox", "aria-label": f.label, checked: f.value === "1", onchange: (e) => note(e.target.checked ? "1" : "0") });
  if (f.kind === "select") return h("select", { "aria-label": f.label, onchange: (e) => note(e.target.value) },
    f.choices.map((c) => h("option", { value: c, selected: c === f.value }, c || "(off)")));
  const attrs = { type: f.kind === "secret" ? "password" : f.kind === "number" ? "number" : "text", spellcheck: "false", autocomplete: "off", "aria-label": f.label,
    value: f.kind === "secret" ? "" : f.value ?? "", oninput: (e) => note(e.target.value),
    placeholder: f.kind === "secret" ? (f.set ? "Saved · type to replace" : "123456789:AA…") : f.kind === "time" ? "HH:MM" : f.default || "" };
  if (f.kind === "tz") {
    const input = h("input", { ...attrs, list: "dl-tz" });
    const zones = Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : [];
    return h("div", {}, input, h("datalist", { id: "dl-tz" }, zones.map((z) => h("option", { value: z }))));
  }
  return h("input", attrs);
}

function renderSetup(page, d) {
  const restartRow = (ids) => h("div", { class: "tile-actions" }, h("small", { class: "sub" }, "Saved. Apply it:"),
    ids.map((sid) => h("button", { class: "ghost", type: "button", onclick: async (e) => {
      const b = e.currentTarget; b.disabled = true; b.textContent = "Restarting…";
      try { await api(`/api/services/${sid}/restart`, { method: "POST" }); } catch (err) { toast(err.message); b.disabled = false; return; }
      if (sid === "web") return waitForWeb();
      setTimeout(showSetup, 3000);
    } }, `Restart the ${sid === "bot" ? "bot" : "web app"}`)));

  const card = (st) => {
    const dirty = {};
    const out = h("div", { class: "step-out" });
    const save = h("button", { class: "run", type: "button", disabled: true, onclick: async () => {
      save.disabled = true;
      try {
        const r = await api("/api/setup", { method: "PUT", body: { env: dirty } });
        if (r.started?.length) toast("Your configuration is started: " + d.config);
        else toast(r.changed.length ? "Saved." : "Nothing changed.");
        await showSetup();
        const again = page.querySelector(`[data-step="${st.id}"] .step-out`);
        if (again && r.changed.length && st.restart.length && d.configured) again.replaceChildren(restartRow(st.restart));
      } catch (e) { toast(e.message); save.disabled = false; }
    } }, d.configured ? "Save" : "Save and start");
    const fields = st.fields.map((f) => {
      const input = setupInput(f, (v) => { dirty[f.key] = v; save.disabled = false; });
      return h("div", { class: "field" + (f.kind === "bool" ? " inline" : "") }, h("label", {}, f.label), input, f.help ? h("small", {}, f.help) : "");
    });
    const act = (a) => h("button", { class: "ghost", type: "button", onclick: async (e) => {
      const b = e.currentTarget, label = b.textContent; b.disabled = true; b.textContent = "Working…";
      try {
        const r = await api(`/api/setup/${a.id}`, { method: "POST" });
        if (r.link) {  // a sign-in in another tab; it comes back by itself, or the user pastes where it ended
          const address = h("input", { type: "text", spellcheck: "false", autocomplete: "off", placeholder: "http://127.0.0.1:…/api/setup/google/callback?…", "aria-label": "The address the browser ended on" });
          const finish = h("button", { class: "run", type: "button", onclick: async () => {
            finish.disabled = true;
            try {
              const f = await api("/api/setup/google/finish", { method: "POST", body: { address: address.value } });
              out.replaceChildren(h("div", { class: "sub" }, f.say), f.restart?.length ? restartRow(f.restart) : "");
            } catch (err) { toast(err.message); finish.disabled = false; }
          } }, "Finish");
          out.replaceChildren(h("div", { class: "sub" }, r.say, " ", h("a", { href: r.link, target: "_blank", rel: "noopener" }, "Open Google's sign-in ↗")),
            r.paste ? h("div", { class: "wf-form" }, h("div", { class: "field" }, h("label", {}, "The address the browser ended on"), address), h("div", { class: "field inline" }, finish)) : "");
        } else if (r.people) {
          out.replaceChildren(h("div", { class: "sub" }, "Who is you?"), h("div", { class: "tile-actions" }, r.people.map((p) =>
            h("button", { class: "ghost", type: "button", onclick: async () => {
              const ids = new Set((st.fields.find((f) => f.key === "FRANZISKA_ALLOWED_IDS")?.value || "").split(",").filter(Boolean));
              ids.add(String(p.id));
              try { await api("/api/setup", { method: "PUT", body: { env: { FRANZISKA_ALLOWED_IDS: [...ids].join(",") } } }); toast(`Allowed ${p.name}.`); }
              catch (err) { toast(err.message); return; }
              await showSetup();
            } }, `${p.name} · ${p.id}`))));
        } else {
          out.replaceChildren(h("div", { class: "sub" }, r.say || "Done.", r.link ? [" ", h("a", { href: r.link, target: "_blank", rel: "noopener" }, "Open the chat ↗")] : ""));
          if (a.id !== "telegram_check") await showSetup();
        }
      } catch (err) { out.replaceChildren(h("div", { class: "note" }, err.message)); }
      b.disabled = false; b.textContent = label;
    } }, a.label);
    const copy = (c) => h("div", { class: "cmd" }, h("code", {}, c), h("button", { class: "ghost", type: "button", onclick: async () => {
      try { await navigator.clipboard.writeText(c); toast("Copied."); } catch { toast("The browser didn't allow copying here."); }
    } }, "Copy"));
    const upload = st.upload ? h("label", { class: "ghost button" }, st.upload.label, h("input", { type: "file", accept: st.upload.accept, hidden: true, onchange: async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const form = new FormData();
      form.append("file", file);
      try {
        const res = await fetch(`/api/setup/google/client`, { method: "POST", body: form });
        const r = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(r.detail || res.statusText);
        toast(r.say);
        await showSetup();
      } catch (err) { out.replaceChildren(h("div", { class: "note" }, err.message)); }
    } })) : "";
    const open = st.required || st.state === "todo" || st.state === "problem" || setupOpen.has(st.id);
    const body = [
      h("p", { class: "step-adds" }, st.adds),
      st.detail ? h("div", { class: st.state === "problem" || st.state === "todo" ? "note" : "sub" }, st.detail) : "",
      st.how.length ? h("ol", { class: "step-how" }, st.how.map((x) => h("li", {}, x))) : "",
      st.commands.length ? h("div", { class: "step-cmds" }, h("small", { class: "sub" }, "In a terminal:"), st.commands.map(copy)) : "",
      fields.length ? h("div", { class: "wf-form" }, fields, h("div", { class: "field inline" }, save)) : "",
      upload || st.actions.length ? h("div", { class: "tile-actions" }, upload, st.actions.map(act)) : "",
      out];
    return h("details", { class: `card step s-${st.state}`, "data-step": st.id, open: open || null,
      ontoggle: (e) => { e.currentTarget.open ? setupOpen.add(st.id) : setupOpen.delete(st.id); } },
      h("summary", {}, h("span", { class: `pill ${st.state}` }, STEP_SAY[st.state] || st.state), h("span", { class: "step-title" }, st.title)),
      h("div", { class: "step-body" }, body));
  };

  const required = d.steps.filter((s) => s.required), optional = d.steps.filter((s) => !s.required);
  page.replaceChildren(
    h("div", { class: "card works" }, h("div", { class: "label" }, d.ready ? "Works now" : "Not working yet"),
      d.works.length ? h("ul", {}, d.works.map((w) => h("li", {}, w))) : h("div", { class: "sub" }, "The two steps below come first: the language model and a few things about you.")),
    h("div", { class: "section" }, h("h2", {}, "First"), h("small", {}, "needed")),
    ...required.map(card),
    h("div", { class: "section" }, h("h2", {}, "When you want them"), h("small", {}, "optional, in any order")),
    ...optional.map(card));
}

// ---- settings: what runs, the choices behind it, and the service buttons ------------------------------------
const STATE_SAY = { active: "Running", inactive: "Stopped", activating: "Starting…", deactivating: "Stopping…", failed: "Failed", missing: "Not installed" };
let settingsPoll;

async function showSettings() {
  const page = $("#settings");
  let d;
  try { d = await api("/api/settings"); } catch (e) { page.replaceChildren(h("div", { class: "note" }, e.message)); return; }
  renderSettings(page, d);
  clearTimeout(settingsPoll);  // while the page is open, service lights follow reality (a restart takes a while)
  settingsPoll = setTimeout(() => { if (showing("settings") && !page.querySelector(":focus-within")) showSettings(); }, 8000);
}

function renderSettings(page, d) {
  const r = d.running, dirty = { env: {}, units: {} };
  const save = h("button", { class: "run", type: "button", disabled: true, onclick: async () => {
    save.disabled = true;
    try { const out = await api("/api/settings", { method: "PUT", body: dirty }); toast(out.changed.length || Object.keys(dirty.units).length ? "Saved. Restart the service to apply." : "Nothing changed."); }
    catch (e) { toast(e.message); save.disabled = false; return; }
    await showSettings();
  } }, "Save changes");
  const touch = () => { save.disabled = !Object.keys(dirty.env).length && !Object.keys(dirty.units).length; };
  const field = (f, label, help, input) => h("div", { class: "field" + (input.type === "checkbox" ? " inline" : "") },
    h("label", {}, label), input, help ? h("small", {}, help) : "");
  const envInput = (f) => {
    const note = (v) => { dirty.env[f.key] = v; touch(); };
    if (f.kind === "bool") return h("input", { type: "checkbox", checked: f.value === "1" || f.value === "true", onchange: (e) => note(e.target.checked ? "1" : "0") });
    if (f.kind === "select") return h("select", { onchange: (e) => note(e.target.value) },
      f.choices.map((c) => h("option", { value: c, selected: c === f.value }, c || "(default)")));
    if (f.kind === "multi") {
      const on = new Set(f.value.split(",").filter(Boolean));
      return h("div", { class: "seg multi" }, f.choices.map((c) => h("button", { type: "button", "aria-checked": String(on.has(c)),
        onclick: (e) => { on.has(c) ? on.delete(c) : on.add(c); e.currentTarget.setAttribute("aria-checked", String(on.has(c))); note([...on].join(",")); } }, c)));
    }
    const input = h("input", { type: f.kind === "number" ? "number" : "text", step: "0.05", value: f.value, placeholder: f.default || "", spellcheck: "false",
      oninput: (e) => note(e.target.value) });
    if (f.datalist === "ollama" && d.ollama.length) {
      const id = "dl-" + f.key; input.setAttribute("list", id);
      return h("div", {}, input, h("datalist", { id }, d.ollama.map((m) => h("option", { value: m }))));
    }
    return input;
  };
  const unitCard = (sid, u) => {
    const svc = d.services.find((s) => s.id === sid);
    return h("div", { class: "card tile settings-card" },
      h("div", { class: "label" }, u.title),
      h("div", { class: "wf-form" }, u.options.map((o) => {
        const note = (v) => { (dirty.units[sid] ||= {})[o.flag] = v; touch(); };
        const input = o.kind === "ctx"
          ? h("input", { type: "number", min: 2048, step: 1024, value: o.value, oninput: (e) => note(e.target.value) })
          : h("select", { onchange: (e) => note(e.target.value) }, o.choices.map((c) =>
            h("option", { value: c, selected: c === o.value }, c || "(none: text only)")));
        return field(null, o.label, o.kind === "mmproj" ? "Lets the model read images; costs a little speed and VRAM."
          : sid === "embed" ? "Switching re-embeds every document." : "", input);
      })),
      svc?.behind?.length ? h("div", { class: "sub behind" }, "Restart to apply: " + svc.behind.join(", ")) : "");
  };
  const restartBtn = (s, label = "Restart") => h("button", { class: "ghost", type: "button", onclick: async (e) => {
    const b = e.currentTarget; b.disabled = true; b.textContent = "Restarting…";
    try { await api(`/api/services/${s.id}/restart`, { method: "POST" }); } catch (err) { toast(err.message); }
    if (s.id === "web") return waitForWeb();
    setTimeout(showSettings, 2500);
  } }, label);
  const llm = d.services.find((s) => s.id === "llm");
  const gpuBtn = llm && (llm.state === "active"
    ? h("button", { class: "ghost", type: "button", onclick: (e) => gpuToggle(e, "stop") }, "Offload from GPU")
    : llm.state === "deactivating" ? h("button", { class: "ghost", type: "button", disabled: true }, "Offloading…")
    : ["inactive", "failed"].includes(llm.state) ? h("button", { class: "ghost", type: "button", onclick: (e) => gpuToggle(e, "start") }, "Load onto GPU") : "");
  const serviceRow = (s) => h("div", { class: "row" },
    h("div", { class: "what" }, h("div", { class: "title" }, h("span", { class: `tdot ${s.state === "active" ? "up" : s.state === "activating" ? "starting" : s.state === "failed" ? "failed" : "off"}` }), " " + s.title),
      h("div", { class: "sub" }, `${STATE_SAY[s.state] || s.state} · ${s.unit}` + (s.behind?.length ? ` · behind the saved settings: ${s.behind.join(", ")}` : ""))),
    h("div", { class: "tile-actions" }, s.id === "llm" ? gpuBtn : "", s.state === "missing" ? "" : restartBtn(s)));
  const groups = [...new Set(d.fields.map((f) => f.group))];
  const now = (label, value) => h("div", { class: "tile card" }, h("div", { class: "label" }, label), h("div", { class: "value small" }, value || "—"));
  page.replaceChildren(
    h("div", { class: "section" }, h("h2", {}, "Running now")),
    h("div", { class: "tiles tools" },
      now("Language model", `${r.backend} · ${r.file || r.model}`),
      now("Context window", r.window ? kilo(r.window) + " tokens" : "—"),
      now("Vision (VLM)", r.backend === "llama.cpp" ? (r.vision ? "On" : "Off") : "per model"),
      now("Embeddings", r.embedding || (r.url ? "off" : "")),
      now("Cloud model", r.cloud ? `${r.cloud}${r.cloud_routes.length ? " · " + r.cloud_routes.join(", ") : ""}` : "None (all local)")),
    h("div", { class: "section" }, h("h2", {}, "Services"), h("small", {}, "restart applies saved changes")),
    h("div", { class: "card rows" }, d.services.map(serviceRow)),
    h("div", { class: "section" }, h("h2", {}, "Model files"), h("small", {}, "llama.cpp units")),
    h("div", { class: "tiles tools" }, Object.entries(d.units).map(([sid, u]) => unitCard(sid, u))),
    ...groups.flatMap((g) => [h("div", { class: "section" }, h("h2", {}, g)),
      h("div", { class: "card tile settings-card" }, h("div", { class: "wf-form" },
        d.fields.filter((f) => f.group === g).map((f) => field(f, f.label, f.help, envInput(f)))))]),
    h("div", { class: "settings-save" }, save));
}

async function gpuToggle(ev, action) {
  const b = ev.currentTarget; b.disabled = true; b.textContent = action === "stop" ? "Offloading…" : "Loading…";
  const result = await api(`/api/local-model/${action}`, { method: "POST" }).catch((e) => ({ error: e.message }));
  if (result.error) toast(result.error);
  showSettings();
}

// The UI restarted itself: wait for the server to answer again, then reload.
async function waitForWeb() {
  toast("Restarting the web UI…");
  await new Promise((r) => setTimeout(r, 2500));
  for (let i = 0; i < 60; i++) {
    try { const res = await fetch("/api/status"); if (res.ok || res.status === 401) { location.reload(); return; } } catch {}
    await new Promise((r) => setTimeout(r, 1000));
  }
  toast("The web UI hasn't come back: journalctl --user -u franziska-web");
}

// Phoenix's tile: whether it runs, and Start / Open / Stop.
async function teleTile(el) {
  const s = await refreshTelemetry();
  const say = { up: "Running", starting: "Starting…", off: "Off", failed: "Failed to start", missing: "Not installed", unknown: "Can't tell" }[s.state] || s.state;
  const act = (label, fn) => h("button", { class: "ghost", type: "button", onclick: fn }, label);
  el.replaceChildren(
    h("div", { class: "label" }, "Telemetry · Phoenix"),
    h("div", { class: "value" }, h("span", { class: `tdot ${s.state}` }), " " + say),
    h("div", { class: "sub" }, s.state === "missing" ? "cp deploy/franziska-phoenix.service ~/.config/systemd/user/ && systemctl --user daemon-reload"
      : s.otlp ? "Each run's model requests, tool calls and guards, as traces." : "FRANZISKA_OTLP isn't set, so no runs reach it."),
    h("div", { class: "tile-actions" },
      s.state === "up" ? [h("a", { class: "ghost", href: "/phoenix/", target: "_blank", rel: "noopener" }, "Open ↗"),
        act("Stop", async () => { el.classList.add("busy"); await api("/api/telemetry/stop", { method: "POST" }).catch((e) => toast(e.message)); teleTile(el); })]
      : s.state !== "missing" ? act(s.state === "starting" ? "Starting…" : "Start", async (e) => {
        e.currentTarget.disabled = true; e.currentTarget.textContent = "Starting…";
        const r = await api("/api/telemetry/start", { method: "POST" }).catch((err) => ({ error: err.message }));
        if (r.error) toast(r.error); teleTile(el);
      }) : ""));
  el.classList.remove("busy");
}

async function loadRows() {
  const { store: st, table, q, sort, desc, offset } = grid;
  $("#data-title").textContent = table;
  const main = $("#data-main");
  const qs = new URLSearchParams({ q, sort, desc, offset });
  let d;
  try { d = await api(`/api/data/${encodeURIComponent(st)}/${encodeURIComponent(table)}?${qs}`); }
  catch (e) { main.replaceChildren(h("div", { class: "note" }, e.message)); return; }
  if (grid.table !== table) return;  // another table was opened meanwhile
  const head = h("tr", {}, d.columns.map((c, i) => h("th", { onclick: () => {
    grid = { ...grid, sort: c, desc: grid.sort === c ? !grid.desc : true, offset: 0 }; loadRows();
  }, class: grid.sort === c ? (grid.desc ? "desc" : "asc") : "" }, c, d.types[i] ? h("small", {}, d.types[i].toLowerCase()) : "")));
  const cell = (v, c) => {
    if (v == null) return h("td", { class: "null" }, "—");
    const num = typeof v === "number";
    const text = num && c.endsWith("_cents") ? eurs(v) : String(v);
    return h("td", { class: num ? "num" : "", title: text.length > 60 ? text.slice(0, 400) : null }, text.length > 80 ? text.slice(0, 80) + "…" : text);
  };
  // ↑ ↓ move between rows, Enter (or a click) opens one; the sheet then steps through the page with ‹ ›.
  const step = (tr, n) => { const to = n > 0 ? tr.nextElementSibling : tr.previousElementSibling; if (to) { to.focus(); } return to; };
  const body = d.rows.map((r, i) => h("tr", { tabindex: 0, onclick: () => rowDetail(d.columns, d.rows, i),
    onkeydown: (e) => {
      if (e.key === "Enter") rowDetail(d.columns, d.rows, i);
      else if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); step(e.currentTarget, 1); }
      else if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); step(e.currentTarget, -1); }
    } }, r.map((v, j) => cell(v, d.columns[j]))));
  const last = Math.min(d.offset + d.limit, d.total);
  const page = (n) => { grid.offset = Math.max(0, n); loadRows(); };
  const kind = data?.stores.find((s) => s.name === st)?.tables.find((t) => t.name === table)?.kind;
  const sql = `select * from "${table.replace(/"/g, '""')}"${kind === "table" ? " order by rowid desc" : ""} limit 100`;
  main.replaceChildren(
    h("div", { class: "grid-bar" },
      h("a", { class: "ghost back", href: "#data" }, "‹ Tables"),
      h("span", {}, d.total ? `${fmtN(d.offset + 1)}–${fmtN(last)} of ${fmtN(d.total)}${grid.q ? ` matching “${grid.q}”` : ""}` : grid.q ? `Nothing matches “${grid.q}”.` : "Empty."),
      h("span", { class: "grow" }),
      h("a", { class: "ghost", href: sqlHref(st, table), target: "_blank", rel: "noopener", title: "This table in Datasette: facets, filters, links to related rows" }, "Explore ↗"),
      h("a", { class: "ghost", href: `${sqlHref(st)}?sql=${encodeURIComponent(sql)}`, target: "_blank", rel: "noopener", title: "Write your own query on this store" }, "SQL ↗"),
      h("button", { class: "ghost icon", type: "button", disabled: d.offset === 0, onclick: () => page(d.offset - d.limit), "aria-label": "Previous page" }, "‹"),
      h("button", { class: "ghost icon", type: "button", disabled: last >= d.total, onclick: () => page(d.offset + d.limit), "aria-label": "Next page" }, "›")),
    h("div", { class: "grid-wrap card" }, h("table", { class: "grid" }, h("thead", {}, head), h("tbody", {}, body))),
    h("div", { class: "empty" }, grid.sort ? `Sorted by ${grid.sort}, ${grid.desc ? "largest" : "smallest"} first. ` : "Newest first. ",
      "Click a column to sort, a row to see all of it (↑ ↓ and Enter work too). *_cents columns are shown in euros."));
}
const eurs = (cents) => (cents / 100).toLocaleString("it-IT", { style: "currency", currency: "EUR" });

let sheetOpen = null;  // the open row sheet's close(), so a second one replaces it
function rowDetail(columns, rows, i) {
  const row = rows[i];
  const pretty = (v) => {
    if (typeof v === "string" && /^[[{]/.test(v)) { try { return JSON.stringify(JSON.parse(v), null, 2); } catch {} }
    return v == null ? "—" : String(v);
  };
  sheetOpen?.();
  const close = () => { sheet.remove(); removeEventListener("keydown", keys); sheetOpen = null;
    $$(".grid tbody tr")[i]?.focus({ preventScroll: true }); };
  const go = (n) => { if (rows[i + n]) rowDetail(columns, rows, i + n); };
  const copy = async () => {
    const obj = Object.fromEntries(columns.map((c, j) => [c, row[j]]));
    try { await navigator.clipboard.writeText(JSON.stringify(obj, null, 2)); toast("Row copied as JSON."); } catch { toast("The browser didn't allow copying here."); }
  };
  const sheet = h("div", { class: "sheet", onclick: (e) => e.target === sheet && close() },
    h("div", { class: "sheet-card card", role: "dialog", "aria-label": "Row" },
      h("div", { class: "sheet-head" }, h("h2", {}, `${grid.store} · ${grid.table}`),
        h("span", { class: "sheet-pos" }, `${grid.offset + i + 1} of ${fmtN(grid.offset + rows.length)}`),
        h("button", { class: "ghost icon", type: "button", disabled: i === 0, onclick: () => go(-1), "aria-label": "Previous row", title: "Previous row (←)" }, "‹"),
        h("button", { class: "ghost icon", type: "button", disabled: i === rows.length - 1, onclick: () => go(1), "aria-label": "Next row", title: "Next row (→)" }, "›"),
        h("button", { class: "ghost", type: "button", onclick: copy, title: "Copy this row as JSON" }, "Copy"),
        h("button", { class: "ghost", type: "button", onclick: close }, "Close")),
      h("dl", {}, columns.flatMap((c, j) => [h("dt", {}, c), h("dd", { class: row[j] == null ? "null" : "" }, pretty(row[j]),
        typeof row[j] === "number" && c.endsWith("_cents") ? h("small", {}, "  " + eurs(row[j])) : "")]))));
  document.body.append(sheet);
  const keys = (e) => {
    if (e.key === "Escape") close();
    else if (e.key === "ArrowRight" || e.key === "j") go(1);
    else if (e.key === "ArrowLeft" || e.key === "k") go(-1);
  };
  addEventListener("keydown", keys);
  sheetOpen = () => { sheet.remove(); removeEventListener("keydown", keys); };
}

// A context file: a memory's front matter as fields and its text (with [[links]] to the others), or plain text.
function showFile(id) {
  const main = $("#data-main"), files = contextFiles || [];
  const f = files.find((x) => x.id === id);
  $("#data-title").textContent = f ? f.name : "Context files";
  if (!f) {
    main.replaceChildren(h("div", { class: "page-in" },
      h("p", { class: "lead" }, "The text the agents work from: what they learned (one markdown file each, under ", h("code", {}, data.data_dir + "/memory"),
        "), the index of it each agent is shown, and its standing instructions (a run adds the date, its tools and the conversation)."),
      ...groupBy(files, "group").flatMap(([group, fs]) => [h("div", { class: "section" }, h("h2", {}, group), h("small", {}, String(fs.length))),
        h("div", { class: "card rows" }, fs.map((x) => h("a", { class: "row file-row", href: `#data/${CONTEXT}/${encodeURIComponent(x.id)}` },
          h("div", { class: "what" }, h("div", { class: "title" }, x.name), h("div", { class: "sub" }, firstLine(x.text))),
          h("div", { class: "when wide" }, x.modified ? when(x.modified) : ""))))]),
      files.length ? "" : h("div", { class: "empty" }, "Nothing here yet.")));
    return;
  }
  const link = (name) => files.find((x) => x.kind === "markdown" && x.name === name.split("/").pop());
  const linked = (text) => rich(text).replace(/\[\[([^\]\n]+)\]\]/g, (m, name) => {
    const to = link(name);
    return to ? `<a href="#data/${CONTEXT}/${encodeURIComponent(to.id)}">${name}</a>` : `<span class="missing" title="No memory with this name">${name}</span>`;
  });
  let meta = [], body = f.text;
  const fm = f.kind === "markdown" && /^---\n([\s\S]*?)\n---\n?/.exec(f.text);
  if (fm) { meta = fm[1].split("\n").map((l) => l.split(/:\s?(.*)/s)).filter((kv) => kv[0]); body = f.text.slice(fm[0].length); }
  main.replaceChildren(h("div", { class: "page-in file" },
    h("div", { class: "grid-bar" }, h("a", { class: "ghost back", href: `#data/${CONTEXT}` }, "‹ Files"),
      h("span", {}, f.group), f.bytes ? h("span", {}, kb(f.bytes)) : "", f.modified ? h("span", {}, "changed " + when(f.modified)) : "",
      h("span", { class: "grow" }),
      h("button", { class: "ghost", type: "button", onclick: async () => {
        try { await navigator.clipboard.writeText(f.text); toast("Copied."); } catch { toast("The browser didn't allow copying here."); }
      } }, "Copy")),
    meta.length ? h("dl", { class: "card fm" }, meta.flatMap(([k, v]) => [h("dt", {}, k), h("dd", { html: linked(v || "") })])) : "",
    h("div", { class: `card file-text${f.kind === "markdown" ? "" : " mono"}`, html: linked(body.trim() || "(empty)") }),
    f.id.startsWith("memory/") ? h("div", { class: "empty" }, "Read-only here: to change it, tell the assistant, or forget it in ", h("a", { href: "#memory" }, "Memory"), ".") : ""));
}
const firstLine = (text) => (text.replace(/^---\n[\s\S]*?\n---\n?/, "").trim().split("\n")[0] || "").slice(0, 140);

let dataqTimer;
$("#dataq").addEventListener("input", () => {
  clearTimeout(dataqTimer);
  dataqTimer = setTimeout(() => { grid = { ...grid, q: $("#dataq").value.trim(), offset: 0 }; loadRows(); }, 250);
});

// ---- files: drop anywhere, or 📎 ---------------------------------------------------------------
// The bot's path for a document (Core.receive): a bank or DEGIRO export is imported, anything else is read here and
// an assistant says what to do about it; what is typed in the composer goes along as the request.
const UPLOAD_MAX = 20 * 1048576;
async function sendFile(file) {
  if (busy) { toast("Franziska is still on the last message; drop it again when that's done."); return; }
  if (file.size > UPLOAD_MAX) { toast(`${file.name} is over 20 MB.`); return; }
  if (!showing("chat")) location.hash = "chat";
  const input = $("#input"), caption = input.value.trim();
  input.value = ""; grow();
  daySep(new Date().toLocaleDateString("sv"));
  addMine(`📎 ${file.name} · ${kb(file.size)}${caption ? "\n" + caption : ""}`);
  const form = new FormData();
  form.append("file", file);
  form.append("caption", caption);
  await converse("/api/files", form);
  refreshStatus();
}
$("#attach").addEventListener("click", () => $("#file").click());
$("#file").addEventListener("change", (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) sendFile(f); });

let drags = 0;
const carriesFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
addEventListener("dragenter", (e) => { if (!carriesFiles(e)) return; drags++; $("#drop").classList.add("on"); });
addEventListener("dragleave", (e) => { if (!carriesFiles(e)) return; if (--drags <= 0) { drags = 0; $("#drop").classList.remove("on"); } });
addEventListener("dragover", (e) => { if (carriesFiles(e)) e.preventDefault(); });
addEventListener("drop", async (e) => {
  if (!carriesFiles(e)) return;
  e.preventDefault();
  drags = 0;
  $("#drop").classList.remove("on");
  for (const f of e.dataTransfer.files) await sendFile(f);  // one after the other: each is a turn
});

// ---- memory ----------------------------------------------------------------------------------
const SCOPE_NAMES = { profile: "About you", life: "📅 Life", money: "💶 Money", mail: "✉️ Mail" };
let memories = [], rules = null;
async function showMemory() {
  try { [memories, rules] = await Promise.all([api("/api/memory"), api("/api/rules")]); }
  catch (e) { $("#memory").replaceChildren(h("div", { class: "note" }, e.message)); return; }
  renderMemory();
}
function renderMemory() {
  const q = $("#memq").value.trim().toLowerCase();
  const found = memories.filter((m) => !q || `${m.name} ${m.fact} ${m.detail}`.toLowerCase().includes(q));
  const out = [];
  for (const [scope, title] of Object.entries(SCOPE_NAMES)) {
    const group = found.filter((m) => m.scope === scope);
    if (!group.length) continue;
    out.push(h("div", { class: "section" }, h("h2", {}, title), h("small", {}, String(group.length))));
    out.push(h("div", { class: "card" }, group.map(memRow)));
  }
  if (!out.length) out.push(h("div", { class: "empty" }, q ? `Nothing learned about “${q}”.` : "I haven't learned anything yet."));
  else if (found.some((m) => !m.confirmed)) out.push(h("div", { class: "empty" }, "“guessed” = learned once, not confirmed: it goes after 30 days unused."));
  if (rules && !q) out.push(...rulesSection());
  $("#memory").replaceChildren(...out);
}

// The agents' standing rules (franziska/instructions.py): the built-in text until edited here; saving the built-in
// text again, or an empty box, resets. Reminder times: tasks.reminder_rules, also set by asking ("palestra sempre la sera").
function rulesSection() {
  const reload = async (msg) => { toast(msg); rules = await api("/api/rules"); renderMemory(); };
  const agentCard = (a) => {
    const box = h("textarea", { class: "rules-text", spellcheck: "false", rows: "14" });
    box.value = a.text;
    const count = h("small", {}, "");
    const tally = () => { count.textContent = `${box.value.split("\n").filter((l) => l.trim().startsWith("-")).length} rules · ${box.value.length} chars`; };
    box.addEventListener("input", tally); tally();
    const save = (text) => async () => {
      try { await reload((await api(`/api/rules/agents/${a.name}`, { method: "PUT", body: { text } })).result); }
      catch (e) { toast(e.message); }
    };
    return h("details", { class: "card rules" },
      h("summary", {}, h("span", {}, a.title), a.edited ? h("span", { class: "chip warn" }, "edited") : h("span", { class: "chip" }, "built-in"),
        a.stale ? h("span", { class: "chip warn", title: "The built-in rules changed after this edit: compare with them, or reset" }, "built-in changed") : ""),
      box,
      h("div", { class: "rules-bar" }, count,
        a.edited ? h("button", { class: "forget", type: "button", onclick: save("") }, "Reset to built-in") : "",
        h("button", { class: "run", type: "button", onclick: () => save(box.value)() }, "Save")));
  };
  const words = h("input", { placeholder: "palestra, gym  (* = every task)" });
  const at = h("input", { type: "time", value: "19:00" });
  const reminderRow = (r) => h("div", { class: "mem" },
    h("div", { class: "what" }, h("div", { class: "fact" }, `${r.words === "*" ? "every task" : r.words} → ${r.at}`)),
    h("button", { class: "forget", type: "button", onclick: async () => {
      try { await reload((await api(`/api/rules/reminders/${r.id}`, { method: "DELETE" })).result); } catch (e) { toast(e.message); }
    } }, "Remove"));
  return [
    h("div", { class: "section" }, h("h2", {}, "Rules"), h("small", {}, "what each assistant follows on every message; edits apply from the next one")),
    ...rules.agents.map(agentCard),
    h("div", { class: "section" }, h("h2", {}, "Reminder times"), h("small", {}, `tasks are reminded at ${rules.day_start} on their day unless a rule says otherwise`)),
    h("div", { class: "card" }, rules.reminders.map(reminderRow),
      h("form", { class: "wf-form", onsubmit: async (e) => {
        e.preventDefault();
        if (!words.value.trim()) return;
        try { await reload((await api("/api/rules/reminders", { method: "POST", body: { words: words.value, at: at.value } })).result.split("\n")[0]); }
        catch (err) { toast(err.message); }
      } },
        h("div", { class: "field" }, h("label", {}, "Tasks with these words"), words),
        h("div", { class: "field inline" }, h("label", {}, "at"), at),
        h("button", { class: "run", type: "submit" }, "Add"))),
  ];
}
function memRow(m) {
  const row = h("div", { class: "mem" });
  const forget = h("button", { class: "forget", type: "button", onclick: async () => {
    if (!forget.classList.contains("armed")) {
      forget.classList.add("armed"); forget.textContent = "Forget?";
      setTimeout(() => { forget.classList.remove("armed"); forget.textContent = "Forget"; }, 3000);
      return;
    }
    try {
      toast((await api(`/api/memory/${m.scope}/${encodeURIComponent(m.name)}`, { method: "DELETE" })).result);
      memories = memories.filter((x) => x !== m); renderMemory();
    } catch (e) { toast(e.message); }
  } }, "Forget");
  row.append(h("div", { class: "what" },
    h("div", { class: "name" }, m.name),
    h("div", { class: "fact" }, m.fact),
    m.detail ? h("div", { class: "detail" }, m.detail) : "",
    h("div", { class: "sub" },
      m.confirmed ? "" : h("span", { class: "chip warn" }, "guessed"),
      m.kind === "cache" ? h("span", { class: "chip" }, "used " + fmtDay(m.last_used, { day: "numeric", month: "short" })) : "",
      m.source ? h("span", { class: "chip" }, m.source) : "")), forget);
  return row;
}
$("#memq").addEventListener("input", renderMemory);

// ---- palette: Ctrl/⌘ K, jump anywhere -------------------------------------------------------------
// Views, every table, the context files, the workflows and a few actions, matched by the letters typed in order.
const pal = $("#palette"), palq = $("#palq");
let palItems = [], palShown = [], palAt = 0;
const nav = (hash) => () => { location.hash = hash; };
const tab = (url) => () => open(url, "_blank", "noopener");

async function openPalette() {
  pal.hidden = false;
  palq.value = "";
  palq.focus();
  palItems = [
    ...[["chat", "Chat"], ["today", "Today"], ["money", "Money"], ["flows", "Workflows"], ["code", "Coding jobs"], ["data", "Data"], ["memory", "Memory"], ["settings", "Settings"]]
      .map(([v, label]) => ({ label, kind: "view", run: nav(v) })),
    { label: "World", kind: "view", run: () => { location.href = "/world"; } },
    { label: "Telemetry: open Phoenix", kind: "tool", run: tab("/phoenix/") },
    { label: "Telemetry: stop Phoenix", kind: "tool", run: async () => toast((await api("/api/telemetry/stop", { method: "POST" })).state === "off" ? "Phoenix stopped." : "Phoenix didn't stop.") },
    { label: "SQL: Datasette, all stores", kind: "tool", run: tab("/sql/") },
    { label: "Context files", kind: "data", run: nav(`data/${CONTEXT}`) },
    { label: "New chat", kind: "action", run: () => $("#new-chat").click() },
    { label: "Undo last change", kind: "action", run: undo },
    ...status.modes.map((m) => ({ label: `Mode: ${MODE_LABELS[m].replace(/^\S+ /, "")}`, kind: "action", run: () => $(`#modes button:nth-child(${status.modes.indexOf(m) + 1})`)?.click() })),
  ];
  renderPalette();
  const [d, files, wfs] = await Promise.all([data ? data : api("/api/data").catch(() => null), contextFiles || loadContext().catch(() => []),
    flows || loadFlows().catch(() => [])]);
  if (d) data = d;
  palItems.push(
    ...(data?.stores || []).flatMap((st) => st.tables.map((t) => ({ label: `${st.name} › ${t.name}`, kind: t.kind, note: fmtN(t.rows),
      run: nav(`data/${st.name}/${encodeURIComponent(t.name)}`) }))),
    ...(data?.stores || []).map((st) => ({ label: `SQL on ${st.name}`, kind: "tool", run: tab(sqlHref(st.name)) })),
    ...(files || []).map((f) => ({ label: `${f.group} › ${f.name}`, kind: "file", run: nav(`data/${CONTEXT}/${encodeURIComponent(f.id)}`) })),
    ...(wfs || []).map((w) => ({ label: `Workflow: ${w.title}`, kind: "workflow", note: "/" + w.name, run: () => {
      location.hash = "flows"; setTimeout(() => $(`#flow-${w.name}`)?.scrollIntoView({ behavior: "smooth", block: "start" }), 300);
    } })));
  if (!pal.hidden) renderPalette();
}
const closePalette = () => { pal.hidden = true; };

// Letters in order, anywhere ('ldmov' finds 'ledger › movements'); earlier and tighter matches first.
function score(label, q) {
  const s = label.toLowerCase();
  if (!q) return 1;
  if (s.includes(q)) return 1000 - s.indexOf(q);
  let at = -1, gaps = 0;
  for (const c of q) { const i = s.indexOf(c, at + 1); if (i < 0) return 0; gaps += i - at - 1; at = i; }
  return 500 - gaps;
}
function renderPalette() {
  const q = palq.value.trim().toLowerCase().replace(/\s+/g, " ");
  palShown = palItems.map((it) => ({ it, s: score(it.label, q) })).filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s).slice(0, 40).map((x) => x.it);
  palAt = Math.min(palAt, Math.max(0, palShown.length - 1));
  $("#pal-list").replaceChildren(...palShown.map((it, i) => h("button", { type: "button", role: "option", "aria-selected": String(i === palAt),
    onmousemove: () => { if (palAt !== i) { palAt = i; mark(); } }, onclick: () => choose(it) },
    h("span", { class: `pk k-${it.kind}` }, it.kind), h("span", { class: "pl" }, it.label), it.note ? h("small", {}, it.note) : "")),
    palShown.length ? "" : h("div", { class: "empty" }, "Nothing matches."));
}
const mark = () => $$("#pal-list button").forEach((b, i) => { b.setAttribute("aria-selected", String(i === palAt)); if (i === palAt) b.scrollIntoView({ block: "nearest" }); });
const choose = (it) => { closePalette(); it.run(); };
palq.addEventListener("input", () => { palAt = 0; renderPalette(); });
palq.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); palAt = (palAt + (e.key === "ArrowDown" ? 1 : -1) + palShown.length) % Math.max(1, palShown.length); mark(); }
  else if (e.key === "Enter") { e.preventDefault(); if (palShown[palAt]) choose(palShown[palAt]); }
  else if (e.key === "Escape") closePalette();
});
pal.addEventListener("click", (e) => { if (e.target === pal) closePalette(); });
$("#find").addEventListener("click", openPalette);
addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); pal.hidden ? openPalette() : closePalette(); }
});

// ---- start -----------------------------------------------------------------------------------
window.addEventListener("hashchange", () => { closePalette(); go(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) { refreshStatus(); if (!busy) go(); } });
setInterval(() => { refreshStatus(); refreshTelemetry(); }, 30000);
refreshStatus().then(firstRun).then(go);
refreshTelemetry();
if ("serviceWorker" in navigator && isSecureContext) navigator.serviceWorker.register("/sw.js").catch(() => {});
