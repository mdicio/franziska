// The catalogue of landscapes. Each member of the team gets one; a new biome is one entry here.
//   height(x, z, d, n)  the ground at a point, d = distance to the member's site, n = noise helpers
//   paint               the colours by height and slope (see land.js)
//   snow                how much snow falls there (0..1)
//   props               what grows or stands there, scattered by density
//   feature(site, land) the landmark and its life (a ski run, a lighthouse…); returns update(t, dt) or nothing
import * as THREE from "three";
import { Scatter } from "./props.js";
import { lerp } from "./noise.js";

const gauss = (d, r) => Math.exp(-((d / r) ** 2));
// distance to a point `by` further out from the centre than the site: a summit behind the station, not under it
const beyond = (x, z, site, by) => { const r = Math.hypot(site.x, site.z) || 1; return Math.hypot(x - site.x * (1 + by / r), z - site.z * (1 + by / r)); };

export const BIOMES = {
  hub: {
    name: "The Commons", accent: 0x9fe8ff, snow: 0,
    height: (x, z, d, n) => 7 + 1.2 * n.fbm(x / 60, z / 60) + 2 * gauss(d, 40),
    paint: { sand: 0xd9c9a3, grass: 0x7ea35a, grass2: 0x8fb366, rock: 0x8d8a83, snowline: 999, sandline: 2.2 },
    props: [{ kind: "tree", density: 0.25, min: 3, max: 30, far: 60 }, { kind: "flower", density: 1.2, min: 3, max: 30, far: 50 }],
  },
  alpine: {
    name: "Ski slopes", accent: 0x7fe0c3, snow: 0.55,
    height: (x, z, d, n, s) => 10 + 150 * gauss(beyond(x, z, s, 105), 105) * (0.7 + 0.6 * n.ridged(x / 140, z / 140)) + 22 * n.ridged(x / 80, z / 80),
    paint: { sand: 0xcab994, grass: 0x4f7d4a, grass2: 0x6a9455, rock: 0x6f6e78, snow: 0xf4f8fb, snowline: 58, sandline: 2 },
    props: [{ kind: "pine", density: 1.4, min: 6, max: 50, slope: 0.8 }, { kind: "snowpine", density: 1.2, min: 40, max: 80, slope: 0.8 },
            { kind: "rock", density: 0.4, min: 20, max: 140 }],
    feature: skiRun,
  },
  glacier: {
    name: "Glacier", accent: 0x8fc8ff, snow: 1,
    height: (x, z, d, n, s) => 14 + 80 * gauss(beyond(x, z, s, 80), 105) * (0.8 + 0.4 * n.ridged(x / 90, z / 90)) + 6 * n.fbm(x / 70, z / 70) + 16 * n.ridged(x / 120, z / 120),
    paint: { sand: 0xd8e2e8, grass: 0xdfe9ef, grass2: 0xc9dbe6, rock: 0x8d9aa8, snow: 0xf7fbff, snowline: 12, sandline: 1.5 },
    props: [{ kind: "ice", density: 0.7, min: 6, max: 90 }, { kind: "snowpine", density: 0.5, min: 6, max: 30, slope: 0.7 }],
    feature: refuge,
  },
  harbour: {
    name: "Harbour", accent: 0xffb547, snow: 0,
    height: (x, z, d, n) => -24 + 36 * gauss(d, 36) + 6 * n.fbm(x / 50, z / 50),
    paint: { sand: 0xe6d3a8, grass: 0x7c8f4c, grass2: 0x95a35a, rock: 0x9b8f80, snowline: 999, sandline: 3 },
    props: [{ kind: "umbrellapine", density: 1.5, min: 4, max: 30 }, { kind: "rock", density: 0.3, min: 0, max: 8 }],
    feature: harbour,
  },
  surf: {
    name: "Surf break", accent: 0x4de2ff, snow: 0,
    height: (x, z, d, n) => -9 + 17 * gauss(d, 95) + 3 * n.fbm(x / 40, z / 40),
    paint: { sand: 0xf0dcae, grass: 0xb9b06a, grass2: 0x9da35a, rock: 0xa89a86, snowline: 999, sandline: 6 },
    props: [{ kind: "parasol", density: 0.5, min: 1, max: 5, far: 90 }, { kind: "umbrellapine", density: 0.6, min: 6, max: 30 }],
    feature: surfBreak,
  },
  meadow: {
    name: "Meadows", accent: 0xc49bff, snow: 0,
    height: (x, z, d, n) => 9 + 11 * n.fbm(x / 110, z / 110) + 8 * gauss(d, 80),
    paint: { sand: 0xd9c9a3, grass: 0x6f9f48, grass2: 0x8db85a, rock: 0x8a857a, snowline: 999, sandline: 2.2 },
    props: [{ kind: "flower", density: 4, min: 3, max: 40 }, { kind: "tree", density: 0.6, min: 3, max: 40 }],
    feature: windmill,
  },
  forest: {
    name: "Pine forest", accent: 0xa6e36b, snow: 0.1,
    height: (x, z, d, n) => 12 + 18 * n.fbm(x / 90, z / 90) + 10 * gauss(d, 90),
    paint: { sand: 0xcdbb92, grass: 0x3f6b3a, grass2: 0x507f42, rock: 0x6e6a62, snow: 0xeef4f6, snowline: 60, sandline: 2 },
    props: [{ kind: "pine", density: 5, min: 3, max: 60, slope: 0.9 }, { kind: "tree", density: 1, min: 3, max: 40 }, { kind: "rock", density: 0.4, min: 3, max: 60 }],
  },
  dunes: {
    name: "Dunes", accent: 0xff8a65, snow: 0,
    height: (x, z, d, n) => 6 + 6 * Math.abs(Math.sin((x * 0.7 + z * 0.3) / 16 + 3 * n.fbm(x / 120, z / 120))) + 4 * n.fbm(x / 60, z / 60) + 8 * gauss(d, 70),
    paint: { sand: 0xe8c48a, grass: 0xe2b77c, grass2: 0xd9a86c, rock: 0xb4684a, snowline: 999, sandline: 99 },
    props: [{ kind: "redrock", density: 0.8, min: 2, max: 40 }],
  },
};

// Which landscape when nothing better is known: the order new members take them in.
export const ORDER = ["alpine", "harbour", "glacier", "meadow", "surf", "dunes", "forest"];

// ---- landmarks --------------------------------------------------------------------------------------------------

// Downhill from near the top: slalom gates, a skier who never tires, a chairlift back up.
function skiRun(site, land, add) {
  const away = Math.atan2(site.z, site.x), down = away + Math.PI;  // from under the summit down towards the station
  let x = site.x + Math.cos(away) * 85, z = site.z + Math.sin(away) * 85;
  const path = [];
  for (let i = 0; i < 160 && land.heightAt(x, z) > 9 && Math.hypot(x - site.x, z - site.z) > 16; i++) {
    const [gx, gz] = land.gradient(x, z), g = Math.hypot(gx, gz) || 1;
    const dx = -gx / g * 0.6 + Math.cos(down) * 0.4, dz = -gz / g * 0.6 + Math.sin(down) * 0.4, l = Math.hypot(dx, dz);
    x += dx / l * 3; z += dz / l * 3;
    path.push(new THREE.Vector3(x, land.heightAt(x, z), z));
  }
  if (path.length < 12) return;
  const red = new Scatter("flag", 40), blue = new Scatter("blueflag", 40);
  for (let i = 6, k = 0; i < path.length - 2; i += 7, k++) {
    const p = path[i], q = path[i + 1], nx = -(q.z - p.z), nz = q.x - p.x, l = Math.hypot(nx, nz) || 1, side = k % 2 ? 4 : -4;
    (k % 2 ? red : blue).place(p.x + nx / l * side, land.heightAt(p.x + nx / l * side, p.z + nz / l * side) - 0.2, p.z + nz / l * side, Math.random() * 6);
  }
  add(red.mesh, blue.mesh);
  // chairlift from the bottom of the run back to the top
  const top = path[0], bottom = path[path.length - 1], pylons = new Scatter("pylon", 16), cable = [];
  const n = Math.max(3, Math.round(top.distanceTo(bottom) / 40));
  for (let i = 0; i <= n; i++) {
    const t = i / n, px = lerp(bottom.x, top.x, t) + 14, pz = lerp(bottom.z, top.z, t) + 14, py = land.heightAt(px, pz) - 0.5;
    pylons.place(px, py, pz, Math.atan2(top.x - bottom.x, top.z - bottom.z) + Math.PI / 2);
    cable.push(new THREE.Vector3(px, py + 12, pz));
  }
  add(pylons.mesh, new THREE.Line(new THREE.BufferGeometry().setFromPoints(cable), new THREE.LineBasicMaterial({ color: 0x30343a })));
  const hut = new Scatter("hut", 1);
  hut.place(bottom.x + 10, land.heightAt(bottom.x + 10, bottom.z) - 0.3, bottom.z, away);
  add(hut.mesh);
  const curve = new THREE.CatmullRomCurve3(path), skier = new Scatter("person", 2), skis = new Scatter("skis", 2);
  add(skier.mesh, skis.mesh);
  const len = curve.getLength(), P = new THREE.Vector3(), T = new THREE.Vector3();
  return (t) => {
    for (let s = 0; s < 2; s++) {
      const u = ((t * 9 + s * len / 2) % len) / len;
      curve.getPointAt(u, P); curve.getTangentAt(u, T);
      const sway = Math.sin(u * 60) * 2.2, nx = -T.z, nz = T.x, nl = Math.hypot(nx, nz) || 1;
      const x = P.x + nx / nl * sway, z = P.z + nz / nl * sway, y = land.heightAt(x, z), yaw = Math.atan2(T.x, T.z) + Math.cos(u * 60) * 0.5;
      skier.move(s, x, y + 0.05, z, yaw, 1, 0.25, Math.cos(u * 60) * 0.3);
      skis.move(s, x, y + 0.05, z, yaw);
    }
  };
}

// A mountain hut among ice spires.
function refuge(site, land, add) {
  const hut = new Scatter("hut", 1), x = site.x + 18, z = site.z + 8;
  hut.place(x, land.heightAt(x, z) - 0.4, z, 0.6, 1.3);
  add(hut.mesh);
}

// A lighthouse on the islet, its beam turning, boats sailing round and buoys riding the swell.
function harbour(site, land, add, world) {
  let lx = site.x, lz = site.z, best = -1e9;
  for (let a = 0; a < 12; a++) for (const r of [0, 8, 16]) {
    const x = site.x + Math.cos(a) * r, z = site.z + Math.sin(a) * r, y = land.heightAt(x, z);
    if (y > best) { best = y; lx = x; lz = z; }
  }
  const r = Math.hypot(site.x, site.z) || 1;  // the station takes the top of the islet, the lighthouse its far side
  lx += site.x / r * 17; lz += site.z / r * 17;
  const light = new Scatter("lighthouse", 1);
  light.place(lx, land.heightAt(lx, lz) - 0.3, lz, 0, 1.1);
  add(light.mesh);
  const beam = new THREE.Mesh(new THREE.ConeGeometry(3.5, 55, 16, 1, true),
    new THREE.MeshBasicMaterial({ color: 0xfff1c0, transparent: true, opacity: 0.06, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide }));
  beam.geometry.translate(0, -27.5, 0).rotateZ(Math.PI / 2);
  const pivot = new THREE.Group();
  pivot.position.set(lx, land.heightAt(lx, lz) + 13.8, lz);
  pivot.add(beam);
  add(pivot);
  const boats = new Scatter("boat", 6), buoys = new Scatter("buoy", 24), lanes = [], marks = [];
  for (let i = 0; i < 4; i++) lanes.push({ r: 70 + i * 18, a: i * 1.7, w: (i % 2 ? -1 : 1) * (0.05 + i * 0.01) });
  for (let i = 0; i < 18; i++) { const a = i / 18 * Math.PI * 2, r = 52 + (i % 3) * 6; marks.push([site.x + Math.cos(a) * r, site.z + Math.sin(a) * r]); }
  lanes.forEach(() => boats.place(0, -50, 0));
  marks.forEach(() => buoys.place(0, -50, 0));
  add(boats.mesh, buoys.mesh);
  return (t) => {
    pivot.rotation.y = t * 0.6;
    lanes.forEach((b, i) => {
      const a = b.a + t * b.w, x = site.x + Math.cos(a) * b.r, z = site.z + Math.sin(a) * b.r;
      if (land.heightAt(x, z) > -1.5) { boats.move(i, 0, -50, 0); return; }  // never sail over land
      boats.move(i, x, world.waveAt(x, z, t) - 0.2, z, -a + (b.w > 0 ? 0 : Math.PI), 1, Math.sin(t + i) * 0.06, Math.sin(t * 1.3 + i) * 0.1);
    });
    marks.forEach(([x, z], i) => buoys.move(i, x, land.heightAt(x, z) < -1 ? world.waveAt(x, z, t) - 0.3 : -50, z, 0, 1, Math.sin(t * 1.7 + i) * 0.15));
  };
}

// Swell rolling in on the beach, and a surfer riding it.
function surfBreak(site, land, add, world) {
  let sea = 0, low = 1e9;  // the direction from the beach out to open water
  for (let a = 0; a < Math.PI * 2; a += Math.PI / 16) {
    const y = land.heightAt(site.x + Math.cos(a) * 110, site.z + Math.sin(a) * 110);
    if (y < low) { low = y; sea = a; }
  }
  const ox = Math.cos(sea), oz = Math.sin(sea), px = -oz, pz = ox;
  let shore = 30;  // where the sand meets the water, walking out to sea
  while (shore < 160 && land.heightAt(site.x + ox * shore, site.z + oz * shore) > 0.2) shore += 2;
  const foam = new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.7, depthWrite: false });
  const crests = [];
  for (let i = 0; i < 4; i++) {
    const m = new THREE.Mesh(new THREE.BoxGeometry(90, 0.5, 1.6), foam.clone());
    m.rotation.y = -sea + Math.PI / 2;
    crests.push(m);
    add(m);
  }
  const boards = new Scatter("surfboard", 8);
  for (let i = 0; i < 6; i++) {
    const s = shore - 12 - (i % 2) * 4, w = (i - 2.5) * 6, x = site.x + ox * s + px * w, z = site.z + oz * s + pz * w;
    boards.place(x, land.heightAt(x, z) - 0.3, z, sea + 0.2 * i, 1, -0.2);
  }
  const rider = new Scatter("person", 1), board = new Scatter("surfboard", 1);
  add(boards.mesh, rider.mesh, board.mesh);
  const period = 7;
  return (t) => {
    crests.forEach((m, i) => {
      const u = ((t / period + i / crests.length) % 1), d = shore + 110 * (1 - u);
      m.position.set(site.x + ox * d, world.waveAt(site.x + ox * d, site.z + oz * d, t) + 0.4, site.z + oz * d);
      m.material.opacity = Math.sin(u * Math.PI) * 0.75;
      m.scale.x = 0.6 + u * 0.5;
    });
    const u = (t / period) % 1, d = shore + 110 * (1 - u) + 1.5, w = Math.sin(t * 0.7) * 30;
    const x = site.x + ox * d + px * w, z = site.z + oz * d + pz * w, y = world.waveAt(x, z, t) + 0.6, yaw = Math.atan2(px, pz) * (Math.cos(t * 0.7) > 0 ? 1 : -1);
    board.move(0, x, y - 1, z, yaw, 1, Math.PI / 2 - 0.1);
    rider.move(0, x, y, z, yaw + Math.PI / 2, 0.95, 0, Math.sin(t * 2) * 0.15);
  };
}

function windmill(site, land, add) {
  const x = site.x + 20, z = site.z - 10, y = land.heightAt(x, z) - 0.3, mill = new Scatter("windmill", 1);
  mill.place(x, y, z, 0.4);
  const blades = new THREE.Group(), mat = new THREE.MeshStandardMaterial({ color: 0xf2ece0, flatShading: true });
  for (let i = 0; i < 4; i++) { const b = new THREE.Mesh(new THREE.BoxGeometry(0.6, 6.5, 0.15), mat); b.position.y = 3.3; const arm = new THREE.Group(); arm.rotation.z = i * Math.PI / 2; arm.add(b); blades.add(arm); }
  blades.position.set(x + Math.sin(0.4) * 1.8, y + 8.5, z + Math.cos(0.4) * 1.8);
  blades.rotation.y = 0.4;
  add(mill.mesh, blades);
  return (t, dt) => { blades.rotation.z -= dt * 0.8; };
}
