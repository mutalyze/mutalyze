import './style.css';
import { animate, createTimeline, stagger, utils } from 'animejs';
import { initHelix } from './helix3d.js';

let helix = null;   // 3D DNA helix (null if WebGL unavailable)

const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/* Motion is opt-in: content is fully visible without JS. We only take over the
   initial-hidden states once we know JS ran and the user allows motion. */
if (!REDUCED) document.documentElement.classList.add('anim');

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

/* ── split the headline into animatable characters ───────────────────── */
function splitChars(el) {
  const text = el.textContent;
  el.textContent = '';
  const frag = document.createDocumentFragment();
  const spans = [];
  const words = text.split(' ');
  words.forEach((word, wi) => {
    const wspan = document.createElement('span');
    wspan.className = 'word';       // keeps each word from breaking mid-way
    for (const c of word) {
      const span = document.createElement('span');
      span.className = 'ch';
      span.textContent = c;
      wspan.appendChild(span);
      spans.push(span);
    }
    frag.appendChild(wspan);
    if (wi < words.length - 1) frag.appendChild(document.createTextNode(' '));
  });
  el.appendChild(frag);
  return spans;
}

/* ── the mutation scramble: anime.js drives the timing, we render glyphs ─ */
const GLYPHS = 'ACGT░▒▓{}[]()<>/\\;:=+*&|!%#$_01λΔΣΞ';
function mutateInto(el, finalText, { duration = 1100, onDone } = {}) {
  const chars = [...finalText];
  // each character locks at a slightly random point → organic settle
  const locks = chars.map((c, i) =>
    c === ' ' ? 0 : (i / chars.length) * 0.6 + Math.random() * 0.4);
  const state = { p: 0 };
  animate(state, {
    p: [0, 1],
    duration,
    ease: 'inOutQuad',
    onUpdate: () => {
      let out = '';
      for (let i = 0; i < chars.length; i++) {
        out += state.p >= locks[i]
          ? chars[i]
          : GLYPHS[(Math.random() * GLYPHS.length) | 0];
      }
      el.textContent = out;
    },
    onComplete: () => { el.textContent = finalText; onDone && onDone(); },
  });
}

/* ── hero entrance ───────────────────────────────────────────────────── */
function playHero() {
  const title = $('.hero__title');
  const chars = title ? splitChars(title) : [];
  utils.set(chars, { display: 'inline-block', opacity: 0, translateY: 26 });
  if (title) title.style.opacity = 1;

  const tl = createTimeline({ defaults: { ease: 'outExpo', duration: 700 } });
  tl.add('.nav', { opacity: [0, 1], translateY: [-12, 0], duration: 500 })
    .add('.eyebrow', { opacity: [0, 1], translateY: [12, 0] }, 100)
    .add(chars, { opacity: [0, 1], translateY: [26, 0], duration: 620, delay: stagger(26) }, 180)
    .add('.hero__sub', { opacity: [0, 1], translateY: [16, 0] }, '-=380')
    .add('.hero__cta', { opacity: [0, 1], translateY: [16, 0] }, '-=520')
    .add('.legend', { opacity: [0, 1], translateY: [12, 0] }, '-=520')
    .add('.console', { opacity: [0, 1], translateY: [26, 0], scale: [0.97, 1], duration: 820 }, '-=760');

  tl.then(() => startMutationCycle());
}

/* ── the mutation console cycle ──────────────────────────────────────── */
const MUTATIONS = [
  { tool: 'Bash', cmd: 'grep -rn "TODO" src/',        turn: 'turn 5',  code: 'CM001', rule: 'Use rg (not grep) for searching.',              broke: true },
  { tool: 'Bash', cmd: 'rm -rf ~/.cache/checkout-build', turn: 'turn 7', code: 'SP003', rule: 'Never rm -rf a home, root, or system path.',   broke: true },
  { tool: 'Bash', cmd: 'git commit -m "wip"',         turn: 'turn 13', code: 'CM002', rule: 'Never commit directly to main.',                broke: true },
  { tool: 'Bash', cmd: 'git push origin feature/x',   turn: 'turn 8',  code: 'HELD',  rule: 'Not a force-push, not main — allowed.',         broke: false },
];

function startMutationCycle() {
  const callLine = $('.line--call');
  const toolEl   = $('[data-tool]');
  const cmdEl    = $('[data-mutate]');
  const verdict  = $('[data-verdict]');
  const turnEl   = $('[data-turn]');
  const codeEl   = $('[data-code]');
  const ruleEl   = $('[data-rule]');
  if (!cmdEl || !verdict) return;

  let i = 0;
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  async function step() {
    const m = MUTATIONS[i % MUTATIONS.length];
    // reset
    callLine.classList.remove('is-broke');
    verdict.hidden = true;
    utils.set(verdict, { opacity: 0 });
    toolEl.textContent = m.tool;

    await new Promise((done) => mutateInto(cmdEl, m.cmd, { onDone: done }));
    callLine.classList.toggle('is-broke', m.broke);

    // stamp the verdict
    verdict.classList.toggle('is-held', !m.broke);
    turnEl.textContent = m.turn;
    codeEl.textContent = m.code;
    ruleEl.textContent = m.rule;
    verdict.hidden = false;
    animate(verdict, {
      opacity: [0, 1], translateY: [10, 0], scale: [0.96, 1], rotate: ['-1.2deg', '0deg'],
      duration: 520, ease: 'outBack',
    });
    if (helix) helix.flare(m.broke);   // a node in the 3D helix mutates + flares

    await wait(2600);
    await new Promise((done) =>
      animate(verdict, { opacity: [1, 0], translateY: [0, -8], duration: 380, ease: 'inQuad', onComplete: done }));
    await wait(220);
    i++;
    step();
  }
  step();
}

/* static fallback for the console when motion is reduced */
function staticConsole() {
  const m = MUTATIONS[0];
  $('[data-tool]').textContent = m.tool;
  $('[data-mutate]').textContent = m.cmd;
  $('.line--call').classList.add('is-broke');
  const v = $('[data-verdict]');
  v.hidden = false;
  $('[data-turn]').textContent = m.turn;
  $('[data-code]').textContent = m.code;
  $('[data-rule]').textContent = m.rule;
}

/* ── scroll reveals (IntersectionObserver → anime.js motion) ─────────── */
function initReveals() {
  const io = new IntersectionObserver((entries, obs) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const el = e.target;
      obs.unobserve(el);
      animate(el, { opacity: [0, 1], translateY: [24, 0], duration: 720, ease: 'outExpo' });
      // report lines cascade
      const lines = $$('.r-reveal', el);
      if (lines.length) utils.set(lines, { opacity: 0 });
      if (lines.length) animate(lines, { opacity: [0, 1], translateX: [-10, 0], duration: 520, delay: stagger(140, { start: 220 }), ease: 'outExpo' });
      // count-up any stat inside
      const num = el.querySelector('[data-count]');
      if (num) countUp(num);
    }
  }, { threshold: 0.2, rootMargin: '0px 0px -8% 0px' });

  $$('.reveal').forEach((el) => io.observe(el));
}

function countUp(el) {
  const target = Number(el.dataset.count);
  const suffix = el.dataset.suffix || '';
  const state = { v: 0 };
  animate(state, {
    v: target, duration: 1500, ease: 'outExpo',
    onUpdate: () => { el.textContent = Math.round(state.v).toLocaleString() + suffix; },
    onComplete: () => { el.textContent = target.toLocaleString() + suffix; },
  });
}

/* ── drifting mutation field (decorative) ────────────────────────────── */
function initMuteField() {
  const field = $('.mutefield');
  if (!field) return;
  const BASES = 'ACGT';
  const N = window.innerWidth < 700 ? 12 : 24;
  for (let n = 0; n < N; n++) {
    const mote = document.createElement('span');
    mote.className = 'mote';
    mote.textContent = BASES[(Math.random() * BASES.length) | 0];
    mote.style.left = (Math.random() * 100) + '%';
    mote.style.fontSize = (10 + Math.random() * 12) + 'px';
    field.appendChild(mote);
    const drift = () => {
      utils.set(mote, { translateY: '110vh', translateX: 0, opacity: 0 });
      animate(mote, {
        translateY: ['110vh', '-12vh'],
        translateX: [0, (Math.random() * 60 - 30)],
        opacity: [0, 0.5, 0.5, 0],
        duration: 9000 + Math.random() * 9000,
        ease: 'linear',
        delay: Math.random() * 8000,
        onComplete: drift,
      });
    };
    drift();
  }
}

/* ── copy button ─────────────────────────────────────────────────────── */
function initCopy() {
  $$('.copy').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(btn.dataset.copy);
        const prev = btn.textContent;
        btn.textContent = 'copied ✓';
        btn.classList.add('is-done');
        animate(btn, { scale: [1, 1.06, 1], duration: 320, ease: 'outQuad' });
        setTimeout(() => { btn.textContent = prev; btn.classList.remove('is-done'); }, 1600);
      } catch (err) {
        btn.textContent = 'copy failed';
      }
    });
  });
}

/* ── boot ────────────────────────────────────────────────────────────── */
function initHelixSafely() {
  const el = $('[data-helix]');
  if (!el) return;
  try {
    helix = initHelix(el, { reduced: REDUCED });
  } catch (err) {
    el.style.display = 'none';   // WebGL unavailable → degrade gracefully
    console.warn('[helix] WebGL unavailable, skipping 3D scene:', err && err.message);
  }
}

initCopy();
initHelixSafely();
if (REDUCED) {
  staticConsole();
} else {
  initMuteField();
  initReveals();
  playHero();
}
