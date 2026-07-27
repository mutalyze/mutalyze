// 3D DNA double-helix for the hero — three.js scene, anime.js-driven flares.
// The helix is your codebase as a living organism; a node flares red when a
// mutation breaks a rule (synced with the console cycle in main.js).
import * as THREE from 'three';
import { animate } from 'animejs';

const GREEN = new THREE.Color('#63f7a6');
const RED   = new THREE.Color('#ff6a54');
const HELD  = new THREE.Color('#9dffc6');

/** radial white→transparent sprite used for the additive glow halo */
function makeGlowTexture() {
  const c = document.createElement('canvas');
  c.width = c.height = 64;
  const ctx = c.getContext('2d');
  const g = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
  g.addColorStop(0.0, 'rgba(255,255,255,1)');
  g.addColorStop(0.25, 'rgba(255,255,255,0.6)');
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
  const camera = new THREE.PerspectiveCamera(42, w / h, 0.1, 100);
  // z chosen so the helix (HEIGHT below) fits the vertical frustum with margin:
  // visible height ≈ 2*z*tan(fov/2) ≈ 2*8.0*0.384 ≈ 6.14 > HEIGHT 5.6
  camera.position.set(0, 0, 8.0);

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(w, h);
  renderer.setClearColor(0x000000, 0);
  container.appendChild(renderer.domElement);

  const group = new THREE.Group();
  group.position.x = 0.4;          // nudge toward the right of the stage
  scene.add(group);

  const glowTex = makeGlowTexture();
  const NODES = 30, R = 1.3, TURN = 0.52, HEIGHT = 5.6;   // ~2.5 turns, compact helix
  const GLOW_BASE = 0.5;
  const sphereGeo = new THREE.SphereGeometry(0.11, 18, 18);
  const nodes = [];

  function addNode(x, y, z) {
    const mat = new THREE.MeshBasicMaterial({ color: GREEN.clone() });
    const sph = new THREE.Mesh(sphereGeo, mat);
    sph.position.set(x, y, z);
    group.add(sph);

    const sprMat = new THREE.SpriteMaterial({
      map: glowTex, color: GREEN.clone(), blending: THREE.AdditiveBlending,
      transparent: true, depthWrite: false, opacity: 0.85,
    });
    const spr = new THREE.Sprite(sprMat);
    spr.scale.setScalar(GLOW_BASE);
    spr.position.set(x, y, z);
    group.add(spr);

    nodes.push({ sph, spr, mat, sprMat, baseScale: 1 });
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
  // base-pair rungs as solid cylinders — the ladder between the two strands
  const rungMat = new THREE.MeshBasicMaterial({ color: GREEN, transparent: true, opacity: 0.4 });
  const Y_AXIS = new THREE.Vector3(0, 1, 0);
  for (let i = 0; i < NODES; i++) {
    const a = strandA[i], b = strandB[i];
    const dir = new THREE.Vector3().subVectors(b, a);
    const len = dir.length();
    const rung = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.05, len, 10), rungMat);
    rung.position.copy(a).add(b).multiplyScalar(0.5);
    rung.quaternion.setFromUnitVectors(Y_AXIS, dir.normalize());
    group.add(rung);
  }
  // sugar-phosphate backbones as thick glowing tubes
  const backboneMat = new THREE.MeshBasicMaterial({ color: GREEN, transparent: true, opacity: 0.6 });
  for (const pts of [strandA, strandB]) {
    const curve = new THREE.CatmullRomCurve3(pts);
    const geo = new THREE.TubeGeometry(curve, NODES * 8, 0.12, 14, false);   // thick strands
    group.add(new THREE.Mesh(geo, backboneMat));
  }

  // ── interaction state ──────────────────────────────────────────────
  let targetTiltX = 0, targetTiltY = 0;
  const onPointer = (e) => {
    const nx = (e.clientX / window.innerWidth) * 2 - 1;
    const ny = (e.clientY / window.innerHeight) * 2 - 1;
    targetTiltY = nx * 0.45;
    targetTiltX = ny * 0.35;
  };
  if (!reduced) window.addEventListener('pointermove', onPointer, { passive: true });

  const clock = new THREE.Clock();
  let raf = 0, running = false;
  function tick() {
    const dt = Math.min(clock.getDelta(), 0.05);
    group.rotation.y += dt * 0.34;
    group.rotation.x += (targetTiltX - group.rotation.x) * 0.05;
    camera.position.x += (targetTiltY * 0.6 - camera.position.x) * 0.05;
    camera.lookAt(0, 0, 0);
    renderer.render(scene, camera);
    raf = requestAnimationFrame(tick);
  }
  // ambient "mutation storm": frequent red flares on random nodes, independent
  // of the console cycle, so red spots keep popping across the whole helix.
  let ambientTO = 0;
  function scheduleAmbient() {
    const delay = 2400 + Math.random() * 3200;   // ~2.4–5.6s between flares (calmer)
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

  // pause when the hero scrolls out of view (battery + perf)
  const io = new IntersectionObserver(([e]) => (e.isIntersecting ? start() : stop()), { threshold: 0 });
  io.observe(container);

  function resize() {
    w = container.clientWidth || 1;
    h = container.clientHeight || 1;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
    if (reduced) renderer.render(scene, camera);
  }
  const ro = new ResizeObserver(resize);
  ro.observe(container);

  // ── the money shot: a node mutates and flares ─────────────────────
  function flare(broke) {
    const node = nodes[(Math.random() * nodes.length) | 0];
    const target = broke ? RED : HELD;
    const peak = broke ? 2.6 : 1.8;
    const proxy = { t: 0 };
    animate(proxy, {
      t: [0, 1], duration: broke ? 900 : 700, ease: 'inOutQuad',
      onUpdate: () => {
        const k = Math.sin(proxy.t * Math.PI);          // 0→1→0
        node.mat.color.copy(GREEN).lerp(target, k);
        node.sprMat.color.copy(node.mat.color);
        node.sph.scale.setScalar(1 + k * (peak - 1));
        node.spr.scale.setScalar(GLOW_BASE * (1 + k * 2.2));
        node.sprMat.opacity = 0.85 + k * 0.15;
      },
      onComplete: () => {
        node.mat.color.copy(GREEN);
        node.sprMat.color.copy(GREEN);
        node.sph.scale.setScalar(1);
        node.spr.scale.setScalar(GLOW_BASE);
        node.sprMat.opacity = 0.85;
      },
    });
  }

  if (reduced) { group.rotation.set(-0.12, 0.5, 0); renderer.render(scene, camera); }
  else start();

  return {
    flare,
    dispose() {
      stop(); io.disconnect(); ro.disconnect();
      window.removeEventListener('pointermove', onPointer);
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}
