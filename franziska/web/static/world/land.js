// The ground: one continuous landscape made from the sites (Franziska's commons and one site per team member).
// Each point belongs mostly to its nearest site, with soft borders, so N members give N regions that flow into
// each other, and the whole thing is an island in the open sea.
import * as THREE from "three";
import { simplex, fbm, ridged, rng, smooth, lerp } from "./noise.js";
import { Scatter } from "./props.js";

export const RADIUS = 620;  // past this the land falls away into the sea
const SOFT = 22;            // how wide the blend between two regions is (world units)
const SEABED = -45;

export class Land {
  // sites: [{ x, z, reach, biome }]
  constructor(sites, seed = 7) {
    this.sites = sites;
    const s1 = simplex(seed), s2 = simplex(seed + 1);
    this.n = { fbm: (x, y) => fbm(s1, x, y, 4), ridged: (x, y) => ridged(s2, x, y, 4), raw: s1 };
    this.w = new Float32Array(sites.length);
    this.d = new Float32Array(sites.length);
  }

  // the share of each site at a point, summing to 1 (this.w); returns the index of the strongest
  weigh(x, z) {
    let best = 0, emin = Infinity;
    const { sites, d, w } = this;
    for (let i = 0; i < sites.length; i++) {
      d[i] = Math.hypot(x - sites[i].x, z - sites[i].z);
      const e = d[i] / sites[i].reach;
      w[i] = e;
      if (e < emin) { emin = e; best = i; }
    }
    let sum = 0;
    for (let i = 0; i < sites.length; i++) { const k = w[i] - emin; w[i] = k > SOFT * 5 ? 0 : Math.exp(-k / SOFT); sum += w[i]; }
    for (let i = 0; i < sites.length; i++) w[i] /= sum;
    return best;
  }

  heightAt(x, z) {
    this.weigh(x, z);
    let h = 0;
    for (let i = 0; i < this.sites.length; i++) if (this.w[i] > 1e-4) h += this.w[i] * this.sites[i].biome.height(x, z, this.d[i], this.n, this.sites[i]);
    return lerp(h, SEABED, smooth(RADIUS - 110, RADIUS + 60, Math.hypot(x, z)));
  }

  gradient(x, z, e = 2) {
    return [(this.heightAt(x + e, z) - this.heightAt(x - e, z)) / (2 * e), (this.heightAt(x, z + e) - this.heightAt(x, z - e)) / (2 * e)];
  }

  // the site whose region a point is in
  siteAt(x, z) { return this.weigh(x, z); }

  // how much of something (a biome property) there is at a point, blended like the ground
  blend(x, z, key) {
    this.weigh(x, z);
    let v = 0;
    for (let i = 0; i < this.sites.length; i++) v += this.w[i] * (this.sites[i].biome[key] || 0);
    return v;
  }

  mesh(segments) {
    const size = (RADIUS + 120) * 2, geo = new THREE.PlaneGeometry(size, size, segments, segments).rotateX(-Math.PI / 2);
    const pos = geo.attributes.position, n = pos.count, row = segments + 1, step = size / segments;
    const heights = new Float32Array(n);
    for (let i = 0; i < n; i++) heights[i] = this.heightAt(pos.getX(i), pos.getZ(i));
    const colors = new Float32Array(n * 3), c = new THREE.Color(), mix = new THREE.Color();
    for (let i = 0; i < n; i++) {
      const x = pos.getX(i), z = pos.getZ(i), h = heights[i], col = i % row, r = (i / row) | 0;
      const hx = heights[r * row + Math.min(col + 1, segments)] - heights[r * row + Math.max(col - 1, 0)];
      const hz = heights[Math.min(r + 1, segments) * row + col] - heights[Math.max(r - 1, 0) * row + col];
      const g = Math.hypot(hx, hz) / (2 * step), nv = this.n.raw(x / 9, z / 9);
      pos.setY(i, h);
      this.weigh(x, z);
      c.setRGB(0, 0, 0);
      for (let k = 0; k < this.sites.length; k++) {
        if (this.w[k] < 1e-3) continue;
        paint(this.sites[k].biome.paint, h, g, nv, mix);
        c.r += mix.r * this.w[k]; c.g += mix.g * this.w[k]; c.b += mix.b * this.w[k];
      }
      c.toArray(colors, i * 3);
    }
    geo.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    geo.computeVertexNormals();
    const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ vertexColors: true, flatShading: true, roughness: 0.95, metalness: 0 }));
    mesh.name = "land";
    return mesh;
  }

  // Trees, rocks and the rest, from each biome's props list; `amount` scales the density (quality).
  scatter(amount, seed = 11) {
    const r = rng(seed), groups = {}, meshes = [];
    this.sites.forEach((site, si) => {
      const span = 150 * site.reach;
      for (const spec of site.biome.props || []) {
        const tries = Math.round(spec.density * Math.PI * span * span / 1000 * amount);
        const set = groups[spec.kind] ||= { list: [] };
        for (let k = 0; k < tries; k++) {
          const a = r() * Math.PI * 2, d = Math.sqrt(r()) * Math.min(span, spec.far || span);
          const x = site.x + Math.cos(a) * d, z = site.z + Math.sin(a) * d;
          if (d < 14 || this.siteAt(x, z) !== si) continue;  // keep the station's pad clear, and props in their region
          const h = this.heightAt(x, z);
          if (h < (spec.min ?? 2) || h > (spec.max ?? 999)) continue;
          const [gx, gz] = this.gradient(x, z);
          if (Math.hypot(gx, gz) > (spec.slope ?? 1)) continue;
          set.list.push([x, h - 0.4, z, r() * Math.PI * 2, 0.75 + r() * 0.6]);
        }
      }
    });
    for (const [kind, { list }] of Object.entries(groups)) {
      if (!list.length) continue;
      const s = new Scatter(kind, list.length);
      for (const p of list) s.place(...p);
      meshes.push(s.mesh);
    }
    return meshes;
  }
}

const DEEP = new THREE.Color(0x24505a), tmp = new THREE.Color();
// a biome's colour for this height (h), steepness (g = rise over run) and a bit of noise (nv, -1..1)
function paint(p, h, g, nv, out) {
  if (h < 0) return out.set(p.sand).lerp(DEEP, smooth(0, 22, -h));
  if (h < p.sandline + nv * 1.2) out.set(p.sand);
  else out.set(p.grass).lerp(tmp.set(p.grass2), nv * 0.5 + 0.5);
  out.lerp(tmp.set(p.rock), smooth(0.75, 1.3, g));
  if (p.snow) out.lerp(tmp.set(p.snow), smooth(p.snowline - 5, p.snowline + 6, h + nv * 9) * (1 - smooth(1.2, 1.8, g)));
  return out;
}
