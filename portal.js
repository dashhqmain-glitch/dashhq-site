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

// ── Bootstrap: URL params → sessionStorage → idle ────────────────────────────
async function init() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get('token');
  const error = params.get('error');

  // Clean the URL so the token isn't bookmarkable
  if (token || error) history.replaceState({}, '', window.location.pathname);

  if (error) { setState('prelogin'); return; }
  if (token) { await verifyToken(token); return; }

  const saved = sessionStorage.getItem(TOKEN_KEY);
  if (saved) { await verifyToken(saved); return; }

  setState('prelogin');
}

document.addEventListener('DOMContentLoaded', init);

// ── Particle network ─────────────────────────────────────────────────────────
(function(){
  const c=document.getElementById('particles');if(!c)return;const x=c.getContext('2d');let w,h,pts,raf;
  function size(){w=c.width=innerWidth;h=c.height=innerHeight;}
  function init(){size();const n=Math.min(64,Math.floor(w*h/18000));pts=[];for(let i=0;i<n;i++)pts.push({x:Math.random()*w,y:Math.random()*h,vx:(Math.random()-.5)*.26,vy:(Math.random()-.5)*.26});}
  function loop(){x.clearRect(0,0,w,h);for(const p of pts){p.x+=p.vx;p.y+=p.vy;if(p.x<0||p.x>w)p.vx*=-1;if(p.y<0||p.y>h)p.vy*=-1;}
    for(let i=0;i<pts.length;i++){for(let j=i+1;j<pts.length;j++){const dx=pts[i].x-pts[j].x,dy=pts[i].y-pts[j].y,d=Math.hypot(dx,dy);if(d<130){x.strokeStyle='rgba(91,155,248,'+(.14*(1-d/130))+')';x.lineWidth=1;x.beginPath();x.moveTo(pts[i].x,pts[i].y);x.lineTo(pts[j].x,pts[j].y);x.stroke();}}x.fillStyle='rgba(120,160,255,.6)';x.beginPath();x.arc(pts[i].x,pts[i].y,1.4,0,7);x.fill();}
    raf=requestAnimationFrame(loop);}
  init();loop();addEventListener('resize',()=>{cancelAnimationFrame(raf);init();loop();});
})();
