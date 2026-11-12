// Low-poly things to scatter on the land: each kind is one merged geometry with its colours baked in, drawn as an
// InstancedMesh, so a few thousand trees cost a handful of draw calls.
import * as THREE from "three";

const M = new THREE.Matrix4(), Q = new THREE.Quaternion(), E = new THREE.Euler(), V = new THREE.Vector3(), S = new THREE.Vector3();
const at = (x, y, z, rx = 0, ry = 0, rz = 0, sx = 1, sy = sx, sz = sx) =>
  M.clone().compose(V.set(x, y, z), Q.setFromEuler(E.set(rx, ry, rz)), S.set(sx, sy, sz));

// parts: [geometry, colour, matrix?] → one non-indexed geometry with a colour attribute
export function merge(parts) {
  const pos = [], nor = [], col = [], c = new THREE.Color();
  for (const [geo, colour, matrix] of parts) {
    const g = (geo.index ? geo.toNonIndexed() : geo.clone());
    if (matrix) g.applyMatrix4(matrix);
    g.computeVertexNormals();
    c.set(colour);
    const p = g.attributes.position.array, n = g.attributes.normal.array;
    for (let i = 0; i < p.length; i += 3) { pos.push(p[i], p[i + 1], p[i + 2]); nor.push(n[i], n[i + 1], n[i + 2]); col.push(c.r, c.g, c.b); }
  }
  const out = new THREE.BufferGeometry();
  out.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  out.setAttribute("normal", new THREE.Float32BufferAttribute(nor, 3));
  out.setAttribute("color", new THREE.Float32BufferAttribute(col, 3));
  return out;
}

const cone = (r, h, s = 6) => new THREE.ConeGeometry(r, h, s);
const cyl = (r1, r2, h, s = 6) => new THREE.CylinderGeometry(r1, r2, h, s);
const box = (x, y, z) => new THREE.BoxGeometry(x, y, z);
const ico = (r, d = 0) => new THREE.IcosahedronGeometry(r, d);

// Each kind is built once, about 1 unit = 1 metre, standing on y = 0.
export const KINDS = {
  pine: () => merge([
    [cyl(0.35, 0.45, 2), 0x5a3d2b, at(0, 1, 0)],
    [cone(2.6, 4.2), 0x2f5d3a, at(0, 3.6, 0)], [cone(2.0, 3.4), 0x356b42, at(0, 5.6, 0)], [cone(1.3, 2.6), 0x3d7849, at(0, 7.4, 0)],
  ]),
  snowpine: () => merge([
    [cyl(0.35, 0.45, 2), 0x5a3d2b, at(0, 1, 0)],
    [cone(2.6, 4.2), 0x2c5240, at(0, 3.6, 0)], [cone(2.0, 3.4), 0xe8f1f7, at(0, 5.6, 0)], [cone(1.3, 2.6), 0xf5fafc, at(0, 7.4, 0)],
  ]),
  tree: () => merge([[cyl(0.3, 0.45, 2.6), 0x6b4a32, at(0, 1.3, 0)], [ico(2.4, 0), 0x5e9a46, at(0, 4.2, 0)], [ico(1.6, 0), 0x6faa50, at(0.9, 5.4, 0.4)]]),
  umbrellapine: () => merge([  // the Mediterranean stone pine
    [cyl(0.25, 0.4, 6), 0x7a5536, at(0, 3, 0, 0, 0, 0.12)],
    [new THREE.SphereGeometry(3.4, 7, 4), 0x4f7f3c, at(0.6, 6.6, 0, 0, 0, 0, 1, 0.38, 1)],
  ]),
  rock: () => merge([[new THREE.DodecahedronGeometry(1.4, 0), 0x8a8580, at(0, 0.6, 0, 0.3, 0.5, 0, 1, 0.7, 1)]]),
  redrock: () => merge([[new THREE.DodecahedronGeometry(1.6, 0), 0xb4684a, at(0, 0.7, 0, 0.2, 0.7, 0.1, 1, 0.8, 1.2)]]),
  ice: () => merge([[cone(1.1, 6, 4), 0xbfe6ff, at(0, 3, 0)], [cone(0.7, 3.6, 4), 0xd8f1ff, at(1.2, 1.8, 0.3, 0, 0, -0.35)]]),
  flower: () => merge([[cyl(0.05, 0.05, 0.8, 3), 0x4c7a3a, at(0, 0.4, 0)], [ico(0.32, 0), 0xf3c94b, at(0, 0.9, 0)]]),
  parasol: () => merge([[cyl(0.08, 0.08, 3, 4), 0xeeeeee, at(0, 1.5, 0)], [cone(1.9, 0.8, 8), 0xff6b5a, at(0, 3.1, 0)]]),
  surfboard: () => merge([[new THREE.SphereGeometry(1, 8, 4), 0x46c6e8, at(0, 1.1, 0, 0.12, 0, 0, 0.32, 1.3, 0.08)]]),
  flag: () => merge([[cyl(0.06, 0.06, 2.4, 4), 0xffffff, at(0, 1.2, 0)], [box(0.9, 0.6, 0.04), 0xe84a4a, at(0.46, 2.0, 0)]]),
  blueflag: () => merge([[cyl(0.06, 0.06, 2.4, 4), 0xffffff, at(0, 1.2, 0)], [box(0.9, 0.6, 0.04), 0x3b7be8, at(0.46, 2.0, 0)]]),
  pylon: () => merge([[box(0.5, 12, 0.5), 0x9aa2ad, at(0, 6, 0)], [box(4, 0.4, 0.4), 0x9aa2ad, at(0, 12, 0)]]),
  buoy: () => merge([[cyl(0.6, 0.8, 1.2, 8), 0xff9b2f, at(0, 0.3, 0)], [cone(0.4, 1.2, 8), 0xffffff, at(0, 1.4, 0)]]),
  boat: () => merge([
    [box(1.6, 0.8, 5), 0xf4efe6, at(0, 0.3, 0)], [cyl(0.07, 0.07, 6, 4), 0x8a6a4a, at(0, 3.5, 0.4)],
    [new THREE.ShapeGeometry(new THREE.Shape([new THREE.Vector2(0, 0), new THREE.Vector2(0, 5), new THREE.Vector2(2.6, 0)])), 0xffffff, at(0, 1, 0.5, 0, -Math.PI / 2, 0)],
  ]),
  person: () => merge([[cyl(0.3, 0.35, 1.1, 6), 0xff5a4a, at(0, 1.1, 0)], [ico(0.28, 1), 0xf1c9a5, at(0, 1.9, 0)], [box(0.36, 0.8, 0.36), 0x2b3a55, at(0, 0.4, 0)]]),
  skis: () => merge([[box(0.12, 0.05, 2.2), 0xffd23f, at(-0.18, 0.03, 0)], [box(0.12, 0.05, 2.2), 0xffd23f, at(0.18, 0.03, 0)]]),
  hut: () => merge([[box(5, 3.2, 4), 0x8a5a3a, at(0, 1.6, 0)], [cone(4.2, 2.6, 4), 0xe9f1f6, at(0, 4.4, 0, 0, Math.PI / 4, 0, 1, 1, 0.9)]]),
  lighthouse: () => merge([
    [cyl(1.7, 2.3, 4, 10), 0xf2f2ee, at(0, 2, 0)], [cyl(1.4, 1.7, 4, 10), 0xd8443c, at(0, 6, 0)], [cyl(1.15, 1.4, 4, 10), 0xf2f2ee, at(0, 10, 0)],
    [cyl(1.2, 1.2, 1.6, 10), 0x2b2f36, at(0, 12.8, 0)], [cone(1.5, 1.4, 10), 0xd8443c, at(0, 14.3, 0)],
  ]),
  windmill: () => merge([[cyl(1.1, 1.7, 9, 8), 0xe7dccb, at(0, 4.5, 0)], [cone(1.6, 2, 8), 0x9a4a3a, at(0, 10, 0)]]),
};

const material = new THREE.MeshStandardMaterial({ vertexColors: true, flatShading: true, roughness: 0.9, metalness: 0, side: THREE.DoubleSide });

// A set of instances of one kind. place(x, y, z, yaw, scale) before build(); move(i, ...) to animate one.
export class Scatter {
  constructor(kind, max) {
    this.mesh = new THREE.InstancedMesh(KINDS[kind](), material, max);
    this.mesh.count = 0;
    this.mesh.frustumCulled = false;  // instances are spread over the whole world
  }
  place(x, y, z, yaw = 0, scale = 1, tilt = 0) {
    if (this.mesh.count >= this.mesh.instanceMatrix.count) return -1;
    const i = this.mesh.count++;
    this.move(i, x, y, z, yaw, scale, tilt);
    return i;
  }
  move(i, x, y, z, yaw = 0, scale = 1, tilt = 0, roll = 0) {
    this.mesh.setMatrixAt(i, at(x, y, z, tilt, yaw, roll, scale));
    this.mesh.instanceMatrix.needsUpdate = true;
  }
}
