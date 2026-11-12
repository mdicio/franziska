// Moving through the world. The camera circles a focus point that walks over the ground:
//   mouse: drag to look around, right-drag (or shift-drag) to move, wheel to zoom
//   touch: one finger looks, two fingers move and pinch to zoom
//   keys:  WASD / arrows move, Q E turn, R F (or + -) zoom
// glide() flies there smoothly (a station, a tapped spot); tap(x, y) is called for a click that wasn't a drag.
import * as THREE from "three";
import { RADIUS } from "./land.js";

const MIN_DIST = 18, MAX_DIST = 900;

export class Fly {
  constructor(camera, dom, land, { onTap } = {}) {
    Object.assign(this, { camera, dom, land, onTap });
    this.focus = new THREE.Vector3(0, 10, 0);
    this.goal = { focus: this.focus.clone(), yaw: 0.6, pitch: 0.5, dist: 160 };
    this.yaw = this.goal.yaw; this.pitch = this.goal.pitch; this.dist = this.goal.dist;
    this.rate = 4;          // how fast the camera follows its goal; lower while gliding
    this.keys = new Set();
    this.pointers = new Map();
    this.touched = 0;       // when the user last moved the camera themselves (performance.now)
    this.bind();
  }

  bind() {
    const d = this.dom;
    d.addEventListener("contextmenu", (e) => e.preventDefault());
    d.addEventListener("pointerdown", (e) => {
      d.setPointerCapture(e.pointerId);
      this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY, at: performance.now(), pan: e.button === 2 || e.shiftKey });
      this.pinch = null;
    });
    d.addEventListener("pointermove", (e) => {
      const p = this.pointers.get(e.pointerId);
      if (!p) return;
      const dx = e.clientX - p.x, dy = e.clientY - p.y;
      p.x = e.clientX; p.y = e.clientY;
      if (this.pointers.size === 2) return this.twoFingers();
      if (Math.hypot(p.x - p.x0, p.y - p.y0) < 4) return;
      this.user();
      if (p.pan) this.pan(dx, dy);
      else { this.goal.yaw -= dx * 0.005; this.goal.pitch = clampPitch(this.goal.pitch + dy * 0.004); }
    });
    const up = (e) => {
      const p = this.pointers.get(e.pointerId);
      this.pointers.delete(e.pointerId);
      this.pinch = null;
      if (p && e.type === "pointerup" && Math.hypot(e.clientX - p.x0, e.clientY - p.y0) < 6 && performance.now() - p.at < 400) this.onTap?.(e.clientX, e.clientY);
    };
    d.addEventListener("pointerup", up);
    d.addEventListener("pointercancel", up);
    d.addEventListener("wheel", (e) => { e.preventDefault(); this.user(); this.zoom(Math.exp(e.deltaY * 0.0012)); }, { passive: false });
    addEventListener("keydown", (e) => {
      if (e.target.closest?.("input, textarea, select, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if ("wasdqerf+-".includes(k) || k.startsWith("arrow")) { this.keys.add(k); e.preventDefault(); }
    });
    addEventListener("keyup", (e) => this.keys.delete(e.key.toLowerCase()));
    addEventListener("blur", () => this.keys.clear());
  }

  twoFingers() {
    const [a, b] = [...this.pointers.values()], gap = Math.hypot(a.x - b.x, a.y - b.y), mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    if (this.pinch) {
      this.user();
      this.zoom(this.pinch.gap / gap);
      this.pan(mid.x - this.pinch.mid.x, mid.y - this.pinch.mid.y);
    }
    this.pinch = { gap, mid };
  }

  user() { this.touched = performance.now(); this.rate = 4; }

  pan(dx, dy) {
    const k = this.dist * 0.0022, s = Math.sin(this.goal.yaw), c = Math.cos(this.goal.yaw);
    this.goal.focus.x -= (dx * c + dy * s) * k;  // grab the ground: it moves with the pointer
    this.goal.focus.z -= (-dx * s + dy * c) * k;
  }

  zoom(f) { this.goal.dist = Math.min(MAX_DIST, Math.max(MIN_DIST, this.goal.dist * f)); }

  glide(to, { dist, pitch, yaw, rate = 1.4 } = {}) {
    this.goal.focus.copy(to);
    if (dist) this.goal.dist = dist;
    if (pitch != null) this.goal.pitch = pitch;
    if (yaw != null) this.goal.yaw = this.yaw + wrap(yaw - this.yaw);
    this.rate = rate;
  }

  update(dt) {
    const g = this.goal, K = this.keys, move = (this.dist * 0.6 + 40) * dt;
    const fwd = (K.has("w") || K.has("arrowup") ? 1 : 0) - (K.has("s") || K.has("arrowdown") ? 1 : 0);
    const side = (K.has("d") || K.has("arrowright") ? 1 : 0) - (K.has("a") || K.has("arrowleft") ? 1 : 0);
    if (fwd || side) {
      this.user();
      const s = Math.sin(g.yaw), c = Math.cos(g.yaw);
      g.focus.x += (-s * fwd + c * side) * move;
      g.focus.z += (-c * fwd - s * side) * move;
    }
    if (K.has("q")) { this.user(); g.yaw += dt * 1.4; }
    if (K.has("e")) { this.user(); g.yaw -= dt * 1.4; }
    if (K.has("r") || K.has("+")) { this.user(); this.zoom(1 - dt * 1.5); }
    if (K.has("f") || K.has("-")) { this.user(); this.zoom(1 + dt * 1.5); }
    const r = Math.hypot(g.focus.x, g.focus.z), lim = RADIUS + 150;
    if (r > lim) g.focus.multiplyScalar(lim / r);
    g.focus.y = Math.max(this.land.heightAt(g.focus.x, g.focus.z), 0) + 4;  // the focus walks over the ground

    const k = 1 - Math.exp(-dt * this.rate);
    this.focus.lerp(g.focus, k);
    this.yaw += (g.yaw - this.yaw) * k;
    this.pitch += (g.pitch - this.pitch) * k;
    this.dist += (g.dist - this.dist) * k;
    if (this.rate < 4 && this.focus.distanceTo(g.focus) < 2) this.rate = Math.min(4, this.rate + dt);

    const cp = Math.cos(this.pitch), cam = this.camera.position;
    cam.set(this.focus.x + Math.sin(this.yaw) * cp * this.dist, this.focus.y + Math.sin(this.pitch) * this.dist, this.focus.z + Math.cos(this.yaw) * cp * this.dist);
    const floor = Math.max(this.land.heightAt(cam.x, cam.z), 0) + 5;
    if (cam.y < floor) cam.y = floor;
    this.camera.lookAt(this.focus);
  }
}

const clampPitch = (p) => Math.min(1.45, Math.max(0.06, p));
const wrap = (a) => Math.atan2(Math.sin(a), Math.cos(a));
