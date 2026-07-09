// ── Config ──────────────────────────────────────────────────────────────────
// Localhost → local FastAPI server. Production → same domain (Vercel routes /auth/* to Python).
const BACKEND_URL = ['localhost', '127.0.0.1'].includes(window.location.hostname)
  ? 'http://localhost:8000'
  : 'https://www.dashhq.site';
const TOKEN_KEY = 'dashhq_citizen_token';

// ── State machine ────────────────────────────────────────────────────────────
function setState(s) {
  document.querySelectorAll('.state').forEach(el => el.classList.toggle('on', el.dataset.state === s));

  const demo = document.querySelector('.demo');
  if (!demo) return;
  demo.classList.toggle('show', s === 'member' || s === 'notmember');
  demo.querySelectorAll('button[data-state]').forEach(el => {
    el.classList.toggle('active', el.dataset.state === s);
    if (el.dataset.state === 'member') el.classList.toggle('hide', s !== 'member');
    if (el.dataset.state === 'notmember') el.classList.toggle('hide', s !== 'notmember');
  });
}

// Kept for the dev demo bar at the bottom of the page.
function verify(result) { setState('verifying'); setTimeout(() => setState(result), 1500); }

// ── Discord OAuth ────────────────────────────────────────────────────────────
function startDiscordAuth() {
  window.location.href = `${BACKEND_URL}/auth/discord`;
}

function signOut() {
  sessionStorage.removeItem(TOKEN_KEY);
  setState('prelogin');
}

// ── Card population ──────────────────────────────────────────────────────────
function updateCard(data) {
  const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
  set('cardName', data.display_name);
  set('cardHandle', data.handle);
  set('cardTier', `★ ${data.tier}`);
  set('cardJoined', data.joined);
  const av = document.getElementById('cardAvatar');
  if (av && data.avatar) { av.src = data.avatar; av.alt = data.display_name; }
}

// ── Token verification ───────────────────────────────────────────────────────
async function verifyToken(token) {
  setState('verifying');
  try {
    const res = await fetch(`${BACKEND_URL}/auth/me?token=${encodeURIComponent(token)}`);
    if (!res.ok) throw new Error('bad_token');
    const data = await res.json();
    if (data.is_member) {
      updateCard(data);
      sessionStorage.setItem(TOKEN_KEY, token);
      setState('member');
    } else {
      sessionStorage.removeItem(TOKEN_KEY);
      setState('notmember');
    }
  } catch {
    sessionStorage.removeItem(TOKEN_KEY);
    setState('prelogin');
  }
}

// ── Portal overlay open/close (mirrors app.js's research-modal pattern) ──────
function openPortal() {
  const modal = document.getElementById('portalModal');
  if (!modal) return;
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';
}
function closePortal() {
  const modal = document.getElementById('portalModal');
  if (!modal) return;
  modal.classList.remove('open');
  modal.setAttribute('aria-hidden', 'true');
  document.body.style.overflow = '';
}
(function () {
  const modal = document.getElementById('portalModal');
  if (!modal) return;
  // Same intentional-backdrop-dismiss logic as the research modal: close only
  // on a deliberate tap on the empty glass area, never on the card/controls.
  let downT = null, downX = 0, downY = 0;
  modal.addEventListener('pointerdown', e => { downT = e.target; downX = e.clientX; downY = e.clientY; });
  modal.addEventListener('pointerup', e => {
    const t = e.target;
    const dead = t === modal || t.classList.contains('rm-scroll');
    const moved = Math.hypot(e.clientX - downX, e.clientY - downY);
    if (dead && t === downT && moved < 8) closePortal();
  });
  window.addEventListener('keydown', e => { if (e.key === 'Escape' && modal.classList.contains('open')) closePortal(); });
})();

// ── Bootstrap: URL params → sessionStorage → idle ────────────────────────────
async function init() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get('token');
  const error = params.get('error');

  // Clean the URL so the token isn't bookmarkable
  if (token || error) history.replaceState({}, '', window.location.pathname);

  if (error) { openPortal(); setState('prelogin'); return; }
  if (token) { openPortal(); await verifyToken(token); return; }

  const saved = sessionStorage.getItem(TOKEN_KEY);
  if (saved) { openPortal(); await verifyToken(saved); return; }

  // Nothing to verify — leave the portal closed until the user opens it.
  setState('prelogin');
}

document.addEventListener('DOMContentLoaded', init);
