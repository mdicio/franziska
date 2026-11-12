// Franziska's world: the team as a landscape. Everything here is laid out from /api/world (who is on the team, what
// kind, how busy) and driven by the chat stream (who is working right now); nothing knows what a member does inside.
// A new assistant or workflow on the server gets a region, a station and a link without a change here.
import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";
import { api, store } from "./net.js";
import { BIOMES, ORDER } from "./biomes.js";
import { Land } from "./land.js";
import { Atmosphere, waveAt } from "./atmos.js";
import { Lead, Station, Links } from "./team.js";
import { Fly } from "./fly.js";
import { Hud } from "./hud.js";

// Optional: the landscape a member would suit. Anyone not listed takes the next free one in biomes.ORDER.
const PREFER = { life: "alpine", money: "harbour", mail: "glacier", chat: "meadow", markets: "surf", mail_bills: "dunes", categorise: "forest" };
const LEAD = 0x9fe8ff;

const params = new URLSearchParams(location.search);
const quality = params.get("quality") || store.get("world-quality") || (matchMedia("(pointer: coarse)").matches ? "low" : "high");
const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;

async function load() {
  const [status, world, flows] = await Promise.all([api("/api/status"), api("/api/world").catch(() => null), api("/api/workflows").catch(() => [])]);
  const members = world?.members || Object.entries(status.labels).map(([key, label]) => ({ key, label, kind: "assistant", about: "", turns: 0 }));
  return { status, flows, world: world || { lead: { name: "Franziska", turns: 0 }, members, days: 7 } };
}

// Where each member lives: assistants on an inner circle, workflows further out between them.
function layout(members) {
  const used = new Set(), pick = (m) => {
    let id = PREFER[m.key];
    if (!id || used.has(id) || !BIOMES[id]) id = ORDER.find((b) => !used.has(b)) || ORDER[used.size % ORDER.length];
    used.add(id);
    return BIOMES[id];
  };
  const inner = members.filter((m) => m.kind !== "workflow"), outer = members.filter((m) => m.kind === "workflow");
  // workflows go in the gaps between assistants, spread out: 3 workflows among 4 assistants take 3 of the 4 gaps
  const gap = (i, n) => (Math.floor(i * Math.max(inner.length, 1) / n) + 0.5) / Math.max(inner.length, 1) * n;
  const ring = (list, r, offset, slot = (i) => i) => list.map((m, i) => {
    const a = offset + slot(i, list.length) / list.length * Math.PI * 2;
    return { member: m, x: Math.cos(a) * r, z: Math.sin(a) * r, reach: m.kind === "workflow" ? 0.8 : 1, biome: pick(m) };
  });
  const start = -Math.PI / 2;
  return [...ring(inner, 270 + Math.max(0, inner.length - 5) * 25, start),
          ...ring(outer, 445, start, gap)];
}

function engine() {
  const canvas = document.getElementById("scene");
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: quality !== "low", powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(devicePixelRatio, quality === "low" ? 1.25 : 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(55, 1, 0.5, 3200);
  let composer = null;
  if (quality !== "low") {
    composer = new EffectComposer(renderer);
    composer.addPass(new RenderPass(scene, camera));
    composer.addPass(new UnrealBloomPass(new THREE.Vector2(256, 256), 0.7, 0.5, 1.25));
    composer.addPass(new OutputPass());
  }
  const resize = () => {
    const w = innerWidth, h = innerHeight;
    renderer.setSize(w, h, false);
    composer?.setSize(w, h);
    camera.aspect = w / h;
    camera.fov = w < 700 ? 65 : 55;
    camera.updateProjectionMatrix();
  };
  addEventListener("resize", resize);
  resize();
  return { renderer, scene, camera, render: () => (composer ? composer.render() : renderer.render(scene, camera)) };
}

async function main() {
  const { status, world, flows } = await load();
  const { renderer, scene, camera, render } = engine();
  const sites = layout(world.members);
  const land = new Land([{ x: 0, z: 0, reach: 1.15, biome: BIOMES.hub }, ...sites]);
  const ground = land.mesh(quality === "low" ? 150 : 260);
  scene.add(ground);
  for (const m of land.scatter(quality === "low" ? 0.45 : 1)) scene.add(m);

  const air = new Atmosphere(scene, quality);
  const hour = () => params.has("hour") ? Number(params.get("hour")) : new Date().getHours() + new Date().getMinutes() / 60;
  air.setHour(hour());
  setInterval(() => air.setHour(hour()), 60_000);

  const updates = [], add = (...objs) => scene.add(...objs), sea = { waveAt };
  for (const site of land.sites) {
    const tick = site.biome.feature?.(site, land, add, sea);
    if (tick) updates.push(tick);
  }

  const lead = new Lead(LEAD, Math.max(land.heightAt(0, 0), 1));
  scene.add(lead.group);
  const links = new Links(scene), stations = new Map(), anchors = new Map();
  const cast = sites.map((s) => ({ ...s.member, color: s.biome.accent, place: s.biome.name }));
  for (const s of sites) {
    const y = Math.max(land.heightAt(s.x, s.z), 0.8) - 0.3, st = new Station(s.member, new THREE.Vector3(s.x, y, s.z), s.biome.accent);
    scene.add(st.group);
    stations.set(s.member.key, st);
    anchors.set(s.member.key, st.anchor);
    links.add(s.member.key, lead.anchor, st.anchor, s.biome.accent);
  }
  anchors.set("", lead.anchor);

  const fly = new Fly(camera, renderer.domElement, land, { onTap: tapped });
  let focused = null;
  const hud = new Hud({
    status, lead: { name: world.lead.name, color: LEAD }, cast, days: world.days, flows,
    on: { go, turn, status: (s) => { if (!working) lead.set(s.desktop ? "idle" : "asleep"); } },
  });
  hud.tag("", world.lead.name, LEAD, false);
  for (const m of cast) hud.tag(m.key, m.label, m.color, m.kind === "workflow");
  hud.history();

  function go(key, rate = 2) {
    focused = key || null;
    hud.focus(focused);
    const st = stations.get(key);
    if (!st) return fly.glide(new THREE.Vector3(0, 0, 0), { dist: 150, pitch: 0.42, rate });
    const p = st.group.position, away = Math.atan2(p.x, p.z);  // from the centre's side, looking out: the station with its landmark behind it
    fly.glide(p.clone(), { dist: st.member.kind === "workflow" ? 70 : 95, pitch: 0.32, yaw: away + Math.PI + 0.35, rate });
  }

  const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
  function tapped(x, y) {
    ndc.set(x / innerWidth * 2 - 1, -(y / innerHeight) * 2 + 1);
    ray.setFromCamera(ndc, camera);
    const hit = ray.intersectObjects([...stations.values()].map((s) => s.hit), false)[0];
    if (hit) return go(hit.object.userData.station.member.key);
    if (ray.intersectObject(lead.core, false).length) return go("");
    const ground = ray.intersectObject(scene.getObjectByName("land"), false)[0];
    if (ground) { focused = null; hud.focus(null); fly.glide(ground.point, { rate: 1.6 }); }
  }

  // the conversation, as the world sees it
  let working = null;
  function turn({ phase, route }) {
    if (phase === "send") { lead.set("routing", 0xffffff); return; }
    if (phase === "status") {
      if (working && working !== route) { stations.get(working) && (stations.get(working).want = 0); links.active(working, false); hud.working(working, false); }
      working = route;
      const st = stations.get(route);
      lead.set("working", st ? st.color : 0xd8e6ff);
      if (st) { st.want = 1; hud.working(route, true); }
      links.active(route, true);
      if (!st) links.pulse(null, new THREE.Color(0xd8e6ff), false);
      if (st && performance.now() - fly.touched > 5000 && focused !== route) go(route, 1.4);  // follow the conversation
      return;
    }
    if (phase === "reply") {
      if (route) links.burst(route, true);
      return;
    }
    if (working) { stations.get(working) && (stations.get(working).want = 0); links.active(working, false); hud.working(working, false); }
    working = null;
    lead.set(hud.status.desktop ? "idle" : "asleep");
    Promise.all([api("/api/world"), api("/api/workflows")]).then(([w, f]) => hud.update(w.members, f)).catch(() => {});  // the counts moved
  }

  setInterval(async () => {
    try { hud.setStatus(await api("/api/status")); } catch {}
  }, 30_000);
  lead.set(status.desktop ? "idle" : "asleep");

  addEventListener("keydown", (e) => {
    if (e.target.closest("input, textarea, select, button") || e.metaKey || e.ctrlKey) return;
    if (e.key === "h" || e.key === "0") go("");
    const n = Number(e.key);
    if (n >= 1 && n <= cast.length) go(cast[n - 1].key);
    if (e.key === "Escape") { focused = null; hud.focus(null); }
  });

  document.getElementById("quality").textContent = quality === "low" ? "Lite" : "Full";
  document.getElementById("quality").addEventListener("click", () => { store.set("world-quality", quality === "low" ? "high" : "low"); location.reload(); });

  // arrive from high above, then settle over the centre
  fly.yaw = 0.9; fly.pitch = 1.1; fly.dist = 900;
  fly.glide(new THREE.Vector3(0, 0, 0), { dist: 175, pitch: 0.42, yaw: 0.5, rate: 0.7 });
  hud.focus(null);
  document.body.classList.add("ready");

  const clock = new THREE.Clock();
  let t = 0;
  renderer.setAnimationLoop(() => {
    const dt = Math.min(clock.getDelta(), 0.1) * (calm ? 0.5 : 1);
    t += dt;
    fly.update(dt);
    lead.update(t, dt);
    for (const st of stations.values()) st.update(t, dt);
    links.update(t, dt);
    for (const u of updates) u(t, dt);
    air.update(t, camera, fly.focus, land.blend(fly.focus.x, fly.focus.z, "snow"));
    hud.place(camera, anchors, innerWidth, innerHeight);
    render();
  });
}

main().catch((e) => {
  console.error(e);
  document.getElementById("loading").textContent = `The world didn't load: ${e.message}`;
});
