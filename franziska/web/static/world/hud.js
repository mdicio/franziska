// The glass over the world: the team bar, floating name tags, the card of the member in focus (a workflow's card runs
// it, with its settings), and the chat.
// The chat is the same conversation as the classic UI; what happens in it is told to the world through `on`.
import * as THREE from "three";
import { api, stream, rich, h, splitLabel } from "./net.js";
import { flowForm, runLabel } from "../flows.js";

const $ = (sel, el = document) => el.querySelector(sel);
// Where a member's details live in the classic view (lists, tables, every run)
const CLASSIC = { life: ["/#today", "Open Today ↗"], money: ["/#money", "Open Money ↗"], workflow: ["/#flows", "All runs ↗"] };
const hex = (c) => "#" + new THREE.Color(c).getHexString();

export class Hud {
  // cast: [{ key, label, kind, about, turns, glance, color, place }], lead: { name, color }, flows: GET /api/workflows
  // on: { go(key), turn({ phase: 'send' | 'status' | 'reply' | 'done', route }), status(s) }
  constructor({ status, lead, cast, days, flows, on }) {
    Object.assign(this, { status, lead, cast, days, flows, on });
    this.byKey = new Map(cast.map((m) => [m.key, m]));
    this.busy = false;
    this.renderTop();
    this.renderTeam();
    this.renderModes();
    this.bindChat();
    this.tags = new Map();
    $("#wake").addEventListener("click", () => this.wake());
  }

  // ---- top: the name, the desktop, a way back to the classic view ----------------------------------------------
  renderTop() {
    const s = this.status, assistants = this.cast.filter((m) => m.kind === "assistant").length, flows = this.cast.length - assistants;
    $("#who").textContent = this.lead.name;
    $("#state").textContent = [s.desktop ? "online" : "desktop asleep", `${assistants} assistants`, flows ? `${flows} workflows` : ""].filter(Boolean).join(" · ");
    document.body.classList.toggle("asleep", !s.desktop);
    $("#wake").hidden = s.desktop || !s.can_wake;
  }

  setStatus(s) { this.status = s; this.renderTop(); this.on.status?.(s); }

  async wake() {
    const b = $("#wake");
    b.disabled = true;
    b.textContent = "Waking… (1–2 min)";
    try { this.note((await api("/api/desktop/wake", { method: "POST" })).up ? "The desktop is up." : "The desktop didn't come up."); }
    catch (e) { this.note(e.message); }
    b.disabled = false;
    b.textContent = "🖥️ Wake";
    try { this.setStatus(await api("/api/status")); } catch {}
  }

  // new counts after a turn: the open card shows them
  update(members, flows) {
    for (const m of members) Object.assign(this.byKey.get(m.key) || {}, { turns: m.turns, glance: m.glance });
    if (flows) this.flows = flows;
    if (this.focused && !this.busy) this.focus(this.focused);
  }

  // ---- the team bar --------------------------------------------------------------------------------------------
  renderTeam() {
    const pill = (key, icon, name, color, title) => h("button", { type: "button", class: "pill", "data-key": key, title, style: `--c:${hex(color)}`, onclick: () => this.on.go(key) },
      h("span", { class: "pi" }, icon), h("span", {}, name));
    $("#team").replaceChildren(
      pill("", "◉", this.lead.name, this.lead.color, "Back to the centre (H)"),
      ...this.cast.map((m, i) => { const { icon, name } = splitLabel(m.label); return pill(m.key, icon, name, m.color, `${m.place} · key ${i + 1}`); }));
  }

  focus(key) {
    document.querySelectorAll("#team .pill").forEach((p) => p.classList.toggle("on", p.dataset.key === (key ?? "")));
    const m = this.byKey.get(key), card = $("#card");
    this.focused = m ? key : null;
    if (!m) { card.classList.remove("on"); return; }
    const { icon, name } = splitLabel(m.label), wf = this.flows?.find((f) => f.name === key);
    const classic = CLASSIC[m.kind === "workflow" ? "workflow" : key];
    card.style.setProperty("--c", hex(m.color));
    card.replaceChildren(
      h("div", { class: "card-kicker" }, `${m.place} · ${m.kind}`),
      h("div", { class: "card-title" }, h("span", { class: "pi" }, icon), name),
      h("p", {}, m.about ? m.about[0].toUpperCase() + m.about.slice(1) + "." : ""),
      m.glance?.length ? h("ul", { class: "glance" }, m.glance.map((g) => h("li", {}, g))) : "",
      h("div", { class: "card-stat" }, h("b", {}, String(m.turns)), ` ${m.kind === "workflow" ? "runs" : "messages"} in the last ${this.days} days`),
      wf ? flowForm(wf, h, { compact: true, disabled: this.busy || !wf.ready, onRun: (params, form) => this.runFlow(wf, params, form) }) : "",
      h("div", { class: "card-tools" },
        this.status.modes?.includes(m.key) ? h("button", { type: "button", class: "ghost", onclick: () => this.setMode(m.key) },
          this.status.mode === m.key ? `Talking only to ${name}` : `Talk only to ${name}`) : "",
        classic ? h("a", { class: "ghost", href: classic[0] }, classic[1]) : ""));
    card.classList.add("on");
  }

  async runFlow(wf, params, form) {
    if (this.busy) return this.note("Still on the last message; run it when that's done.");
    this.mine(runLabel(wf, params));
    this.open(true);
    form.running(true);
    await this.converse(`/api/workflows/${wf.name}`, { params });
    form.running(false);
  }

  // ---- name tags that follow the stations ----------------------------------------------------------------------
  tag(key, label, color, small) {
    const { icon, name } = splitLabel(label);
    const el = h("div", { class: `tag${small ? " small" : ""}`, style: `--c:${hex(color)}` }, h("span", { class: "pi" }, icon), h("span", {}, name));
    $("#tags").append(el);
    this.tags.set(key, el);
  }

  place(camera, anchors, width, height) {  // anchors: Map key → Vector3
    const v = new THREE.Vector3();
    for (const [key, el] of this.tags) {
      v.copy(anchors.get(key)).add({ x: 0, y: 9, z: 0 });
      const dist = camera.position.distanceTo(v);
      v.project(camera);
      if (v.z > 1 || dist > 1100) { el.style.opacity = 0; continue; }
      const x = (v.x * 0.5 + 0.5) * width, y = (-v.y * 0.5 + 0.5) * height, k = Math.max(0.6, Math.min(1.1, 260 / dist));
      el.style.transform = `translate(${x}px, ${y}px) translate(-50%, -100%) scale(${k})`;
      el.style.opacity = dist < 25 ? 0 : Math.min(1, (1100 - dist) / 300);
    }
  }

  working(key, on) { this.tags.get(key)?.classList.toggle("working", on); }

  // ---- mode: Auto, or every message to one assistant -----------------------------------------------------------
  renderModes() {
    const modes = this.status.modes || [];
    $("#modes").replaceChildren(...modes.map((m) => h("button", {
      type: "button", "aria-pressed": String(this.status.mode === m), onclick: () => this.setMode(m),
      title: m === "auto" ? "Franziska picks who answers" : `Every message goes to ${m}`,
    }, m === "auto" ? "Auto" : splitLabel(this.status.labels?.[m] || m).icon + " " + splitLabel(this.status.labels?.[m] || m).name)));
  }

  async setMode(mode) {
    try { Object.assign(this.status, await api("/api/mode", { method: "POST", body: { mode } })); } catch (e) { return this.note(e.message); }
    this.renderModes();
    const open = [...document.querySelectorAll("#team .pill.on")][0]?.dataset.key;
    if (open) this.focus(open);
  }

  // ---- chat ----------------------------------------------------------------------------------------------------
  bindChat() {
    const input = $("#input");
    $("#composer").addEventListener("submit", (e) => { e.preventDefault(); this.send(); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !matchMedia("(pointer: coarse)").matches) { e.preventDefault(); this.send(); }
      if (e.key === "Escape") input.blur();
    });
    input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 140) + "px"; });
    input.addEventListener("focus", () => this.open(true));
    $("#fold").addEventListener("click", () => this.open(!$("#chat").classList.contains("open")));
    addEventListener("keydown", (e) => {
      if (e.target.closest("input, textarea, select, button")) return;
      if (e.key === "Enter" || e.key === "/") { e.preventDefault(); input.focus(); }
    });
    // a file: 📎 or dropped anywhere on the world (the same path as the classic view and Telegram)
    $("#attach").addEventListener("click", () => $("#file").click());
    $("#file").addEventListener("change", (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) this.sendFile(f); });
    let drags = 0;
    const files = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
    addEventListener("dragenter", (e) => { if (files(e)) { drags++; $("#drop").classList.add("on"); } });
    addEventListener("dragleave", (e) => { if (files(e) && --drags <= 0) { drags = 0; $("#drop").classList.remove("on"); } });
    addEventListener("dragover", (e) => { if (files(e)) e.preventDefault(); });
    addEventListener("drop", async (e) => {
      if (!files(e)) return;
      e.preventDefault();
      drags = 0;
      $("#drop").classList.remove("on");
      for (const f of e.dataTransfer.files) await this.sendFile(f);
    });
  }

  async sendFile(file) {
    if (this.busy) return this.note("Still on the last message; drop it again when that's done.");
    if (file.size > 20 * 1048576) return this.note(`${file.name} is over 20 MB.`);
    const input = $("#input"), caption = input.value.trim();
    input.value = "";
    input.style.height = "auto";
    this.mine(`📎 ${file.name}${caption ? "\n" + caption : ""}`);
    this.open(true);
    const form = new FormData();
    form.append("file", file);
    form.append("caption", caption);
    await this.converse("/api/files", form);
  }

  open(on) {
    $("#chat").classList.toggle("open", on);
    $("#fold").setAttribute("aria-expanded", String(on));
    if (on) this.scroll();
  }

  async history() {
    const turns = await api("/api/chat").catch(() => []);
    const log = $("#log");
    log.replaceChildren();
    for (const t of turns) { this.mine(t.text); this.hers({ text: t.reply || "(no reply)", route: t.route, label: t.label }); }
    if (!turns.length) log.append(h("div", { class: "hello" }, `Ciao, I'm ${this.lead.name}. Ask me anything; the one of us who knows best will answer.`));
    this.scroll();
  }

  scroll() { requestAnimationFrame(() => { const l = $("#log"); l.scrollTop = l.scrollHeight; }); }

  mine(text) {
    $(".hello", $("#log"))?.remove();
    $("#log").append(h("div", { class: "msg me" }, h("div", { class: "bubble" }, text)));
  }

  hers(r) {
    const m = this.byKey.get(r.route), actions = h("div", { class: "actions" });
    const msg = h("div", { class: `msg her${r.stopped ? " stopped" : ""}`, style: m ? `--c:${hex(m.color)}` : "" },
      r.label ? h("div", { class: "from" }, r.label) : "",
      h("div", { class: "bubble", html: rich(r.text) }), actions);
    for (const a of r.actions || []) actions.append(h("button", { type: "button", onclick: (ev) => this.tap(a.id, ev.currentTarget, msg) }, a.label));
    $("#log").append(msg);
    $("#last").replaceChildren(h("span", { class: "from" }, r.label || this.lead.name), h("span", { html: rich(r.text.split("\n")[0].slice(0, 140)) }));
    return msg;
  }

  live(text) {
    const el = $("#live");
    el.classList.toggle("on", !!text);
    if (text) el.replaceChildren(h("span", { class: "dots" }, h("i"), h("i"), h("i")), h("span", {}, text));
  }

  note(text) {
    const el = $("#note");
    el.textContent = text;
    el.classList.add("on");
    clearTimeout(this.noteTimer);
    this.noteTimer = setTimeout(() => el.classList.remove("on"), 3500);
  }

  async send() {
    const input = $("#input"), text = input.value.trim();
    if (!text || this.busy) return;
    input.value = "";
    input.style.height = "auto";
    this.mine(text);
    this.open(true);
    await this.converse("/api/chat", { text });
  }

  async tap(id, button, msg) {
    if (this.busy) return;
    button.classList.add("tapped");
    if (!id.startsWith("wf:")) msg.querySelectorAll(".actions button").forEach((b) => (b.disabled = true));
    await this.converse("/api/action", { id }, msg);
  }

  async converse(path, body, msg) {
    this.busy = true;
    $("#send").disabled = true;
    this.on.turn({ phase: "send" });
    this.live("…");
    let route = null;
    try {
      await stream(path, body, (ev) => {
        if (ev.type === "status") {
          this.live(ev.text);
          if (ev.route && ev.route !== route) { route = ev.route; this.on.turn({ phase: "status", route }); }
        } else if (ev.type === "applied") {
          const b = msg?.querySelector(".actions button.tapped");
          if (b) b.replaceWith(h("span", { class: "applied" }, ev.text));
        } else if (ev.type === "reply") {
          this.live("");
          this.hers(ev);
          this.on.turn({ phase: "reply", route: ev.route || route });
        }
        this.scroll();
      });
    } catch (e) {
      this.hers({ text: `I couldn't reach the server (${e.message}).`, stopped: "error" });
    } finally {
      this.live("");
      this.busy = false;
      $("#send").disabled = false;
      this.on.turn({ phase: "done" });
    }
  }
}
