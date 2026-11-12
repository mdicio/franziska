// Franziska and her team as things in the world: her core and rings at the centre, a station per member in its
// region, and a link of light between them that carries pulses while a member works on a message.
import * as THREE from "three";

const ADD = { transparent: true, blending: THREE.AdditiveBlending, depthWrite: false };
const bright = (hex, k) => new THREE.Color(hex).multiplyScalar(k);  // over 1 so the bloom picks it up

let glowTex;
export function glow() {  // a soft round light, drawn once
  if (glowTex) return glowTex;
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const g = c.getContext("2d"), r = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  r.addColorStop(0, "rgba(255,255,255,1)"); r.addColorStop(0.25, "rgba(255,255,255,.45)"); r.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = r; g.fillRect(0, 0, 128, 128);
  glowTex = new THREE.CanvasTexture(c);
  glowTex.colorSpace = THREE.SRGBColorSpace;
  return glowTex;
}
const sprite = (color, size, opacity = 1) => {
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: glow(), color, opacity, ...ADD }));
  s.scale.setScalar(size);
  return s;
};

// what the lead looks like in each state: ring speed, core glow, tint
const MOODS = { idle: [1, 1], routing: [4.5, 1.7], working: [2.6, 1.4], asleep: [0.12, 0.3] };

export class Lead {
  constructor(color, ground) {
    this.color = new THREE.Color(color);
    this.group = new THREE.Group();
    this.group.position.set(0, ground, 0);
    this.height = 24;
    this.spin = 1; this.power = 1; this.tint = this.color.clone();
    this.target = { mood: "idle", tint: this.color.clone() };

    this.coreU = { time: { value: 0 }, color: { value: this.tint }, power: { value: 1 } };
    const core = new THREE.Mesh(new THREE.IcosahedronGeometry(4.6, 4), new THREE.ShaderMaterial({
      uniforms: this.coreU,
      vertexShader: `varying vec3 vN; varying vec3 vV; varying vec3 vP; uniform float time;
        void main(){ vec3 p = position * (1. + .04 * sin(time * 2. + position.y * 1.3) + .03 * sin(time * 3.1 + position.x * 2.));
          vN = normalize(normalMatrix * normal); vec4 mv = modelViewMatrix * vec4(p, 1.); vV = normalize(-mv.xyz); vP = position;
          gl_Position = projectionMatrix * mv; }`,
      fragmentShader: `uniform vec3 color; uniform float power, time; varying vec3 vN; varying vec3 vV; varying vec3 vP;
        void main(){ float f = pow(1. - max(dot(vN, vV), 0.), 2.2);
          float bands = .5 + .5 * sin(vP.y * 3. - time * 2.5) * sin(vP.x * 2.2 + time * 1.3);
          vec3 c = color * (.35 + bands * .4 + f * 2.4) * power + vec3(1.) * pow(f, 3.) * power * .6;
          gl_FragColor = vec4(c, 1.); }`,
    }));
    core.position.y = this.height;
    this.core = core;
    this.halo = sprite(this.tint, 46, 0.55);
    this.halo.position.y = this.height;

    this.rings = [[10, 0.2, 0.9, 0.3], [14, 0.14, -0.6, 1.2], [18.5, 0.1, 0.4, -0.7]].map(([r, w, speed, tilt]) => {
      const m = new THREE.Mesh(new THREE.TorusGeometry(r, w, 8, 160), new THREE.MeshBasicMaterial({ color: bright(color, 2.2), ...ADD }));
      m.position.y = this.height;
      m.rotation.x = Math.PI / 2 + tilt * 0.4;
      m.userData = { speed, tilt };
      return m;
    });
    // the data ring: dashes going round the core
    const dashes = new THREE.InstancedMesh(new THREE.BoxGeometry(1.6, 0.18, 0.5), new THREE.MeshBasicMaterial({ color: bright(color, 1.6), ...ADD }), 72);
    const M = new THREE.Matrix4(), Q = new THREE.Quaternion(), Y = new THREE.Vector3(0, 1, 0);
    for (let i = 0; i < 72; i++) {
      const a = i / 72 * Math.PI * 2, r = 24 + (i % 3 === 0 ? 0.8 : 0);
      M.compose(new THREE.Vector3(Math.cos(a) * r, 0, Math.sin(a) * r), Q.setFromAxisAngle(Y, -a + Math.PI / 2), new THREE.Vector3(i % 4 ? 1 : 2.2, 1, 1));
      dashes.setMatrixAt(i, M);
    }
    dashes.position.y = this.height;
    this.dashes = dashes;
    // the pad on the ground and a beam up to the core
    const pad = new THREE.Mesh(new THREE.RingGeometry(26, 27.2, 6, 1).rotateX(-Math.PI / 2), new THREE.MeshBasicMaterial({ color: bright(color, 1.4), ...ADD, side: THREE.DoubleSide }));
    pad.position.y = 0.6;
    const pad2 = new THREE.Mesh(new THREE.RingGeometry(8, 8.6, 48, 1).rotateX(-Math.PI / 2), pad.material);
    pad2.position.y = 0.6;
    const beam = new THREE.Mesh(new THREE.CylinderGeometry(1.1, 2.4, this.height, 16, 1, true), new THREE.MeshBasicMaterial({ color, opacity: 0.18, ...ADD, side: THREE.DoubleSide }));
    beam.position.y = this.height / 2;
    this.beam = beam;
    this.group.add(core, this.halo, ...this.rings, dashes, pad, pad2, beam);
  }

  get anchor() { return new THREE.Vector3(0, this.group.position.y + this.height, 0); }

  set(mood, tint) { this.target = { mood, tint: new THREE.Color(tint ?? this.color) }; }

  update(t, dt) {
    const [spin, power] = MOODS[this.target.mood] || MOODS.idle, k = 1 - Math.exp(-dt * 2.5);
    this.spin += (spin - this.spin) * k;
    this.power += (power - this.power) * k;
    this.tint.lerp(this.target.tint, k);
    this.coreU.time.value = t;
    this.coreU.power.value = this.power * (1 + 0.06 * Math.sin(t * 1.6));
    this.halo.material.color.copy(this.tint);
    this.halo.material.opacity = 0.35 + 0.25 * this.power;
    this.core.position.y = this.halo.position.y = this.height + Math.sin(t * 0.8) * 0.6;
    this.rings.forEach((r, i) => {
      r.rotation.z += dt * r.userData.speed * this.spin;
      r.rotation.x = Math.PI / 2 + Math.sin(t * 0.3 + i) * 0.35 * r.userData.tilt;
      r.material.color.copy(this.tint).multiplyScalar(1.2 + this.power);
    });
    this.dashes.rotation.y -= dt * 0.15 * this.spin;
    this.dashes.material.color.copy(this.tint).multiplyScalar(0.6 + this.power);
    this.beam.material.opacity = 0.08 + 0.1 * this.power;
  }
}

export class Station {
  // member: { key, label, kind, about, turns }; at: Vector3 on the ground
  constructor(member, at, color) {
    this.member = member;
    this.color = new THREE.Color(color);
    const small = member.kind === "workflow", s = small ? 0.7 : 1;
    this.group = new THREE.Group();
    this.group.position.copy(at);
    this.lift = 10 * s;
    this.level = 0; this.want = 0;

    const stone = new THREE.MeshStandardMaterial({ color: 0x3a3f48, flatShading: true, roughness: 0.7, metalness: 0.2 });
    const pad = new THREE.Mesh(new THREE.CylinderGeometry(7 * s, 8 * s, 1.6, 6), stone);
    pad.position.y = 0.4;
    this.edge = new THREE.Mesh(new THREE.TorusGeometry(7.2 * s, 0.16, 6, 6), new THREE.MeshBasicMaterial({ color: bright(color, 1.5), ...ADD }));
    this.edge.rotation.x = Math.PI / 2;
    this.edge.position.y = 1.25;
    const geo = small ? new THREE.IcosahedronGeometry(2.2, 0) : new THREE.OctahedronGeometry(2.8, 0);
    if (!small) geo.scale(1, 1.7, 1);
    this.crystal = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({
      color, emissive: color, emissiveIntensity: 0.7, flatShading: true, roughness: 0.25, metalness: 0.3,
    }));
    this.crystal.position.y = this.lift;
    this.halo = sprite(color, 22 * s, 0.4);
    this.halo.position.y = this.lift;
    // the pillar: taller for a member that has been busy this week
    const height = (12 + 9 * Math.log2(1 + (member.turns || 0))) * s;
    this.pillar = new THREE.Mesh(new THREE.CylinderGeometry(0.5 * s, 0.9 * s, height, 12, 1, true), new THREE.MeshBasicMaterial({ color, opacity: 0.16, ...ADD, side: THREE.DoubleSide }));
    this.pillar.position.y = height / 2 + 1;
    // a big invisible ball to tap
    this.hit = new THREE.Mesh(new THREE.SphereGeometry(13 * s, 8, 6), new THREE.MeshBasicMaterial({ visible: false }));
    this.hit.position.y = this.lift;
    this.hit.userData.station = this;
    this.group.add(pad, this.edge, this.crystal, this.halo, this.pillar, this.hit);
  }

  get anchor() { return this.group.position.clone().add(new THREE.Vector3(0, this.lift, 0)); }

  update(t, dt) {
    this.level += (this.want - this.level) * (1 - Math.exp(-dt * 3));
    const L = this.level, bob = Math.sin(t * 1.1 + this.group.position.x) * 0.6;
    this.crystal.rotation.y += dt * (0.4 + L * 3.5);
    this.crystal.position.y = this.halo.position.y = this.lift + bob + L * 2;
    this.crystal.material.emissiveIntensity = 0.6 + L * 2.2 + 0.15 * Math.sin(t * 2);
    this.halo.material.opacity = 0.3 + L * 0.6;
    this.halo.scale.setScalar((this.member.kind === "workflow" ? 15 : 22) * (1 + L * 0.5));
    this.pillar.material.opacity = 0.12 + L * 0.35;
    this.edge.material.color.copy(this.color).multiplyScalar(1.2 + L * 2.5);
    this.edge.rotation.z += dt * (0.1 + L);
  }
}

// The lines of light from the lead to each member, and the pulses running along them.
export class Links {
  constructor(scene) {
    this.scene = scene;
    this.links = new Map();
    this.pulses = [];
  }

  add(key, from, to, color) {
    const mid = from.clone().lerp(to, 0.5);
    mid.y = Math.max(from.y, to.y) + from.distanceTo(to) * 0.28;
    const curve = new THREE.QuadraticBezierCurve3(from, mid, to);
    const tube = new THREE.Mesh(new THREE.TubeGeometry(curve, 64, 0.22, 5, false), new THREE.MeshBasicMaterial({ color: bright(color, 1.4), opacity: 0.12, ...ADD }));
    this.scene.add(tube);
    this.links.set(key, { curve, tube, color: new THREE.Color(color), level: 0, want: 0, every: 0 });
  }

  // a member is working (on) or done (off); unknown keys (the cloud) send pulses up into the sky
  active(key, on) { const l = this.links.get(key); if (l) l.want = on ? 1 : 0; }

  burst(key, back = true, n = 6) {
    const l = this.links.get(key);
    for (let i = 0; i < n; i++) this.pulse(l?.curve, l?.color || new THREE.Color(0xffffff), back, -i * 0.07);
  }

  pulse(curve, color, back, delay = 0) {
    if (!curve) {  // to the sky
      const from = new THREE.Vector3(0, 30, 0);
      curve = new THREE.LineCurve3(back ? new THREE.Vector3(0, 420, 0) : from, back ? from : new THREE.Vector3(0, 420, 0));
      back = false;
    }
    const s = sprite(color.clone().multiplyScalar(2), 5, 1);
    s.visible = false;
    this.scene.add(s);
    this.pulses.push({ s, curve, back, u: delay, speed: 0.55 + Math.random() * 0.2 });
  }

  update(t, dt) {
    for (const l of this.links.values()) {
      l.level += (l.want - l.level) * (1 - Math.exp(-dt * 3));
      l.tube.material.opacity = 0.1 + l.level * 0.45;
      if (l.want && (l.every -= dt) <= 0) { l.every = 0.28; this.pulse(l.curve, l.color, false); }
    }
    this.pulses = this.pulses.filter((p) => {
      p.u += dt * p.speed;
      if (p.u >= 1) { this.scene.remove(p.s); p.s.material.dispose(); return false; }
      if (p.u >= 0) { p.s.visible = true; p.curve.getPointAt(p.back ? 1 - p.u : p.u, p.s.position); p.s.material.opacity = Math.sin(p.u * Math.PI); }
      return true;
    });
  }
}
