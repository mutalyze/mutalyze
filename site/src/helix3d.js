// Full-page 3D DNA helix for the site background.
// three.js scene with cinematic bloom + a floating particle field; the helix is
// tall and you travel down it as you scroll, so the strand runs the whole page.
// anime.js drives the per-node "mutation flare" (a node swells + turns red).
import * as THREE from 'three';
import { animate } from 'animejs';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';

const GREEN = new THREE.Color('#63f7a6');
const RED   = new THREE.Color('#ff5540');
const HELD  = new THREE.Color('#c4ffde');

function makeGlowTexture() {
  const c = document.createElement('canvas');
  c.width = c.height = 64;
  const ctx = c.getContext('2d');
  const g = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
  g.addColorStop(0.0, 'rgba(255,255,255,1)');
  g.addColorStop(0.25, 'rgba(255,255,255,0.55)');
  g.addColorStop(1.0, 'rgba(255,255,255,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 64, 64);
  const tex = new THREE.Texture(c);
  tex.needsUpdate = true;
  return tex;
}

export function initHelix(container, { reduced = false } = {}) {
  let w = container.clientWidth || 1;
  let h = container.clientHeight || 1;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(46, w / h, 0.1, 200);
  camera.position.set(0, 0, 8.6);

  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(w, h);
  renderer.setClearColor(0x05070b, 1);   // opaque — this canvas IS the page background
  container.appendChild(renderer.domElement);

  // ── cinematic bloom pipeline ───────────────────────────────────────
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(w, h), 0.32, 0.4, 0.22); // strength, radius, threshold — subtle
  composer.addPass(bloom);
  composer.addPass(new OutputPass());

  const group = new THREE.Group();
  group.position.x = 2.0;   // helix runs down the right side
  scene.add(group);

  // ── helix geometry (tall — spans the full scroll) ─────────────────
  const glowTex = makeGlowTexture();
  const NODES = 74, R = 1.25, TURN = 0.5, HEIGHT = 20;
  const GLOW_BASE = 0.34;
  const sphereGeo = new THREE.SphereGeometry(0.1, 16, 16);
  const nodes = [];

  function addNode(x, y, z) {
    const mat = new THREE.MeshBasicMaterial({ color: GREEN.clone() });
    const sph = new THREE.Mesh(sphereGeo, mat);
    sph.position.set(x, y, z);
    group.add(sph);
    const sprMat = new THREE.SpriteMaterial({
      map: glowTex, color: GREEN.clone(), blending: THREE.AdditiveBlending,
      transparent: true, depthWrite: false, opacity: 0.4,
    });
    const spr = new THREE.Sprite(sprMat);
    spr.scale.setScalar(GLOW_BASE);
    spr.position.set(x, y, z);
    group.add(spr);
    nodes.push({ sph, spr, mat, sprMat, ly: y });
  }

  const strandA = [], strandB = [];
  for (let i = 0; i < NODES; i++) {
    const a = i * TURN;
    const y = (i / (NODES - 1) - 0.5) * HEIGHT;
    const ax = Math.cos(a) * R,           az = Math.sin(a) * R;
    const bx = Math.cos(a + Math.PI) * R, bz = Math.sin(a + Math.PI) * R;
    addNode(ax, y, az);
    addNode(bx, y, bz);
    strandA.push(new THREE.Vector3(ax, y, az));
    strandB.push(new THREE.Vector3(bx, y, bz));
  }
  // thick base-pair rungs
  const rungMat = new THREE.MeshBasicMaterial({ color: GREEN, transparent: true, opacity: 0.4 });
  const Y_AXIS = new THREE.Vector3(0, 1, 0);
  for (let i = 0; i < NODES; i++) {
    const a = strandA[i], b = strandB[i];
    const dir = new THREE.Vector3().subVectors(b, a);
    const rung = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.05, dir.length(), 10), rungMat);
    rung.position.copy(a).add(b).multiplyScalar(0.5);
    rung.quaternion.setFromUnitVectors(Y_AXIS, dir.normalize());
    group.add(rung);
  }
  // thick glowing backbones
  const backboneMat = new THREE.MeshBasicMaterial({ color: GREEN, transparent: true, opacity: 0.6 });
  for (const pts of [strandA, strandB]) {
    const curve = new THREE.CatmullRomCurve3(pts);
    const geo = new THREE.TubeGeometry(curve, NODES * 8, 0.12, 14, false);
    group.add(new THREE.Mesh(geo, backboneMat));
  }

  // ── floating particle field (depth / atmosphere) ──────────────────
  const P = 340;
  const pPos = new Float32Array(P * 3);
  for (let i = 0; i < P; i++) {
    pPos[i * 3]     = -3 + Math.random() * 7;
    pPos[i * 3 + 1] = (Math.random() - 0.5) * HEIGHT * 1.05;
    pPos[i * 3 + 2] = -4 + Math.random() * 6;
  }
  const pGeo = new THREE.BufferGeometry();
  pGeo.setAttribute('position', new THREE.BufferAttribute(pPos, 3));
  const particles = new THREE.Points(pGeo, new THREE.PointsMaterial({
    color: GREEN, size: 0.03, transparent: true, opacity: 0.3,
    blending: THREE.AdditiveBlending, depthWrite: false, sizeAttenuation: true,
  }));
  group.add(particles);

  // ── interaction state ─────────────────────────────────────────────
  let targetTiltX = 0, targetTiltY = 0, scrollFrac = 0;
  const onPointer = (e) => {
    targetTiltY = ((e.clientX / window.innerWidth) * 2 - 1) * 0.4;
    targetTiltX = ((e.clientY / window.innerHeight) * 2 - 1) * 0.28;
  };
  const onScrollEv = () => {
    const max = document.documentElement.scrollHeight - window.innerHeight;
    scrollFrac = max > 0 ? Math.min(1, Math.max(0, window.scrollY / max)) : 0;
  };
  if (!reduced) {
    window.addEventListener('pointermove', onPointer, { passive: true });
    window.addEventListener('scroll', onScrollEv, { passive: true });
    onScrollEv();
  }

  const clock = new THREE.Clock();
  let raf = 0, running = false;
  function tick() {
    const dt = Math.min(clock.getDelta(), 0.05);
    group.rotation.y += dt * 0.28;
    group.rotation.x += (targetTiltX - group.rotation.x) * 0.05;
    const targetY = (scrollFrac - 0.5) * (HEIGHT - 6);   // travel down the strand
    group.position.y += (targetY - group.position.y) * 0.08;
    camera.position.x += (targetTiltY * 0.5 - camera.position.x) * 0.05;
    camera.lookAt(0, 0, 0);
    composer.render();
    raf = requestAnimationFrame(tick);
  }

  // calm ambient red flares, plus whatever the console cycle triggers
  let ambientTO = 0;
  function scheduleAmbient() {
    const delay = 1000 + Math.random() * 400;   // steady ~1.0–1.4s between flares
    ambientTO = setTimeout(() => { flare(true); scheduleAmbient(); }, delay);
  }
  function start() {
    if (running || reduced) return;
    running = true; clock.start(); tick(); scheduleAmbient();
  }
  function stop() {
    if (!running) return;
    running = false; cancelAnimationFrame(raf); clearTimeout(ambientTO);
  }

  const io = new IntersectionObserver(([e]) => (e.isIntersecting ? start() : stop()), { threshold: 0 });
  io.observe(container);

  function resize() {
    w = container.clientWidth || 1;
    h = container.clientHeight || 1;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
    composer.setSize(w, h);
    bloom.setSize(w, h);
    if (reduced) composer.render();
  }
  const ro = new ResizeObserver(resize);
  ro.observe(container);

  // ── mutation flare (prefer a node currently in view) ──────────────
  function flare(broke) {
    const viewLy = -group.position.y;
    const visible = nodes.filter((n) => Math.abs(n.ly - viewLy) < 3.4);
    const pool = visible.length ? visible : nodes;
    const node = pool[(Math.random() * pool.length) | 0];
    const target = broke ? RED : HELD;
    const peak = broke ? 2.8 : 1.9;
    const proxy = { t: 0 };
    animate(proxy, {
      t: [0, 1], duration: broke ? 900 : 700, ease: 'inOutQuad',
      onUpdate: () => {
        const k = Math.sin(proxy.t * Math.PI);
        node.mat.color.copy(GREEN).lerp(target, k);
        node.sprMat.color.copy(node.mat.color);
        node.sph.scale.setScalar(1 + k * (peak - 1));
        node.spr.scale.setScalar(GLOW_BASE * (1 + k * 2.4));
        node.sprMat.opacity = 0.4 + k * 0.45;
      },
      onComplete: () => {
        node.mat.color.copy(GREEN);
        node.sprMat.color.copy(GREEN);
        node.sph.scale.setScalar(1);
        node.spr.scale.setScalar(GLOW_BASE);
        node.sprMat.opacity = 0.4;
      },
    });
  }

  if (reduced) { group.rotation.set(-0.1, 0.4, 0); composer.render(); }
  else start();

  return {
    flare,
    dispose() {
      stop(); io.disconnect(); ro.disconnect();
      window.removeEventListener('pointermove', onPointer);
      window.removeEventListener('scroll', onScrollEv);
      composer.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}
