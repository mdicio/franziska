// Sky, sun, sea and snowfall. The sky follows the real local time (or ?hour=… to try another), the sea moves with
// the same waves the boats ride (waveAt), and snow falls where the land under the camera is snowy.
import * as THREE from "three";
import { smooth, rng } from "./noise.js";

const C = (h) => new THREE.Color(h);
// sky moods by the sun's height: [elevation, zenith, horizon, sun colour, sun strength, ambient strength]
const MOODS = [
  [-0.35, C(0x050914), C(0x0d1630), C(0x7088c0), 0.15, 0.35],
  [-0.08, C(0x14204a), C(0x3a3462), C(0x8a90d0), 0.25, 0.45],
  [0.02, C(0x2c3f78), C(0xff9466), C(0xffb07a), 0.9, 0.6],
  [0.18, C(0x3c78c8), C(0xf5c9a0), C(0xffe0b8), 1.6, 0.8],
  [0.45, C(0x2f74d0), C(0xa9d4ee), C(0xfff6e8), 2.0, 0.8],
];

export function waveAt(x, z, t) {  // must match the water's vertex shader
  return 0.55 * Math.sin(x * 0.045 + t * 0.9) + 0.35 * Math.sin(z * 0.06 - t * 1.1 + x * 0.02) + 0.18 * Math.sin((x + z) * 0.11 + t * 1.7);
}

export class Atmosphere {
  constructor(scene, quality) {
    this.scene = scene;
    this.sunDir = new THREE.Vector3();
    this.sun = new THREE.DirectionalLight(0xffffff, 2);
    this.hemi = new THREE.HemisphereLight(0xbfdfff, 0x5a5040, 1);
    scene.add(this.sun, this.sun.target, this.hemi);
    scene.fog = new THREE.FogExp2(0xbfe1f2, 0.0006);

    this.skyU = { zenith: { value: C(0) }, horizon: { value: C(0) }, sunDir: { value: this.sunDir }, sunColor: { value: C(0) }, night: { value: 0 } };
    const sky = new THREE.Mesh(new THREE.SphereGeometry(2800, 32, 16), new THREE.ShaderMaterial({
      uniforms: this.skyU, side: THREE.BackSide, depthWrite: false, fog: false,
      vertexShader: `varying vec3 vDir; void main(){ vDir = normalize(position); gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.); }`,
      fragmentShader: `uniform vec3 zenith, horizon, sunColor, sunDir; uniform float night; varying vec3 vDir;
        void main(){ float y = max(vDir.y, 0.); vec3 c = mix(horizon, zenith, pow(y, .55));
          float s = max(dot(normalize(vDir), normalize(sunDir)), 0.);
          c += sunColor * (pow(s, 900.) * 6. + pow(s, 12.) * .35) * (1. - night * .8);
          c = mix(c, horizon * .6, smoothstep(0., -.25, vDir.y));
          gl_FragColor = vec4(c, 1.); }`,
    }));
    sky.renderOrder = -10;
    this.sky = sky;
    scene.add(sky);

    // stars
    const r = rng(3), star = new Float32Array(1500 * 3);
    for (let i = 0; i < 1500; i++) {
      const u = r() * 2 - 1, a = r() * Math.PI * 2, s = Math.sqrt(1 - u * u);
      star.set([Math.cos(a) * s * 2600, Math.abs(u) * 2600, Math.sin(a) * s * 2600], i * 3);
    }
    const sg = new THREE.BufferGeometry();
    sg.setAttribute("position", new THREE.BufferAttribute(star, 3));
    this.stars = new THREE.Points(sg, new THREE.PointsMaterial({ color: 0xffffff, size: 2.2, sizeAttenuation: false, transparent: true, opacity: 0, fog: false, depthWrite: false }));
    scene.add(this.stars);

    // the sea
    this.waterU = THREE.UniformsUtils.merge([THREE.UniformsLib.fog, {
      time: { value: 0 }, deep: { value: C(0x0b4a6e) }, shallow: { value: C(0x2fa7b8) }, sunDir: { value: this.sunDir },
      sunColor: { value: C(0xffffff) }, skyColor: { value: C(0xbfe1f2) },
    }]);
    const seg = quality === "low" ? 120 : 220;
    const water = new THREE.Mesh(new THREE.PlaneGeometry(3600, 3600, seg, seg).rotateX(-Math.PI / 2), new THREE.ShaderMaterial({
      uniforms: this.waterU, transparent: true, fog: true,
      vertexShader: `uniform float time; varying vec3 vPos; varying vec3 vNormal; varying float vCrest;
        #include <fog_pars_vertex>
        void main(){ vec3 p = (modelMatrix * vec4(position, 1.)).xyz; float x = p.x, z = p.z, t = time;
          float y = .55*sin(x*.045+t*.9) + .35*sin(z*.06-t*1.1+x*.02) + .18*sin((x+z)*.11+t*1.7);
          float dx = .55*.045*cos(x*.045+t*.9) + .35*.02*cos(z*.06-t*1.1+x*.02) + .18*.11*cos((x+z)*.11+t*1.7);
          float dz = .35*.06*cos(z*.06-t*1.1+x*.02) + .18*.11*cos((x+z)*.11+t*1.7);
          p.y = y; vCrest = y; vNormal = normalize(vec3(-dx, 1., -dz));
          vec4 world = vec4(p, 1.); vPos = world.xyz;
          vec4 mvPosition = viewMatrix * world; gl_Position = projectionMatrix * mvPosition;
          #include <fog_vertex>
        }`,
      fragmentShader: `uniform vec3 deep, shallow, sunDir, sunColor, skyColor; varying vec3 vPos; varying vec3 vNormal; varying float vCrest;
        #include <fog_pars_fragment>
        void main(){ vec3 v = normalize(cameraPosition - vPos), n = normalize(vNormal);
          float fres = pow(1. - max(dot(v, n), 0.), 3.);
          vec3 c = mix(deep, shallow, .35 + .3 * n.x);
          c = mix(c, skyColor, fres * .65);
          vec3 h = normalize(normalize(sunDir) + v);
          c += sunColor * pow(max(dot(n, h), 0.), 180.) * 2.2;
          c += vec3(.85) * smoothstep(.7, 1.05, vCrest) * .35;
          gl_FragColor = vec4(c, .86);
          #include <fog_fragment>
        }`,
    }));
    water.name = "water";
    this.water = water;
    scene.add(water);

    // snow: flakes in a box that follows the camera, moved in the shader
    const N = quality === "low" ? 900 : 2600, B = 160, flakes = new Float32Array(N * 3);
    for (let i = 0; i < N; i++) flakes.set([r() * B, r() * B * 0.6, r() * B], i * 3);
    const fg = new THREE.BufferGeometry();
    fg.setAttribute("position", new THREE.BufferAttribute(flakes, 3));
    this.snowU = { time: { value: 0 }, center: { value: new THREE.Vector3() }, amount: { value: 0 }, box: { value: new THREE.Vector3(B, B * 0.6, B) } };
    this.snow = new THREE.Points(fg, new THREE.ShaderMaterial({
      uniforms: this.snowU, transparent: true, depthWrite: false,
      vertexShader: `uniform float time; uniform vec3 center, box; varying float vA;
        void main(){ vec3 p = position + vec3(sin(time*.4 + position.y)*3., -time*7., cos(time*.3 + position.x)*3.);
          p = mod(p - center + box*.5, box) + center - box*.5;
          vec4 mv = viewMatrix * vec4(p, 1.); gl_Position = projectionMatrix * mv;
          gl_PointSize = clamp(260. / -mv.z, 1., 9.); vA = smoothstep(150., 20., -mv.z); }`,
      fragmentShader: `uniform float amount; varying float vA;
        void main(){ float d = length(gl_PointCoord - .5); if (d > .5) discard; gl_FragColor = vec4(1., 1., 1., (1. - d*2.) * amount * vA * .9); }`,
    }));
    this.snow.frustumCulled = false;
    scene.add(this.snow);
    this.snowAmount = 0;
  }

  // hour: 0..24, local time
  setHour(hour) {
    const a = (hour - 6) / 24 * Math.PI * 2;  // sunrise about 6, sunset about 18
    const elev = Math.sin(a) * 0.75;
    this.sunDir.set(Math.cos(a) * 0.8, elev, -0.45).normalize();
    let i = 0;
    while (i < MOODS.length - 2 && elev > MOODS[i + 1][0]) i++;
    const [e0, z0, h0, s0, k0, m0] = MOODS[i], [e1, z1, h1, s1, k1, m1] = MOODS[i + 1];
    const t = smooth(e0, e1, elev);
    this.skyU.zenith.value.copy(z0).lerp(z1, t);
    this.skyU.horizon.value.copy(h0).lerp(h1, t);
    this.skyU.sunColor.value.copy(s0).lerp(s1, t);
    const night = 1 - smooth(-0.2, 0.05, elev);
    this.skyU.night.value = night;
    this.night = night;
    this.sun.color.copy(this.skyU.sunColor.value);
    this.sun.intensity = k0 + (k1 - k0) * t;
    this.hemi.intensity = m0 + (m1 - m0) * t;
    this.hemi.color.copy(this.skyU.zenith.value).lerp(C(0xffffff), 0.5);
    this.scene.fog.color.copy(this.skyU.horizon.value);
    this.stars.material.opacity = night;
    this.waterU.skyColor.value.copy(this.skyU.horizon.value);
    this.waterU.sunColor.value.copy(this.skyU.sunColor.value).multiplyScalar(1 - night * 0.7);
    this.waterU.deep.value.set(0x0b4a6e).lerp(C(0x04121e), night);
    this.waterU.shallow.value.set(0x2fa7b8).lerp(C(0x0e2c3c), night);
  }

  update(t, camera, focus, snowiness) {
    this.waterU.time.value = t;
    this.snowU.time.value = t;
    this.snowU.center.value.copy(camera.position);
    this.snowAmount += (snowiness - this.snowAmount) * 0.02;
    this.snowU.amount.value = this.snowAmount;
    this.sky.position.copy(camera.position);
    this.stars.position.copy(camera.position);
    this.water.position.x = Math.round(camera.position.x / 200) * 200;  // the sea follows the camera; waves are in world space
    this.water.position.z = Math.round(camera.position.z / 200) * 200;
    this.sun.position.copy(focus).addScaledVector(this.sunDir, 400);
    this.sun.target.position.copy(focus);
  }
}
