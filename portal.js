// ── Config ──────────────────────────────────────────────────────────────────
// Localhost → local FastAPI server. Production → same domain (Vercel routes /auth/* to Python).
const BACKEND_URL = ['localhost', '127.0.0.1'].includes(window.location.hostname)
  ? 'http://localhost:8000'
  : 'https://www.dashhq.site';
const TOKEN_KEY = 'dashhq_citizen_token';

// ── Crash isolation ───────────────────────────────────────────────────────────
// A bug in any one tool's init/render logic must never be able to silently
// block every tool listed after it in a shared sequence (Dash.init() calls
// several tools' init() back to back — an uncaught throw in one stops the
// rest from ever running) or leave a citizen stuck on a broken screen with
// zero feedback. safeCall wraps a single step so a failure there is
// contained and visible, not a chain reaction.
function safeCall(label, fn) {
  try { return fn(); }
  catch (e) { console.error('[' + label + '] failed, continuing:', e); }
}
window.addEventListener('error', function (e) {
  console.error('[uncaught]', e.message, e.error);
});
window.addEventListener('unhandledrejection', function (e) {
  console.error('[unhandled promise rejection]', e.reason);
});

// ── State machine ────────────────────────────────────────────────────────────
function setState(s) {
  document.querySelectorAll('.state').forEach(el => el.classList.toggle('on', el.dataset.state === s));
  document.getElementById('auth')?.classList.toggle('wide', s === 'member');
  // .auth normally has backdrop-filter:blur() for the login-card look, which
  // creates a new CSS containing block for descendant position:fixed
  // elements — that would trap the dashboard shell's fixed, inset:0 sizing
  // inside .auth's small content box instead of the real viewport. is-dash
  // strips that filter so #dash can actually fill the screen.
  document.getElementById('auth')?.classList.toggle('is-dash', s === 'member');
  document.querySelector('.portal')?.classList.toggle('in-hub', s === 'member');
  // The dashboard shell reads like a full app rather than a page with
  // marketing chrome floating on top of it — hide the × close button once
  // inside; the sidebar's own brand-logo click is the way back out instead.
  document.getElementById('portalModal')?.classList.toggle('dash-active', s === 'member');
  if (s === 'member') Dash.init();
}

// ── Discord OAuth ────────────────────────────────────────────────────────────
function startDiscordAuth() {
  window.location.href = `${BACKEND_URL}/auth/discord`;
}

function signOut() {
  sessionStorage.removeItem(TOKEN_KEY);
  setState('prelogin');
}

// ── Card population (feeds the Card, dashboard header, and Profile alike) ────
function updateCard(data) {
  const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
  set('cardName', data.display_name);
  set('cardHandle', data.handle);
  set('cardTier', data.tier);
  set('cardJoined', data.joined);
  set('dtopName', data.display_name);
  set('profileName', data.display_name);
  set('profileHandle', data.handle);
  set('profileTierTag', `★ ${data.tier}`);
  set('profileSinceTag', `Citizen since ${data.joined}`);
  ['dtopAvatar', 'profileAvatar'].forEach(id => {
    const av = document.getElementById(id);
    if (av && data.avatar) { av.src = data.avatar; av.alt = data.display_name; }
  });
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
let _scrollLockY = 0;
function openPortal() {
  const modal = document.getElementById('portalModal');
  if (!modal) return;
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
  modal.scrollTop = 0;
  _scrollLockY = window.scrollY || document.documentElement.scrollTop || 0;
  document.body.style.position = 'fixed';
  document.body.style.top = -_scrollLockY + 'px';
  document.body.style.left = '0';
  document.body.style.right = '0';
  document.body.style.width = '100%';
}
function closePortal() {
  const modal = document.getElementById('portalModal');
  if (!modal) return;
  modal.classList.remove('open');
  modal.setAttribute('aria-hidden', 'true');
  document.body.style.position = '';
  document.body.style.top = '';
  document.body.style.left = '';
  document.body.style.right = '';
  document.body.style.width = '';
  window.scrollTo(0, _scrollLockY);
}
(function () {
  const modal = document.getElementById('portalModal');
  if (!modal) return;
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
  safeCall('CardTheme.init', function () { CardTheme.init(); });
  const params = new URLSearchParams(window.location.search);
  const token = params.get('token');
  const error = params.get('error');

  if (token || error) history.replaceState({}, '', window.location.pathname);

  if (error) { openPortal(); setState('prelogin'); return; }
  if (token) { openPortal(); await verifyToken(token); return; }

  const saved = sessionStorage.getItem(TOKEN_KEY);
  if (saved) { await verifyToken(saved); return; }

  setState('prelogin');
}

// ── DASHBOARD SHELL — sidebar nav, collapse, mobile drawer, bento overview ───
var Dash = (function () {
  var inited = false;
  var currentPage = 'overview';
  function go(page) {
    document.querySelectorAll('.dsb-item').forEach(function (t) { t.classList.toggle('active', t.dataset.page === page); });
    document.querySelectorAll('.dpage').forEach(function (p) { p.classList.toggle('on', p.dataset.page === page); });
    closeMobile();
    var c = document.getElementById('dcontent');
    if (c) c.scrollTop = 0;
    if (currentPage === 'pairs' && page !== 'pairs' && typeof Pairs !== 'undefined') Pairs.onLeave();
    if (page === 'pairs' && typeof Pairs !== 'undefined') Pairs.onEnter();
    currentPage = page;
    if (page === 'overview') renderBento();
  }
  function toggleCollapse() { document.getElementById('dash').classList.toggle('collapsed'); }
  function openMobile() { document.getElementById('dash').classList.add('mobile-open'); }
  function closeMobile() { document.getElementById('dash').classList.remove('mobile-open'); }
  function renderBento() {
    var g = document.getElementById('gasBig');
    var bg = document.getElementById('bentoGas');
    var bgd = document.getElementById('bentoGasDesc');
    if (bg && g) {
      var unitWord = (document.getElementById('gasUnitWord') || {}).textContent || 'gwei';
      // The unit needs its own (much smaller) size, concatenating it into
      // one plain-text stat string let "gwei" wrap onto its own line at
      // the same huge font size as the number when the combined text was
      // too wide for the tile.
      bg.innerHTML = (g.textContent && g.textContent !== '-')
        ? g.textContent + ' <span class="bento-unit">' + unitWord + '</span>'
        : '-';
    }
    if (bgd) bgd.textContent = ((document.getElementById('gasChainLabel') || {}).textContent || 'Ethereum mainnet') + ' · avg';

    var pf = document.getElementById('pairsFeed');
    var bp = document.getElementById('bentoPairs');
    if (bp && pf) bp.textContent = pf.querySelectorAll('.pair-row').length;

    var t = (typeof Ticker !== 'undefined') ? Ticker.top() : null;
    var bms = document.getElementById('bentoMoverSym'), bmc = document.getElementById('bentoMoverChg');
    var bmSpark = document.getElementById('bentoMoverSpark');
    if (t && bms && bmc) {
      bms.textContent = t.sym;
      var up = t.chg >= 0;
      bmc.textContent = (up ? '+' : '') + t.chg.toFixed(2) + '%';
      bmc.style.color = up ? 'var(--green)' : 'var(--red)';
    }
    if (bmSpark) bmSpark.innerHTML = (typeof Ticker !== 'undefined') ? Ticker.topSparkSvg() : '';

    var nc = document.getElementById('bentoNftStack'), nd = document.getElementById('bentoNftDesc');
    if (nc && nd && typeof Watchlist !== 'undefined') {
      var ns = Watchlist.summary();
      nc.innerHTML = Watchlist.bentoStackHtml(5);
      nd.textContent = ns.count ? (ns.count + (ns.count === 1 ? ' collection watched' : ' collections watched')) : 'Search & discover collections';
    }

    try {
      var saved = JSON.parse(localStorage.getItem('dashXrayLast') || 'null');
      if (saved) {
        var be = document.getElementById('bentoTierEmoji'), bn = document.getElementById('bentoTierName');
        var br = document.getElementById('bentoEmojiRing'), bd = document.getElementById('bentoTierDesc');
        if (be) be.textContent = saved.emoji;
        if (bn) bn.innerHTML = saved.name + ' · <span style="color:' + saved.color + '">' + saved.score + '/100</span>';
        if (bd) bd.textContent = saved.flavor;
        if (br) {
          br.style.background = 'color-mix(in srgb, ' + saved.color + ' 18%, transparent)';
          br.style.borderColor = 'color-mix(in srgb, ' + saved.color + ' 55%, transparent)';
          br.style.boxShadow = '0 0 22px color-mix(in srgb, ' + saved.color + ' 30%, transparent)';
        }
      }
    } catch (e) { }
  }
  function init() {
    if (inited) { renderBento(); return; }
    inited = true;
    // Each tool's init runs independently — a bug in any one (say, a
    // malformed backend response) must not prevent every tool listed
    // after it from ever initializing.
    safeCall('Ticker.init', function () { Ticker.init(); });
    safeCall('Gas.init', function () { Gas.init(); });
    safeCall('Pnl.init', function () { Pnl.init(); });
    safeCall('Ape.calc', function () { Ape.calc(); });
    safeCall('Pairs.init', function () { Pairs.init(); });
    safeCall('Slip.calc', function () { Slip.calc(); });
    safeCall('Watchlist.init', function () { Watchlist.init(); });
    safeCall('Profile.init', function () { Profile.init(); });
    setInterval(function () { safeCall('renderBento', renderBento); }, 5000);
    setTimeout(function () { safeCall('renderBento', renderBento); }, 300);
  }
  return { go: go, init: init, toggleCollapse: toggleCollapse, openMobile: openMobile, closeMobile: closeMobile, renderBento: renderBento };
})();

// ── CARD ACTIONS — download + share ───────────────────────────────────────────
// ── Membership card color theme (orb selector) ───────────────────────────────
var CardTheme = (function () {
  var THEMES = ['blue', 'gold', 'purple', 'red', 'mono'];
  var KEY = 'dashhq_card_theme';
  var current = 'blue';
  function set(name) {
    if (THEMES.indexOf(name) === -1) return;
    current = name;
    var card = document.getElementById('memberCard');
    if (card) {
      THEMES.forEach(function (t) { card.classList.remove('theme-' + t); });
      card.classList.add('theme-' + name);
      card.dataset.theme = name;
    }
    document.querySelectorAll('.orb-btn').forEach(function (b) {
      b.classList.toggle('active', b.dataset.theme === name);
    });
    try { localStorage.setItem(KEY, name); } catch (e) { }
  }
  function get() { return current; }
  function init() {
    var saved = null;
    try { saved = localStorage.getItem(KEY); } catch (e) { }
    set(THEMES.indexOf(saved) !== -1 ? saved : 'blue');
  }
  return { init: init, set: set, get: get };
})();

var CardActions = (function () {
  function download() {
    var card = document.getElementById('memberCard');
    var rect = card.getBoundingClientRect();
    var S = 3, W = Math.round(rect.width * S), H = Math.round(rect.height * S);
    var cs = getComputedStyle(card);
    var cv1 = cs.getPropertyValue('--c-1').trim() || '#10204f';
    var cv2 = cs.getPropertyValue('--c-2').trim() || '#0a1330';
    var cv3 = cs.getPropertyValue('--c-3').trim() || '#0a1a3e';
    var accent = cs.getPropertyValue('--c-accent').trim() || '#5B9BF8';
    var lineColor = cs.getPropertyValue('--c-line').trim() || 'rgba(91,155,248,.5)';
    var glowColor = cs.getPropertyValue('--c-glow').trim() || 'rgba(91,155,248,.4)';
    var accentSoft = cs.getPropertyValue('--c-accent-soft').trim() || 'rgba(91,155,248,.16)';
    var accentBorder = cs.getPropertyValue('--c-accent-border').trim() || 'rgba(91,155,248,.5)';

    var logo = new Image(); logo.crossOrigin = 'anonymous';
    var logoReady = new Promise(function (res) { logo.onload = res; logo.onerror = res; logo.src = 'assets/logo-mark-white.png'; });
    var fontsReady = Promise.all([
      document.fonts.load('400 40px OCRAStd'),
      document.fonts.load('700 16px Geist'),
      document.fonts.load('600 14px Geist')
    ]).catch(function () { });

    Promise.all([fontsReady, logoReady]).then(draw);

    function chromeGrad(x, yTop, yBottom) {
      var g = x.createLinearGradient(0, yTop, 0, yBottom);
      g.addColorStop(0, '#f7f7fa'); g.addColorStop(0.32, '#c7c7d2'); g.addColorStop(0.55, '#f2f3f7'); g.addColorStop(1, '#93939f');
      return g;
    }

    function draw() {
      var cvs = document.createElement('canvas'); cvs.width = W; cvs.height = H;
      var x = cvs.getContext('2d');
      function rr(px, py, w, h, r) { x.beginPath(); x.moveTo(px + r, py); x.arcTo(px + w, py, px + w, py + h, r); x.arcTo(px + w, py + h, px, py + h, r); x.arcTo(px, py + h, px, py, r); x.arcTo(px, py, px + w, py, r); x.closePath(); }
      rr(0, 0, W, H, 20 * S); x.save(); x.clip();

      var g = x.createLinearGradient(0, 0, W, H); g.addColorStop(0, cv1); g.addColorStop(0.55, cv2); g.addColorStop(1, cv3);
      x.fillStyle = g; x.fillRect(0, 0, W, H);

      var rg = x.createRadialGradient(W * 0.88, H * 0.05, 0, W * 0.88, H * 0.05, W * 0.42);
      rg.addColorStop(0, glowColor); rg.addColorStop(1, 'rgba(0,0,0,0)');
      x.globalCompositeOperation = 'screen'; x.fillStyle = rg; x.fillRect(0, 0, W, H);
      x.globalCompositeOperation = 'source-over';

      var vx = W / 1034, vy = H / 652;
      var segs = [[0, 652, 1034, 0], [30, 70, 150, 190], [150, 190, 150, 280], [150, 280, 260, 280], [260, 280, 260, 380], [700, 60, 820, 180], [820, 180, 820, 80], [60, 450, 180, 570], [380, 500, 500, 620], [600, 440, 720, 560], [850, 380, 970, 500], [900, 240, 1010, 130]];
      x.strokeStyle = lineColor; x.lineWidth = 1.3 * S; x.globalAlpha = 0.55;
      segs.forEach(function (l) { x.beginPath(); x.moveTo(l[0] * vx, l[1] * vy); x.lineTo(l[2] * vx, l[3] * vy); x.stroke(); });
      [46, 80, 114].forEach(function (r) { x.beginPath(); x.arc(890 * vx, 70 * vy, r * Math.min(vx, vy), 0, Math.PI * 2); x.stroke(); });
      var dots = [[30, 70], [150, 190], [150, 280], [260, 280], [260, 380], [700, 60], [820, 180], [820, 80], [60, 450], [180, 570], [380, 500], [500, 620], [600, 440], [720, 560], [850, 380], [970, 500], [900, 240], [1010, 130]];
      x.fillStyle = lineColor;
      dots.forEach(function (d) { x.beginPath(); x.arc(d[0] * vx, d[1] * vy, 4 * Math.min(vx, vy), 0, Math.PI * 2); x.fill(); });
      x.globalAlpha = 1;

      var P = 26 * S;
      var ls = 18 * S;
      if (logo.complete && logo.naturalWidth) x.drawImage(logo, P, P - 3 * S, ls, ls);
      x.fillStyle = '#fff'; x.font = '700 ' + (14 * S) + 'px Geist,Sora,sans-serif'; x.textBaseline = 'middle'; x.letterSpacing = (1.5 * S) + 'px';
      x.fillText('DASH CITIZEN', P + ls + 8 * S, P - 3 * S + ls / 2); x.letterSpacing = '0px'; x.textBaseline = 'alphabetic';

      var tierTxt = ((document.getElementById('cardTier') || {}).textContent || 'CITIZEN').toUpperCase();
      x.font = '600 ' + (11 * S) + 'px Geist,"JetBrains Mono",monospace'; x.letterSpacing = (1 * S) + 'px';
      var gemW = 15 * S, gap = 6 * S, padL = 10 * S, padR = 13 * S;
      var tw = x.measureText(tierTxt).width;
      var pw = gemW + gap + tw + padL + padR, ph = 30 * S;
      var bx = W - P - pw, by = P - 4 * S;
      rr(bx, by, pw, ph, ph / 2); x.fillStyle = accentSoft; x.fill();
      x.strokeStyle = accentBorder; x.lineWidth = 1.5 * S; x.stroke();
      x.save(); x.translate(bx + padL, by + ph / 2 - gemW / 2); var gs = gemW / 24; x.scale(gs, gs);
      x.lineJoin = 'round';
      var gemGrad = x.createLinearGradient(0, 3, 0, 21);
      gemGrad.addColorStop(0, '#fff'); gemGrad.addColorStop(0.38, accent); gemGrad.addColorStop(1, accent);
      x.beginPath(); x.moveTo(7, 3); x.lineTo(17, 3); x.lineTo(21, 10); x.lineTo(12, 21); x.lineTo(3, 10); x.closePath();
      x.fillStyle = gemGrad; x.fill();
      x.strokeStyle = accent; x.globalAlpha = 0.4; x.lineWidth = 0.6; x.stroke(); x.globalAlpha = 1;
      x.beginPath(); x.moveTo(6.7, 4.6); x.bezierCurveTo(8.5, 3.6, 15.5, 3.6, 17.3, 4.6); x.lineTo(15.2, 7.6); x.lineTo(8.8, 7.6); x.closePath();
      x.fillStyle = 'rgba(255,255,255,.55)'; x.fill();
      x.fillStyle = 'rgba(255,255,255,.9)'; x.beginPath(); x.arc(7.6, 9.2, 0.85, 0, Math.PI * 2); x.fill();
      x.fillStyle = 'rgba(255,255,255,.75)'; x.beginPath(); x.arc(8.2, 14, 0.65, 0, Math.PI * 2); x.fill();
      x.fillStyle = 'rgba(255,255,255,.65)'; x.beginPath(); x.arc(17, 12.6, 0.65, 0, Math.PI * 2); x.fill();
      x.restore();
      x.fillStyle = accent; x.textBaseline = 'middle';
      x.fillText(tierTxt, bx + padL + gemW + gap, by + ph / 2 + 1 * S);
      x.textBaseline = 'alphabetic'; x.letterSpacing = '0px';

      var nameTxt = (document.getElementById('cardName') || {}).textContent || 'CITIZEN';
      var nameY = H * 0.52;
      x.font = '400 ' + (30 * S) + 'px OCRAStd,monospace';
      x.fillStyle = chromeGrad(x, nameY - 26 * S, nameY + 6 * S);
      x.fillText(nameTxt, P, nameY);
      var handleTxt = (document.getElementById('cardHandle') || {}).textContent || '';
      x.fillStyle = accent; x.font = '400 ' + (13 * S) + 'px "JetBrains Mono",monospace';
      x.shadowColor = accent; x.shadowBlur = 8 * S;
      x.fillText(handleTxt, P, nameY + 24 * S);
      x.shadowBlur = 0;

      var orbCy = nameY - 10 * S, orbR = 19 * S, orbCx = W - P - orbR;
      var triX = orbCx - orbR - 14 * S;
      x.beginPath(); x.moveTo(triX, orbCy - 7 * S); x.lineTo(triX, orbCy + 7 * S); x.lineTo(triX - 11 * S, orbCy); x.closePath();
      x.fillStyle = accent; x.globalAlpha = 0.85; x.fill(); x.globalAlpha = 1;
      x.save(); x.shadowColor = glowColor; x.shadowBlur = 22 * S;
      var og = x.createRadialGradient(orbCx - orbR * 0.24, orbCy - orbR * 0.3, orbR * 0.05, orbCx - orbR * 0.1, orbCy - orbR * 0.05, orbR * 1.05);
      og.addColorStop(0, '#fff'); og.addColorStop(0.32, accent); og.addColorStop(0.92, cv2); og.addColorStop(1, cv1);
      x.beginPath(); x.arc(orbCx, orbCy, orbR, 0, Math.PI * 2); x.fillStyle = og; x.fill();
      var spec = x.createRadialGradient(orbCx - orbR * 0.3, orbCy - orbR * 0.36, 0, orbCx - orbR * 0.3, orbCy - orbR * 0.36, orbR * 0.4);
      spec.addColorStop(0, 'rgba(255,255,255,.95)'); spec.addColorStop(1, 'rgba(255,255,255,0)');
      x.beginPath(); x.arc(orbCx, orbCy, orbR, 0, Math.PI * 2); x.fillStyle = spec; x.fill();
      x.restore();

      var by2 = H - 40 * S, dotY = by2 - 22 * S;
      x.font = '600 ' + (9.5 * S) + 'px Geist,"JetBrains Mono",monospace'; x.letterSpacing = (1 * S) + 'px';
      var dotR = 3.5 * S, dotX = P + dotR;
      x.save(); x.shadowColor = glowColor; x.shadowBlur = 8 * S;
      x.beginPath(); x.arc(dotX, dotY, dotR, 0, Math.PI * 2); x.fillStyle = accent; x.fill(); x.restore();
      x.fillStyle = '#8A9BBF'; x.fillText('STATUS', dotX + dotR + 6 * S, dotY + 1 * S);
      x.letterSpacing = '0px';
      var footGrad = chromeGrad(x, by2 - 2 * S, by2 + 16 * S);
      x.font = '400 ' + (15 * S) + 'px OCRAStd,monospace'; x.fillStyle = footGrad;
      x.fillText('ACTIVE', P, by2 + 14 * S);

      var sinceTxt = (document.getElementById('cardJoined') || {}).textContent || '-';
      x.textAlign = 'right';
      x.font = '600 ' + (9.5 * S) + 'px Geist,"JetBrains Mono",monospace'; x.letterSpacing = (1 * S) + 'px'; x.fillStyle = '#8A9BBF';
      x.fillText('MEMBER SINCE', W - P, dotY + 1 * S); x.letterSpacing = '0px';
      x.font = '400 ' + (15 * S) + 'px OCRAStd,monospace'; x.fillStyle = footGrad;
      x.fillText(sinceTxt, W - P, by2 + 14 * S);
      x.textAlign = 'left';

      x.restore();
      rr(1 * S, 1 * S, W - 2 * S, H - 2 * S, 20 * S); x.strokeStyle = 'rgba(255,255,255,0.16)'; x.lineWidth = 2 * S; x.stroke();
      var a = document.createElement('a'); a.href = cvs.toDataURL('image/png'); a.download = 'dash-citizen-card.png';
      document.body.appendChild(a); a.click(); a.remove();
    }
  }
  function shareX() {
    var since = (document.getElementById('cardJoined') || {}).textContent || '2024';
    var lines = [
      "Just claimed my Dash Citizen card. Verified since " + since + ", @DASHHQX.",
      "Official Dash Citizen, verified since " + since + ". Come get yours, @DASHHQX.",
      "Dash HQ Citizen since " + since + ". One step ahead of the curve. @DASHHQX",
      "My Dash Citizen card just went live. Verified since " + since + ", @DASHHQX."
    ];
    var text = lines[Math.floor(Math.random() * lines.length)];
    var url = 'https://www.dashhq.site';
    var intent = 'https://twitter.com/intent/tweet?text=' + encodeURIComponent(text) + '&url=' + encodeURIComponent(url);
    window.open(intent, '_blank', 'noopener,width=600,height=520');
  }
  return { download: download, shareX: shareX };
})();

// ── 1. PRICE TICKER — real CoinGecko data ─────────────────────────────────────
var Ticker = (function () {
  var state = {}; // sym -> {price, history[], chg, loading, error}

  async function fetchBatch(syms) {
    if (!syms.length) return {};
    try {
      var res = await fetch(BACKEND_URL + '/toolkit/ticker?symbols=' + encodeURIComponent(syms.join(',')));
      if (!res.ok) return {};
      return await res.json();
    } catch (e) { return {}; }
  }
  function sparkPath(hist, h) {
    h = h || 24;
    if (hist.length < 2) return '';
    var min = Math.min.apply(null, hist), max = Math.max.apply(null, hist), range = (max - min) || 1;
    var w = 100;
    return hist.map(function (v, i) {
      var x = (i / (hist.length - 1)) * w, y = h - ((v - min) / range) * h;
      return (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
  }
  // A slightly taller sparkline for the dashboard's Top Mover tile, built
  // from the same live price history the ticker grid already tracks — no
  // separate fetch needed.
  function topSparkSvg() {
    var t = top();
    if (!t) return '';
    var s = state[t.sym];
    if (!s || s.history.length < 2) return '';
    var up = t.chg >= 0;
    var path = sparkPath(s.history, 36);
    return '<svg viewBox="0 0 100 36" preserveAspectRatio="none"><path d="' + path + '" fill="none" stroke="' + (up ? '#10B981' : '#EF4444') + '" stroke-width="2"/></svg>';
  }
  function render() {
    var grid = document.getElementById('tickerGrid');
    var syms = Object.keys(state);
    if (!syms.length) { grid.innerHTML = '<div class="pin-empty">Add a token above to start your watchlist.</div>'; return; }
    grid.innerHTML = syms.map(function (sym) {
      var s = state[sym];
      if (s.loading) return '<div class="tk-item"><button class="rm" onclick="Ticker.remove(\'' + sym + '\')" aria-label="Remove">×</button><div class="sym">' + sym + '</div><div class="px">Loading…</div></div>';
      if (s.error) return '<div class="tk-item"><button class="rm" onclick="Ticker.remove(\'' + sym + '\')" aria-label="Remove">×</button><div class="sym">' + sym + '</div><div class="px" style="cursor:pointer" onclick="Ticker.retry(\'' + sym + '\')" title="Click to retry">Not found, retry</div></div>';
      var up = s.chg >= 0;
      var path = sparkPath(s.history);
      return '<div class="tk-item"><button class="rm" onclick="Ticker.remove(\'' + sym + '\')" aria-label="Remove">×</button>'
        + '<div class="sym">' + sym + '</div>'
        + '<div class="px">$' + (s.price < 1 ? s.price.toFixed(4) : s.price.toFixed(2)) + '</div>'
        + '<div class="chg ' + (up ? 'up' : 'down') + '">' + (up ? '▲' : '▼') + ' ' + Math.abs(s.chg).toFixed(2) + '%</div>'
        + (path ? '<svg class="spark" viewBox="0 0 100 24" preserveAspectRatio="none"><path d="' + path + '" fill="none" stroke="' + (up ? '#10B981' : '#EF4444') + '" stroke-width="2"/></svg>' : '')
        + '</div>';
    }).join('');
  }
  function applyResult(sym, r) {
    var s = state[sym];
    if (!s) return;
    s.loading = false;
    if (!r || r.error || typeof r.price !== 'number') { s.error = true; return; }
    s.error = false;
    s.price = r.price;
    s.chg = r.chg || 0;
    s.history.push(r.price);
    if (s.history.length > 30) s.history.shift();
  }
  async function seed(sym) {
    if (state[sym]) return;
    state[sym] = { loading: true, price: 0, history: [], chg: 0 };
    render();
    var results = await fetchBatch([sym]);
    applyResult(sym, results[sym]);
    render();
    if (typeof Dash !== 'undefined') Dash.renderBento();
    if (state[sym] && !state[sym].error) {
      setTimeout(function () {
        if (!state[sym]) return;
        fetchBatch([sym]).then(function (r) {
          applyResult(sym, r[sym]);
          render();
          if (typeof Dash !== 'undefined') Dash.renderBento();
        });
      }, 6000);
    }
  }
  function add() {
    var input = document.getElementById('tickerInput');
    var sym = (input.value || '').trim().toUpperCase().replace(/[^A-Z0-9]/g, '');
    input.value = '';
    if (!sym || state[sym]) return;
    seed(sym);
  }
  function remove(sym) { delete state[sym]; render(); }
  function retry(sym) { delete state[sym]; seed(sym); }
  async function tick() {
    var syms = Object.keys(state).filter(function (s) { return !state[s].loading; });
    if (!syms.length) return;
    var results = await fetchBatch(syms);
    syms.forEach(function (sym) { applyResult(sym, results[sym]); });
    render();
    var u = document.getElementById('tickerUpdated');
    if (u) u.textContent = 'Updated just now';
    // The dashboard's Top Mover tile reads this same state — refresh it
    // the moment new prices actually land, instead of leaving it to catch
    // up whenever Dash's separate polling interval next happens to fire.
    if (typeof Dash !== 'undefined') Dash.renderBento();
  }
  function top() {
    var syms = Object.keys(state).filter(function (s) { return !state[s].loading && !state[s].error; });
    if (!syms.length) return null;
    var best = syms.reduce(function (a, b) { return Math.abs(state[b].chg) > Math.abs(state[a].chg) ? b : a; });
    return { sym: best, chg: state[best].chg };
  }
  function init() {
    seed('ETH'); seed('SOL');
    setInterval(tick, 15000);
  }
  return { init: init, add: add, remove: remove, retry: retry, top: top, topSparkSvg: topSparkSvg };
})();

// ── 2. GAS TRACKER — real public RPC + CoinGecko, multi-chain ────────────────
var Gas = (function () {
  var gwei = null, nativePrice = null, solanaFees = null;
  var GAS_UNITS = { transfer: 21000, swap: 150000, mint: 220000 };
  var SOLANA_CU = { transfer: 450, swap: 200000, mint: 300000 };
  var SOLANA_BASE_FEE_LAMPORTS = 5000;
  var CHAINS = {
    ethereum: { label: 'Ethereum mainnet', symbol: 'ETH' },
    bsc: { label: 'BNB Chain', symbol: 'BNB' },
    polygon: { label: 'Polygon', symbol: 'POL' },
    arbitrum: { label: 'Arbitrum One', symbol: 'ETH' },
    optimism: { label: 'Optimism', symbol: 'ETH' },
    base: { label: 'Base', symbol: 'ETH' },
    avalanche: { label: 'Avalanche C-Chain', symbol: 'AVAX' },
    robinhood: { label: 'Robinhood Chain', symbol: 'ETH' },
    solana: { label: 'Solana', symbol: 'SOL', isSolana: true }
  };
  var current = 'ethereum';

  async function fetchGasData(chain) {
    try {
      var res = await fetch(BACKEND_URL + '/toolkit/gas?chain=' + encodeURIComponent(chain));
      if (!res.ok) return { gwei: null, nativeUsd: null, solanaFees: null };
      var data = await res.json();
      return { gwei: data.gwei, nativeUsd: data.native_usd, solanaFees: data.solana_fees || null };
    } catch (e) { return { gwei: null, nativeUsd: null, solanaFees: null }; }
  }
  function fmtGwei(v) {
    if (v >= 10) return v.toFixed(1);
    if (v >= 1) return v.toFixed(2);
    if (v >= 0.01) return v.toFixed(4);
    return v.toFixed(6);
  }
  function render() {
    document.getElementById('gasChainLabel').textContent = CHAINS[current].label;
    document.getElementById('gasUnitWord').textContent = CHAINS[current].isSolana ? 'µlamports/CU' : 'gwei';
    if (CHAINS[current].isSolana) { renderSolana(); return; }
    renderEvm();
  }
  function renderEvm() {
    if (gwei == null) { document.getElementById('gasBig').textContent = '-'; document.getElementById('gasTiers').innerHTML = ''; document.getElementById('gasCosts').innerHTML = ''; return; }
    document.getElementById('gasBig').textContent = fmtGwei(gwei);
    var tiers = [
      { k: 'slow', label: 'Slow', mult: 0.85 },
      { k: 'avg', label: 'Average', mult: 1 },
      { k: 'fast', label: 'Fast', mult: 1.35 }
    ];
    document.getElementById('gasTiers').innerHTML = tiers.map(function (t) {
      return '<div class="gas-tier ' + t.k + '"><div class="tl">' + t.label + '</div><div class="tv">' + fmtGwei(gwei * t.mult) + ' gwei</div></div>';
    }).join('');
    var type = document.getElementById('gasTxType').value;
    var units = GAS_UNITS[type];
    var symbol = CHAINS[current].symbol;
    document.getElementById('gasCosts').innerHTML = tiers.map(function (t) {
      var costNative = (gwei * t.mult) * units / 1e9;
      var costUsd = nativePrice != null ? costNative * nativePrice : null;
      var usdTxt = costUsd == null ? null : (costUsd < 0.01 ? '<$0.01' : '$' + costUsd.toFixed(2));
      return '<div class="gas-cost"><div class="cl">' + t.label + '</div><div class="cv">' + (usdTxt != null ? usdTxt : costNative.toFixed(6) + ' ' + symbol) + '</div></div>';
    }).join('');
  }
  function renderSolana() {
    if (!solanaFees) { document.getElementById('gasBig').textContent = '-'; document.getElementById('gasTiers').innerHTML = ''; document.getElementById('gasCosts').innerHTML = ''; return; }
    document.getElementById('gasBig').textContent = solanaFees.avg.toLocaleString('en-US');
    var tiers = [
      { k: 'slow', label: 'Slow', v: solanaFees.slow },
      { k: 'avg', label: 'Average', v: solanaFees.avg },
      { k: 'fast', label: 'Fast', v: solanaFees.fast }
    ];
    document.getElementById('gasTiers').innerHTML = tiers.map(function (t) {
      return '<div class="gas-tier ' + t.k + '"><div class="tl">' + t.label + '</div><div class="tv">' + t.v.toLocaleString('en-US') + ' µ◎/CU</div></div>';
    }).join('');
    var type = document.getElementById('gasTxType').value;
    var cu = SOLANA_CU[type];
    document.getElementById('gasCosts').innerHTML = tiers.map(function (t) {
      var lamports = SOLANA_BASE_FEE_LAMPORTS + (t.v * cu / 1e6);
      var sol = lamports / 1e9;
      var costUsd = nativePrice != null ? sol * nativePrice : null;
      var usdTxt = costUsd == null ? null : (costUsd < 0.01 ? '<$0.01' : '$' + costUsd.toFixed(2));
      return '<div class="gas-cost"><div class="cl">' + t.label + '</div><div class="cv">' + (usdTxt != null ? usdTxt : sol.toFixed(6) + ' SOL') + '</div></div>';
    }).join('');
  }
  async function refresh() {
    var chain = current;
    var data = await fetchGasData(chain);
    if (chain !== current) return;
    if (data.gwei != null) gwei = data.gwei;
    if (data.nativeUsd != null) nativePrice = data.nativeUsd;
    solanaFees = data.solanaFees;
    render();
  }
  function switchChain() {
    current = document.getElementById('gasChain').value;
    gwei = null; nativePrice = null; solanaFees = null;
    render();
    refresh();
  }
  function init() { refresh(); setInterval(refresh, 20000); }
  return { init: init, render: render, switchChain: switchChain };
})();

// ── 3. WALLET CARD — real QR (branded download) + real ENS resolution ────────
var WalletCard = (function () {
  function short(addr) { return addr.length <= 14 ? addr : addr.slice(0, 6) + '…' + addr.slice(-4); }
  async function render() {
    var raw = (document.getElementById('walletInput').value || '').trim();
    if (!raw) return;
    var out = document.getElementById('wcardOut');
    var isEns = /\.eth$/i.test(raw);
    var isEvmHex = /^0x[a-fA-F0-9]{40}$/.test(raw);
    var addr = raw, ensName = '';
    if (isEns) {
      try {
        var res = await fetch('https://api.ensideas.com/ens/resolve/' + encodeURIComponent(raw));
        var data = await res.json();
        addr = (data && data.address) ? data.address : '';
        if (addr) ensName = raw;
      } catch (e) { addr = ''; }
    } else if (isEvmHex) {
      try {
        var res2 = await fetch('https://api.ensideas.com/ens/resolve/' + raw);
        var data2 = await res2.json();
        if (data2 && data2.name) ensName = data2.name;
      } catch (e) { }
    }
    if (!addr) {
      document.getElementById('wcardAddr').textContent = 'Could not resolve that address or ENS name';
      document.getElementById('wcardEns').textContent = '';
      out.style.display = 'block';
      return;
    }
    document.getElementById('wcardAddr').textContent = short(addr);
    document.getElementById('wcardAddr').dataset.full = addr;
    document.getElementById('wcardEns').textContent = ensName;
    document.getElementById('wcardQr').src = 'https://api.qrserver.com/v1/create-qr-code/?size=200x200&margin=8&data=' + encodeURIComponent(addr);
    out.style.display = 'block';
  }
  function copy() {
    var addr = document.getElementById('wcardAddr').dataset.full || '';
    if (!addr) return;
    navigator.clipboard.writeText(addr).then(function () {
      var btn = document.getElementById('wcardCopy');
      var orig = btn.textContent;
      btn.textContent = 'Copied ✓';
      setTimeout(function () { btn.textContent = orig; }, 1600);
    });
  }
  // Branded download: composes the QR onto a Dash HQ-styled card (wordmark +
  // dashhq.site + the full address) instead of handing back a bare,
  // anonymous QR PNG that carries no indication of what it's for or where
  // it came from. Height is measured then drawn (same two-pass approach as
  // XRay.download) so there's no leftover blank space below the content.
  function downloadQr() {
    var qrImgEl = document.getElementById('wcardQr');
    var addr = document.getElementById('wcardAddr').dataset.full || document.getElementById('wcardAddr').textContent || '';
    var ens = document.getElementById('wcardEns').textContent || '';
    if (!qrImgEl || !qrImgEl.src || !addr) return;
    var loader = new Image();
    loader.crossOrigin = 'anonymous';
    loader.onload = function () {
      var W = 560, padX = 48;
      function rr(ctx, px, py, w, h, r) { ctx.beginPath(); ctx.moveTo(px + r, py); ctx.arcTo(px + w, py, px + w, py + h, r); ctx.arcTo(px + w, py + h, px, py + h, r); ctx.arcTo(px, py + h, px, py, r); ctx.arcTo(px, py, px + w, py, r); ctx.closePath(); }
      function fitFont(x, weight, text, maxW, startPx) {
        var size = startPx;
        x.font = weight + ' ' + size + 'px "JetBrains Mono",monospace';
        while (x.measureText(text).width > maxW && size > 11) { size -= 1; x.font = weight + ' ' + size + 'px "JetBrains Mono",monospace'; }
        return size;
      }

      function paint(x, H, finalize) {
        if (finalize) {
          rr(x, 0, 0, W, H, 28); x.save(); x.clip();
          var g = x.createLinearGradient(0, 0, W, H); g.addColorStop(0, '#10204f'); g.addColorStop(0.55, '#0a1330'); g.addColorStop(1, '#0a1a3e');
          x.fillStyle = g; x.fillRect(0, 0, W, H);
          var sh = x.createLinearGradient(0, 0, W, H); sh.addColorStop(0.30, 'rgba(255,255,255,0)'); sh.addColorStop(0.45, 'rgba(145,190,255,0.18)'); sh.addColorStop(0.52, 'rgba(145,190,255,0.10)'); sh.addColorStop(0.62, 'rgba(255,255,255,0)');
          x.fillStyle = sh; x.fillRect(0, 0, W, H);
        }
        var dy = 58;
        x.textAlign = 'center';
        x.fillStyle = '#fff'; x.font = '800 27px Sora,sans-serif'; x.letterSpacing = '3px';
        x.fillText('DASH HQ', W / 2, dy); x.letterSpacing = '0px';
        dy += 26;
        x.fillStyle = '#5B9BF8'; x.font = '700 12px "JetBrains Mono",monospace'; x.letterSpacing = '2px';
        x.fillText('WALLET ID · DASHHQ.SITE', W / 2, dy); x.letterSpacing = '0px';
        dy += 40;
        var qrSize = 250, qrX = (W - qrSize) / 2;
        rr(x, qrX - 14, dy, qrSize + 28, qrSize + 28, 18); x.fillStyle = '#fff'; x.fill();
        if (finalize) x.drawImage(loader, qrX, dy + 14, qrSize, qrSize);
        dy += qrSize + 28 + 42;
        if (ens) {
          x.fillStyle = '#fff'; x.font = '700 18px "JetBrains Mono",monospace';
          x.fillText(ens, W / 2, dy);
          dy += 30;
        }
        var addrSize = fitFont(x, ens ? '400' : '700', addr, W - padX * 2, ens ? 14 : 16);
        x.fillStyle = ens ? '#8A9BBF' : '#E8EFFF';
        x.fillText(addr, W / 2, dy);
        dy += 44;
        x.strokeStyle = 'rgba(255,255,255,.1)'; x.lineWidth = 1;
        x.beginPath(); x.moveTo(padX, dy); x.lineTo(W - padX, dy); x.stroke();
        dy += 32;
        x.fillStyle = '#8A9BBF'; x.font = '400 11px "JetBrains Mono",monospace'; x.letterSpacing = '1px';
        x.fillText('SCAN TO VERIFY · DASH HQ CITIZEN PORTAL', W / 2, dy);
        x.letterSpacing = '0px';
        dy += 32;
        if (finalize) {
          x.restore();
          rr(x, 1, 1, W - 2, H - 2, 28); x.strokeStyle = 'rgba(255,255,255,0.16)'; x.lineWidth = 2; x.stroke();
        }
        return dy;
      }

      var probeCv = document.createElement('canvas'); probeCv.width = W; probeCv.height = 1200;
      var H = Math.ceil(paint(probeCv.getContext('2d'), 1200, false));

      var cv = document.createElement('canvas'); cv.width = W; cv.height = H;
      paint(cv.getContext('2d'), H, true);

      var a = document.createElement('a'); a.href = cv.toDataURL('image/png'); a.download = 'dash-hq-wallet-qr.png';
      document.body.appendChild(a); a.click(); a.remove();
    };
    loader.onerror = function () {
      // If the QR host ever blocks a cross-origin canvas read, fall back to
      // opening the bare QR so the user can still save something manually.
      window.open(qrImgEl.src, '_blank', 'noopener');
    };
    loader.src = qrImgEl.src;
  }
  return { render: render, copy: copy, downloadQr: downloadQr };
})();

// ── 4. DCA / PNL CALCULATOR — pure math, already real ─────────────────────────
var Pnl = (function () {
  var rowCount = 0;
  function addRow(amount, price) {
    rowCount++;
    var id = 'pnl-r' + rowCount;
    var div = document.createElement('div');
    div.className = 'pnl-row'; div.id = id;
    div.innerHTML = '<input type="number" placeholder="Amount bought" value="' + (amount != null ? amount : '') + '" oninput="Pnl.calc()">'
      + '<input type="number" placeholder="Price paid ($)" value="' + (price != null ? price : '') + '" oninput="Pnl.calc()">'
      + '<button class="rm" onclick="Pnl.removeRow(\'' + id + '\')" aria-label="Remove">×</button>';
    document.getElementById('pnlRows').appendChild(div);
    calc();
  }
  function removeRow(id) { var el = document.getElementById(id); if (el) el.remove(); calc(); }
  function calc() {
    var rows = document.querySelectorAll('#pnlRows .pnl-row');
    var totalUnits = 0, totalCost = 0;
    rows.forEach(function (r) {
      var inputs = r.querySelectorAll('input');
      var amt = parseFloat(inputs[0].value) || 0, price = parseFloat(inputs[1].value) || 0;
      totalUnits += amt; totalCost += amt * price;
    });
    var avgEntry = totalUnits > 0 ? totalCost / totalUnits : 0;
    var current = parseFloat(document.getElementById('pnlCurrent').value) || 0;
    var currentValue = totalUnits * current;
    var pnl = currentValue - totalCost;
    var pnlPct = totalCost > 0 ? (pnl / totalCost) * 100 : 0;
    var up = pnl >= 0;
    document.getElementById('pnlSummary').innerHTML =
      '<div class="pnl-stat"><div class="sl">Avg Entry</div><div class="sv">$' + avgEntry.toFixed(2) + '</div></div>'
      + '<div class="pnl-stat"><div class="sl">Total Invested</div><div class="sv">$' + totalCost.toFixed(2) + '</div></div>'
      + '<div class="pnl-stat hero"><div class="sl">Profit / Loss</div><div class="sv ' + (up ? 'up' : 'down') + '">' + (up ? '+' : '') + '$' + pnl.toFixed(2) + ' (' + (up ? '+' : '') + pnlPct.toFixed(1) + '%)</div></div>';
  }
  function init() { addRow(1, 2800); addRow(0.5, 3400); }
  return { init: init, addRow: addRow, removeRow: removeRow, calc: calc };
})();

// ── 5. APE MATH CALCULATOR — pure math, already real ──────────────────────────
var Ape = (function () {
  function fmt(n) { return '$' + Math.round(n).toLocaleString('en-US'); }
  function syncFromNum() {
    var num = document.getElementById('apeTargetNum');
    document.getElementById('apeTarget').value = num.value;
    calc();
  }
  function calc() {
    var amount = parseFloat(document.getElementById('apeAmount').value) || 0;
    var curMc = parseFloat(document.getElementById('apeCurrentMc').value) || 1;
    var target = parseFloat(document.getElementById('apeTarget').value) || 0;
    document.getElementById('apeTargetNum').value = target;
    document.getElementById('apeTargetLabel').textContent = Math.round(target).toLocaleString('en-US');
    var mult = curMc > 0 ? target / curMc : 0;
    document.getElementById('apeMult').textContent = mult.toFixed(1) + 'x';
    document.getElementById('apeValue').textContent = '→ ' + fmt(amount * mult);
  }
  return { calc: calc, syncFromNum: syncFromNum };
})();

// ── 6. NEW PAIR SCANNER — real GeckoTerminal data ─────────────────────────────
var Pairs = (function () {
  var cache = {};

  async function fetchNetwork(net) {
    try {
      var res = await fetch('https://api.geckoterminal.com/api/v2/networks/' + net + '/new_pools?page=1');
      var data = await res.json();
      return (data.data || []).map(function (p) {
        var a = p.attributes;
        var poolAddr = (a.address || p.id.split('_').pop());
        var baseTokenId = ((p.relationships || {}).base_token || {}).data && p.relationships.base_token.data.id;
        var tokenAddr = baseTokenId && baseTokenId.indexOf(net + '_') === 0 ? baseTokenId.slice(net.length + 1) : null;
        return {
          name: a.name,
          liq: parseFloat(a.reserve_in_usd) || 0,
          born: new Date(a.pool_created_at).getTime(),
          url: 'https://www.geckoterminal.com/' + net + '/pools/' + poolAddr,
          tokenAddr: tokenAddr
        };
      });
    } catch (e) { return []; }
  }
  function render() {
    var chain = document.getElementById('pairsChain').value;
    var age = parseInt(document.getElementById('pairsAge').value, 10);
    var minLiq = parseInt(document.getElementById('pairsLiq').value, 10);
    document.getElementById('pairsLiqLabel').textContent = minLiq.toLocaleString('en-US');
    var now = Date.now();
    var list = (cache[chain] || []).filter(function (p) {
      var ageMin = Math.floor((now - p.born) / 60000);
      if (age > 0 && ageMin > age) return false;
      if (p.liq < minLiq) return false;
      return true;
    }).slice(0, 30);
    document.getElementById('pairsFeed').innerHTML = list.map(function (p) {
      var ageMin = Math.floor((now - p.born) / 60000);
      var ageTxt = ageMin < 1 ? 'now' : ageMin + 'm ago';
      var fresh = ageMin < 10;
      var copyBtn = p.tokenAddr
        ? '<button class="pair-copy" title="Copy contract address" aria-label="Copy contract address" onclick="Pairs.copyAddress(event,\'' + p.tokenAddr + '\')"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg></button>'
        : '';
      return '<a href="' + p.url + '" target="_blank" rel="noopener" class="pair-row' + (fresh ? ' fresh' : '') + '"><span class="pair-chain">' + chain.toUpperCase() + '</span><span class="pair-name">' + p.name + '</span><span class="pair-liq">$' + Math.round(p.liq).toLocaleString('en-US') + '</span><span class="pair-age">' + ageTxt + '</span>' + copyBtn + '</a>';
    }).join('') || '<div class="pin-empty">No pairs match these filters right now.</div>';
  }
  function copyAddress(ev, addr) {
    ev.preventDefault();
    ev.stopPropagation();
    var btn = ev.currentTarget;
    navigator.clipboard.writeText(addr).then(function () {
      var orig = btn.innerHTML;
      btn.classList.add('copied');
      btn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>';
      setTimeout(function () { btn.classList.remove('copied'); btn.innerHTML = orig; }, 1400);
    });
  }
  async function refresh() {
    var chain = document.getElementById('pairsChain').value;
    cache[chain] = await fetchNetwork(chain);
    render();
  }
  var ageTicker = null;
  function onEnter() {
    // The "Xm ago" labels only updated on a full network refetch (every
    // 45s) or a filter change, so they visibly sat frozen for most of
    // that window even though the underlying data was fine, that read as
    // "not refreshing" even when it was. A cheap local re-render (no
    // network call, just reformats the already-cached list) keeps them
    // visibly live without adding any extra load. Also refresh
    // immediately on entering the page instead of waiting for whatever
    // point the background interval happens to be at.
    refresh();
    if (ageTicker) clearInterval(ageTicker);
    ageTicker = setInterval(render, 15000);
  }
  function onLeave() {
    if (ageTicker) { clearInterval(ageTicker); ageTicker = null; }
  }
  function init() {
    refresh();
    setInterval(refresh, 45000);
  }
  return { init: init, render: render, refresh: refresh, copyAddress: copyAddress, onEnter: onEnter, onLeave: onLeave };
})();

// ── 7. CA SCANNER — real DexScreener data, chain auto-detected ───────────────
var CaScan = (function () {
  var pairs = [];
  var current = null;

  var CHAIN_LABELS = {
    ethereum: 'Ethereum', bsc: 'BNB Chain', polygon: 'Polygon', arbitrum: 'Arbitrum',
    optimism: 'Optimism', base: 'Base', avalanche: 'Avalanche', solana: 'Solana', robinhood: 'Robinhood Chain'
  };
  function chainLabel(id) {
    if (CHAIN_LABELS[id]) return CHAIN_LABELS[id];
    return id.charAt(0).toUpperCase() + id.slice(1).replace(/-/g, ' ');
  }
  function fmtPrice(n) {
    if (n == null || isNaN(n)) return '-';
    if (n >= 1) return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (n >= 0.01) return '$' + n.toFixed(4);
    if (n >= 0.000001) return '$' + n.toFixed(8).replace(/0+$/, '').replace(/\.$/, '');
    return '$' + n.toExponential(2);
  }
  function fmtCompact(n) {
    if (n == null || isNaN(n)) return '-';
    if (n >= 1e9) return '$' + (n / 1e9).toFixed(2) + 'B';
    if (n >= 1e6) return '$' + (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return '$' + (n / 1e3).toFixed(1) + 'K';
    return '$' + Math.round(n).toLocaleString('en-US');
  }
  function fmtAge(ms) {
    if (!ms) return null;
    var diff = Date.now() - ms;
    var mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + 'm ago';
    var hours = Math.floor(mins / 60);
    if (hours < 24) return hours + 'h ago';
    var days = Math.floor(hours / 24);
    if (days < 30) return days + 'd ago';
    var months = Math.floor(days / 30);
    if (months < 12) return months + 'mo ago';
    return Math.floor(months / 12) + 'y ago';
  }
  function bestPairPerChain(list) {
    var byChain = {};
    list.forEach(function (p) {
      var liq = (p.liquidity && p.liquidity.usd) || 0;
      var existing = byChain[p.chainId];
      if (!existing || liq > ((existing.liquidity && existing.liquidity.usd) || 0)) byChain[p.chainId] = p;
    });
    return byChain;
  }

  async function check() {
    var raw = (document.getElementById('scanInput').value || '').trim();
    if (!raw) return;
    var out = document.getElementById('scanOut');
    var empty = document.getElementById('scanEmpty');
    out.style.display = 'none';
    empty.style.display = 'none';
    var btn = document.querySelector('[data-page="scan"] .tk-addrow .btn');
    var origLabel = btn.textContent;
    btn.textContent = 'Scanning…'; btn.disabled = true;
    try {
      var res = await fetch('https://api.dexscreener.com/latest/dex/tokens/' + encodeURIComponent(raw));
      if (!res.ok) throw new Error('lookup failed');
      var data = await res.json();
      pairs = data.pairs || [];
      if (!pairs.length) {
        empty.textContent = 'No pools found for that address on any indexed chain.';
        empty.style.display = 'block';
        return;
      }
      var byChain = bestPairPerChain(pairs);
      var best = pairs.reduce(function (a, b) {
        return ((b.liquidity && b.liquidity.usd) || 0) > ((a.liquidity && a.liquidity.usd) || 0) ? b : a;
      });
      render(best, byChain);
    } catch (e) {
      empty.textContent = 'Could not reach the scanner right now. Try again in a moment.';
      empty.style.display = 'block';
    } finally {
      btn.textContent = origLabel; btn.disabled = false;
    }
  }

  function render(pair, byChain) {
    current = pair;
    var out = document.getElementById('scanOut');
    document.getElementById('scanEmpty').style.display = 'none';

    var info = pair.info || {};
    var logo = document.getElementById('scanLogo');
    logo.src = info.imageUrl || '';
    logo.alt = pair.baseToken.symbol || '';
    document.getElementById('scanName').textContent = pair.baseToken.name || 'Unknown token';
    document.getElementById('scanSym').textContent = pair.baseToken.symbol || '';
    document.getElementById('scanChainBadge').textContent = chainLabel(pair.chainId).toUpperCase();
    document.getElementById('scanDexBadge').textContent = pair.dexId || '';

    var price = parseFloat(pair.priceUsd);
    document.getElementById('scanPrice').textContent = fmtPrice(price);
    var pc = pair.priceChange || {};
    var chgEl = document.getElementById('scanChg24');
    if (pc.h24 != null) {
      var up24 = pc.h24 >= 0;
      chgEl.className = 'scan-chg-24 ' + (up24 ? 'up' : 'down');
      chgEl.textContent = (up24 ? '▲ ' : '▼ ') + Math.abs(pc.h24).toFixed(2) + '% (24h)';
    } else {
      chgEl.className = 'scan-chg-24'; chgEl.textContent = '';
    }

    var tfOrder = [['m5', '5m'], ['h1', '1h'], ['h6', '6h'], ['h24', '24h']];
    document.getElementById('scanTfRow').innerHTML = tfOrder.filter(function (t) { return pc[t[0]] != null; }).map(function (t) {
      var v = pc[t[0]], up = v >= 0;
      return '<div class="scan-tf"><div class="l">' + t[1] + '</div><div class="v ' + (up ? 'up' : 'down') + '">' + (up ? '+' : '') + v.toFixed(1) + '%</div></div>';
    }).join('');

    var txns24 = (pair.txns && pair.txns.h24) || {};
    var buys = txns24.buys || 0, sells = txns24.sells || 0, total = buys + sells;
    var buyPct = total > 0 ? (buys / total) * 100 : 50;
    document.getElementById('scanPressureBuy').style.width = buyPct + '%';
    document.getElementById('scanPressureSell').style.width = (100 - buyPct) + '%';
    document.getElementById('scanPressureLabel').innerHTML = total > 0
      ? '<span>' + buys + ' buys (' + buyPct.toFixed(0) + '%)</span><span>' + sells + ' sells (' + (100 - buyPct).toFixed(0) + '%)</span>'
      : '<span>No trades in the last 24h</span><span></span>';

    document.getElementById('scanStats').innerHTML =
      '<div class="st"><div class="sl">Market Cap</div><div class="sv">' + fmtCompact(pair.marketCap || pair.fdv) + '</div></div>'
      + '<div class="st"><div class="sl">Liquidity</div><div class="sv">' + fmtCompact(pair.liquidity && pair.liquidity.usd) + '</div></div>'
      + '<div class="st"><div class="sl">24h Volume</div><div class="sv">' + fmtCompact(pair.volume && pair.volume.h24) + '</div></div>';

    var age = fmtAge(pair.pairCreatedAt);
    document.getElementById('scanMeta').textContent = age ? 'Pair created ' + age : '';

    var links = [];
    (info.websites || []).slice(0, 1).forEach(function (w) {
      links.push('<a href="' + w.url + '" target="_blank" rel="noopener"><svg viewBox="0 0 24 24" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18"/></svg>' + (w.label || 'Website') + '</a>');
    });
    (info.socials || []).slice(0, 2).forEach(function (s) {
      links.push('<a href="' + s.url + '" target="_blank" rel="noopener"><svg viewBox="0 0 24 24" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M8 12h8M12 8v8"/></svg>' + s.type + '</a>');
    });
    links.push('<a href="' + pair.url + '" target="_blank" rel="noopener"><svg viewBox="0 0 24 24" stroke-linecap="round"><path d="M7 17L17 7M7 7h10v10"/></svg>DexScreener</a>');
    document.getElementById('scanLinks').innerHTML = links.join('');

    var otherChains = Object.keys(byChain).filter(function (c) { return c !== pair.chainId; });
    var ocEl = document.getElementById('scanOtherChains');
    ocEl.innerHTML = otherChains.length
      ? '<div class="ocl">Also trading on</div><div class="scan-otherchains-row">' + otherChains.map(function (c) {
          return '<button class="scan-chain-pill" onclick="CaScan.switchChain(\'' + c + '\')">' + chainLabel(c) + '</button>';
        }).join('') + '</div>'
      : '';

    document.getElementById('scanMoreBody').innerHTML = buildMoreDetails(pair);

    out.style.display = 'block';
  }

  function fmtNative(n) {
    if (n == null || isNaN(n)) return '-';
    if (n >= 1) return n.toLocaleString('en-US', { maximumFractionDigits: 6 });
    if (n >= 0.000001) return n.toFixed(10).replace(/0+$/, '').replace(/\.$/, '');
    return n.toExponential(2);
  }
  function shortAddr(a) { return a && a.length > 14 ? a.slice(0, 6) + '…' + a.slice(-4) : (a || '-'); }
  function copySvg() { return '<svg viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M4 16V5a1 1 0 0 1 1-1h11"/></svg>'; }

  function buildMoreDetails(pair) {
    var quote = pair.quoteToken || {};
    var dexLabel = (pair.dexId || '-') + (pair.labels && pair.labels.length ? ' · ' + pair.labels.join('/').toUpperCase() : '');
    var mcap = pair.marketCap, fdv = pair.fdv;

    var pairInfo = '<div class="scan-more-section"><div class="msl">Pair Info</div>'
      + '<div class="scan-detail-row"><span class="dl">Paired with</span><span class="dv">' + (quote.symbol || '-') + '</span></div>'
      + '<div class="scan-detail-row"><span class="dl">Price (native)</span><span class="dv">' + fmtNative(parseFloat(pair.priceNative)) + ' ' + (quote.symbol || '') + '</span></div>'
      + '<div class="scan-detail-row"><span class="dl">DEX</span><span class="dv">' + dexLabel + '</span></div>'
      + '<div class="scan-detail-row"><span class="dl">Token contract</span><span class="dv copyable" onclick="CaScan.copyAddress()">' + shortAddr(pair.baseToken.address) + copySvg() + '</span></div>'
      + '<div class="scan-detail-row"><span class="dl">Pair (LP) address</span><span class="dv copyable" onclick="CaScan.copyPairAddress(event)">' + shortAddr(pair.pairAddress) + copySvg() + '</span></div>'
      + '</div>';

    var valuation = '<div class="scan-more-section"><div class="msl">Valuation</div>'
      + '<div class="scan-detail-row"><span class="dl">Market Cap</span><span class="dv">' + fmtCompact(mcap) + '</span></div>'
      + (fdv != null && fdv !== mcap ? '<div class="scan-detail-row"><span class="dl">Fully Diluted Valuation</span><span class="dv">' + fmtCompact(fdv) + '</span></div>' : '')
      + '</div>';

    var tfKeys = [['m5', '5M'], ['h1', '1H'], ['h6', '6H'], ['h24', '24H']];
    var vol = pair.volume || {}, txns = pair.txns || {};
    function row(label, fn) {
      return '<tr><td>' + label + '</td>' + tfKeys.map(function (t) { return '<td>' + fn(t[0]) + '</td>'; }).join('') + '</tr>';
    }
    var mtf = '<div class="scan-more-section"><div class="msl">Volume &amp; Trades by Timeframe</div>'
      + '<div class="scan-mtf-wrap"><table class="scan-mtf-table"><thead><tr><th></th>' + tfKeys.map(function (t) { return '<th>' + t[1] + '</th>'; }).join('') + '</tr></thead><tbody>'
      + row('Volume', function (k) { return fmtCompact(vol[k]); })
      + row('Buys', function (k) { return (txns[k] && txns[k].buys != null) ? txns[k].buys : '-'; })
      + row('Sells', function (k) { return (txns[k] && txns[k].sells != null) ? txns[k].sells : '-'; })
      + '</tbody></table></div></div>';

    return pairInfo + valuation + mtf;
  }

  function switchChain(chainId) {
    var byChain = bestPairPerChain(pairs);
    var pair = byChain[chainId];
    if (pair) render(pair, byChain);
  }

  function copyAddress() {
    if (!current) return;
    navigator.clipboard.writeText(current.baseToken.address).then(function () {
      var btn = document.getElementById('scanCopyBtn');
      btn.classList.add('copied');
      setTimeout(function () { btn.classList.remove('copied'); }, 1600);
    });
  }

  function copyPairAddress(ev) {
    if (!current || !current.pairAddress) return;
    navigator.clipboard.writeText(current.pairAddress).then(function () {
      var el = ev.currentTarget;
      var orig = el.innerHTML;
      el.innerHTML = 'Copied ✓';
      setTimeout(function () { el.innerHTML = orig; }, 1600);
    });
  }

  return { check: check, switchChain: switchChain, copyAddress: copyAddress, copyPairAddress: copyPairAddress };
})();

// ── 8. RUG RISK CHECKER — real backend-proxied honeypot.is / rugcheck.xyz ────
var Rug = (function () {
  async function check() {
    var addr = (document.getElementById('rugInput').value || '').trim();
    if (!addr) return;
    var out = document.getElementById('rugOut');
    var badge = document.getElementById('rugBadge');
    badge.className = 'rug-badge'; badge.textContent = 'Checking…';
    document.getElementById('rugChecklist').innerHTML = '';
    out.style.display = 'block';
    var chainId = document.getElementById('rugChain').value;
    try {
      var res = await fetch(BACKEND_URL + '/toolkit/rug-check', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ address: addr, chain_id: chainId })
      });
      if (!res.ok) throw new Error('check failed');
      var data = await res.json();
      badge.className = 'rug-badge ' + data.level;
      badge.textContent = data.label;
      document.getElementById('rugChecklist').innerHTML = data.checks.map(function (c) {
        return '<div class="rug-item ' + (c.pass ? 'pass' : 'fail') + '"><span class="ric">' + (c.pass ? '✓' : '✕') + '</span>' + c.label + '</div>';
      }).join('');
    } catch (e) {
      badge.className = 'rug-badge high';
      badge.textContent = 'Could not check this address right now';
    }
  }
  return { check: check };
})();

// ── 9. PRICE IMPACT / SLIPPAGE ESTIMATOR — real GeckoTerminal pool depth ─────
var Slip = (function () {
  var poolCache = {};
  var debounceTimer = null;

  async function fetchPoolLiquidity(network, query) {
    var key = network + ':' + query.toLowerCase();
    if (poolCache[key] != null) return poolCache[key];
    try {
      var res = await fetch('https://api.geckoterminal.com/api/v2/search/pools?query=' + encodeURIComponent(query) + '&network=' + network);
      var data = await res.json();
      var pool = (data.data || [])[0];
      if (!pool) return null;
      var liq = parseFloat(pool.attributes.reserve_in_usd) || 0;
      poolCache[key] = liq;
      return liq;
    } catch (e) { return null; }
  }
  function calc() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(run, 350);
  }
  async function run() {
    var network = document.getElementById('slipChain').value;
    var query = (document.getElementById('slipQuery').value || '').trim();
    var amountIn = parseFloat(document.getElementById('slipAmount').value) || 0;
    var resultEl = document.getElementById('slipResult');
    if (!query) { resultEl.innerHTML = '<div class="pin-empty">Enter a pair to check, e.g. ETH/USDC.</div>'; return; }
    resultEl.innerHTML = '<div class="pin-empty">Loading pool data…</div>';
    var liq = await fetchPoolLiquidity(network, query);
    if (liq == null) { resultEl.innerHTML = '<div class="pin-empty">Could not find a pool for that pair on this chain.</div>'; return; }
    var halfDepth = liq / 2;
    var impact = halfDepth > 0 ? (amountIn / (amountIn + halfDepth)) * 100 : 0;
    var level = impact < 1 ? 'low' : impact < 5 ? 'medium' : 'high';
    var color = level === 'low' ? '#10B981' : level === 'medium' ? '#F59E0B' : '#EF4444';
    var html = '<div class="slip-stat"><div class="sl">Est. Price Impact</div><div class="sv" style="color:' + color + '">' + impact.toFixed(2) + '%</div></div>'
      + '<div class="slip-stat"><div class="sl">Pool Liquidity</div><div class="sv">$' + Math.round(liq).toLocaleString('en-US') + '</div></div>';
    if (impact >= 5) {
      html += '<div class="slip-warn" style="background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.35);color:#EF4444">⚠ High impact. This trade will move the price significantly. Consider splitting it into smaller trades.</div>';
    } else if (impact >= 1) {
      html += '<div class="slip-warn" style="background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.35);color:#F59E0B">Moderate impact, worth keeping an eye on for larger size.</div>';
    }
    resultEl.innerHTML = html;
  }
  return { calc: calc };
})();

// ── 10. NFT WATCHLIST — real OpenSea search/collection/discover data ─────────
var Watchlist = (function () {
  var cache = {};       // slug -> collection shape from the backend
  var watchSlugs = [];  // persisted list of watched slugs
  var searchTimer = null;
  var currentTab = 'search';
  var currentSub = 'trending';
  var discoverLoaded = { trending: false, new: false };

  function load() {
    try { watchSlugs = JSON.parse(localStorage.getItem('dashhq_nft_watchlist') || '[]'); }
    catch (e) { watchSlugs = []; }
  }
  function save() {
    try { localStorage.setItem('dashhq_nft_watchlist', JSON.stringify(watchSlugs)); } catch (e) { }
  }
  function fmtCompact(n) {
    if (n == null || isNaN(n)) return '-';
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return (Math.round(n * 100) / 100).toString();
  }
  function thumbHtml(c) {
    if (c.image) return '<img class="nft-thumb" src="' + c.image + '" alt="" onerror="this.outerHTML=\'<span class=&quot;nft-thumb ph&quot;>' + ((c.name || '?')[0] || '?').toUpperCase() + '</span>\'">';
    return '<span class="nft-thumb ph">' + ((c.name || '?')[0] || '?').toUpperCase() + '</span>';
  }
  // OpenSea's own editorial verification (safelist_status === "verified"),
  // not a self-reported badge — only render it when the backend confirms it.
  function verifiedBadge(c) {
    if (!c.verified) return '';
    return '<svg class="nft-verified" viewBox="0 0 24 24" title="OpenSea Verified"><path d="M12 2l2.4 1.3 2.7-.4 1.3 2.4 2.4 1.3-.4 2.7 1.3 2.4-1.3 2.4.4 2.7-2.4 1.3-1.3 2.4-2.7-.4L12 22l-2.4-1.3-2.7.4-1.3-2.4-2.4-1.3.4-2.7L2.3 12l1.3-2.4-.4-2.7 2.4-1.3 1.3-2.4 2.7.4z" fill="#5B9BF8"/><path d="M8.5 12.3l2.4 2.4 4.8-4.8" stroke="#fff" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  }
  function shortAddr(a) { return a && a.length > 14 ? a.slice(0, 6) + '…' + a.slice(-4) : (a || ''); }

  function updateCount() {
    var el = document.getElementById('nftWatchCount');
    if (el) el.textContent = watchSlugs.length;
  }

  function tab(t) {
    currentTab = t;
    document.querySelectorAll('.nft-tab[data-nft-tab]').forEach(function (b) { b.classList.toggle('active', b.dataset.nftTab === t); });
    document.getElementById('nftPaneSearch').classList.toggle('on', t === 'search');
    document.getElementById('nftPaneWatch').classList.toggle('on', t === 'watch');
    document.getElementById('nftPaneDiscover').classList.toggle('on', t === 'discover');
    if (t === 'watch') renderWatchlist();
    if (t === 'discover' && !discoverLoaded[currentSub]) refreshDiscover(currentSub);
  }
  function subTab(t) {
    currentSub = t;
    document.querySelectorAll('.nft-tab[data-nft-sub]').forEach(function (b) { b.classList.toggle('active', b.dataset.nftSub === t); });
    var note = document.getElementById('nftNewNote');
    if (note) note.style.display = t === 'new' ? 'block' : 'none';
    if (!discoverLoaded[t]) refreshDiscover(t); else renderDiscover(t);
  }

  async function search(q) {
    q = (q || '').trim();
    clearTimeout(searchTimer);
    var el = document.getElementById('nftSearchResults');
    if (q.length < 2) { el.innerHTML = ''; return; }
    searchTimer = setTimeout(async function () {
      el.innerHTML = '<div class="nft-empty-msg">Searching…</div>';
      try {
        var res = await fetch(BACKEND_URL + '/toolkit/nft-search?q=' + encodeURIComponent(q));
        if (!res.ok) throw new Error('search failed');
        var data = await res.json();
        var results = data.results || [];
        // Marked partial: search results are OpenSea's lighter list shape,
        // missing verified/category/description/socials - only the
        // single-collection endpoint has those (see Watchlist.add). Don't
        // let a repeat search downgrade an entry already upgraded to full.
        results.forEach(function (c) { if (c.slug && (!cache[c.slug] || cache[c.slug]._partial)) { c._partial = true; cache[c.slug] = c; } });
        if (!results.length) { el.innerHTML = '<div class="nft-empty-msg">No collections found for that search.</div>'; return; }
        el.innerHTML = results.map(function (c) {
          var added = watchSlugs.indexOf(c.slug) !== -1;
          return '<div class="nft-result-row' + (added ? ' added' : '') + '" onclick="' + (added ? '' : "Watchlist.add('" + c.slug + "')") + '">'
            + thumbHtml(c)
            + '<div class="nft-result-info"><div class="nft-result-name"><span>' + c.name + '</span>' + verifiedBadge(c) + '</div><div class="nft-result-stat">Floor ' + (c.floor != null ? fmtCompact(c.floor) + ' ' + (c.symbol || 'ETH') : '-') + '</div></div>'
            + '<div class="nft-result-add">' + (added ? 'Watching ✓' : '+ Watch') + '</div>'
            + '</div>';
        }).join('');
      } catch (e) {
        el.innerHTML = '<div class="nft-empty-msg">Could not reach OpenSea right now. Try again in a moment.</div>';
      }
    }, 350);
  }

  async function add(slug) {
    if (!slug || watchSlugs.indexOf(slug) !== -1) return;
    watchSlugs.push(slug);
    save();
    updateCount();
    if (currentTab === 'search') search(document.getElementById('nftSearchInput').value);
    if (currentTab === 'watch') renderWatchlist();
    if (typeof Dash !== 'undefined') Dash.renderBento();
    // Search/Discover results come from OpenSea's lighter list endpoints,
    // which don't include verified status, category, description, or
    // socials — only the single-collection endpoint does. Fetch and
    // upgrade the cache entry to the full shape right away, so a card
    // never sits in the watchlist half-populated until some later,
    // unrelated re-render happens to refresh it.
    try {
      var res = await fetch(BACKEND_URL + '/toolkit/nft-collection?slug=' + encodeURIComponent(slug));
      if (res.ok) {
        cache[slug] = await res.json();
        if (currentTab === 'watch') renderWatchlist();
        if (typeof Dash !== 'undefined') Dash.renderBento();
      }
    } catch (e) { }
  }
  function remove(slug) {
    watchSlugs = watchSlugs.filter(function (s) { return s !== slug; });
    save();
    updateCount();
    renderWatchlist();
    if (typeof Dash !== 'undefined') Dash.renderBento();
  }

  function watchCardHtml(c) {
    var tags = '';
    if (c.category || c.chain) {
      tags = '<div class="nft-watch-tags">'
        + (c.category ? '<span class="nft-watch-tag">' + c.category + '</span>' : '')
        + (c.chain ? '<span class="nft-watch-tag">' + c.chain + '</span>' : '')
        + '</div>';
    }
    var desc = c.description ? '<div class="nft-watch-desc">' + c.description + '</div>' : '';
    var contract = c.contractAddress
      ? '<div class="nft-watch-contract" onclick="Watchlist.copyContract(event,\'' + c.contractAddress + '\')" title="Copy contract address">'
        + '<svg viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M4 16V5a1 1 0 0 1 1-1h11"/></svg>' + shortAddr(c.contractAddress) + '</div>'
      : '';
    var socials = [];
    if (c.website) socials.push('<a href="' + c.website + '" target="_blank" rel="noopener" title="Website" onclick="event.stopPropagation()"><svg viewBox="0 0 24 24" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18"/></svg></a>');
    if (c.twitter) socials.push('<a href="https://x.com/' + c.twitter + '" target="_blank" rel="noopener" title="X / Twitter" onclick="event.stopPropagation()"><svg viewBox="0 0 24 24"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg></a>');
    if (c.discord) socials.push('<a href="' + c.discord + '" target="_blank" rel="noopener" title="Discord" onclick="event.stopPropagation()"><svg viewBox="0 0 24 24"><path d="M20.317 4.369a19.79 19.79 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.865-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.74 19.74 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.058a.082.082 0 0 0 .031.057 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 0 0-.041-.106 13.1 13.1 0 0 1-1.872-.892.077.077 0 0 1-.008-.128c.126-.094.252-.192.372-.291a.074.074 0 0 1 .078-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .079.009c.12.099.245.198.372.292a.077.077 0 0 1-.006.127c-.598.35-1.22.645-1.873.891a.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.84 19.84 0 0 0 6.002-3.03.077.077 0 0 0 .032-.056c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.028zM8.02 15.331c-1.183 0-2.157-1.086-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.332-.955 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.086-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.332-.946 2.418-2.157 2.418z"/></svg></a>');
    return '<div class="nft-watch-card">'
      + '<button class="rm" onclick="Watchlist.remove(\'' + c.slug + '\')" aria-label="Remove">×</button>'
      + '<div class="nft-watch-top">' + thumbHtml(c) + '<div class="nft-watch-name"><span>' + c.name + '</span>' + verifiedBadge(c) + '</div></div>'
      + tags + desc
      + '<div class="nft-watch-stats">'
      + '<div class="nft-watch-stat"><div class="lbl">Floor</div><div class="val' + (c.floor == null ? ' zero' : '') + '">' + (c.floor != null ? fmtCompact(c.floor) + ' ' + (c.symbol || 'ETH') : '-') + '</div></div>'
      + '<div class="nft-watch-stat"><div class="lbl">24h Vol</div><div class="val' + (c.vol1d == null ? ' zero' : '') + '">' + (c.vol1d != null ? fmtCompact(c.vol1d) + ' ' + (c.symbol || 'ETH') : '-') + '</div></div>'
      + '<div class="nft-watch-stat"><div class="lbl">24h Sales</div><div class="val' + (c.sales24h == null ? ' zero' : '') + '">' + (c.sales24h != null ? c.sales24h : '-') + '</div></div>'
      + '<div class="nft-watch-stat"><div class="lbl">Owners</div><div class="val' + (c.owners == null ? ' zero' : '') + '">' + (c.owners != null ? c.owners.toLocaleString('en-US') : '-') + '</div></div>'
      + '</div>'
      + contract
      + '<div class="nft-watch-foot">'
      + (socials.length ? '<div class="nft-watch-socials">' + socials.join('') + '</div>' : '<span></span>')
      + '<a class="nft-watch-link" href="' + (c.openseaUrl || ('https://opensea.io/collection/' + c.slug)) + '" target="_blank" rel="noopener"><svg viewBox="0 0 24 24" stroke-linecap="round"><path d="M7 17L17 7M7 7h10v10"/></svg>OpenSea</a>'
      + '</div>'
      + '</div>';
  }
  function copyContract(ev, addr) {
    ev.stopPropagation();
    navigator.clipboard.writeText(addr).then(function () {
      var el = ev.currentTarget;
      var orig = el.innerHTML;
      el.innerHTML = 'Copied ✓';
      setTimeout(function () { el.innerHTML = orig; }, 1400);
    });
  }

  async function renderWatchlist() {
    var grid = document.getElementById('nftWatchGrid');
    if (!watchSlugs.length) { grid.innerHTML = '<div class="pin-empty">No collections watched yet. Search or discover one to add it here.</div>'; return; }
    // Entries cached from a search/discover result carry a leaner shape
    // (no verified/category/description/etc., flagged _partial when
    // cached) - re-fetch the full detail endpoint for anything that
    // hasn't already been upgraded to the rich shape, not just entries
    // missing from the cache entirely.
    var missing = watchSlugs.filter(function (s) { return !cache[s] || cache[s]._partial; });
    if (missing.length) {
      grid.innerHTML = '<div class="nft-empty-msg">Loading watchlist…</div>';
      await Promise.all(missing.map(async function (slug) {
        try {
          var res = await fetch(BACKEND_URL + '/toolkit/nft-collection?slug=' + encodeURIComponent(slug));
          if (res.ok) cache[slug] = await res.json();
        } catch (e) { }
      }));
    }
    grid.innerHTML = watchSlugs.map(function (slug) {
      var c = cache[slug] || { slug: slug, name: slug };
      return watchCardHtml(c);
    }).join('');
  }

  async function refreshDiscover(tabName) {
    var listEl = document.getElementById('nftDiscoverList');
    if (currentSub === tabName) listEl.innerHTML = '<div class="nft-empty-msg">Loading…</div>';
    try {
      var res = await fetch(BACKEND_URL + '/toolkit/nft-discover?tab=' + encodeURIComponent(tabName));
      if (!res.ok) throw new Error('discover failed');
      var data = await res.json();
      var results = data.results || [];
      results.forEach(function (c) { if (c.slug && (!cache[c.slug] || cache[c.slug]._partial)) { c._partial = true; cache[c.slug] = c; } });
      discoverLoaded[tabName] = results;
      if (currentSub === tabName) renderDiscover(tabName);
    } catch (e) {
      if (currentSub === tabName) listEl.innerHTML = '<div class="nft-empty-msg">Could not reach OpenSea right now. Try again in a moment.</div>';
    }
  }
  function renderDiscover(tabName) {
    var results = discoverLoaded[tabName];
    var listEl = document.getElementById('nftDiscoverList');
    if (!results || !results.length) { listEl.innerHTML = '<div class="nft-empty-msg">Nothing to show right now.</div>'; return; }
    listEl.innerHTML = results.map(function (c) {
      var added = watchSlugs.indexOf(c.slug) !== -1;
      var statTxt = tabName === 'trending'
        ? '7d Vol ' + (c.vol7d != null ? fmtCompact(c.vol7d) + ' ' + (c.symbol || 'ETH') : '-')
        : (c.floor != null ? 'Floor ' + fmtCompact(c.floor) + ' ' + (c.symbol || 'ETH') : 'Newly listed');
      return '<div class="nft-discover-row">'
        + thumbHtml(c)
        + '<div class="nft-discover-info"><div class="nft-discover-name"><span>' + c.name + '</span>' + verifiedBadge(c) + '</div><div class="nft-discover-stat">' + statTxt + '</div></div>'
        + '<button class="nft-watch-btn" ' + (added ? 'disabled' : ('onclick="Watchlist.add(\'' + c.slug + '\')"')) + '>' + (added ? 'Watching' : 'Watch') + '</button>'
        + '</div>';
    }).join('');
  }

  function summary() { return { count: watchSlugs.length }; }
  function bentoStackHtml(max) {
    if (!watchSlugs.length) return '<div class="nft-empty-msg" style="padding:0">Nothing watched yet</div>';
    var shown = watchSlugs.slice(0, max);
    var html = shown.map(function (slug) {
      var c = cache[slug];
      var img = c && c.image;
      return img
        ? '<img class="bento-pfp" src="' + img + '" alt="" onerror="this.style.display=\'none\'">'
        : '<span class="bento-pfp">' + (slug[0] || '?').toUpperCase() + '</span>';
    }).join('');
    if (watchSlugs.length > max) html += '<span class="bento-pfp bento-pfp-more">+' + (watchSlugs.length - max) + '</span>';
    return html;
  }

  function init() {
    load();
    updateCount();
    if (watchSlugs.length) {
      Promise.all(watchSlugs.map(async function (slug) {
        try {
          var res = await fetch(BACKEND_URL + '/toolkit/nft-collection?slug=' + encodeURIComponent(slug));
          if (res.ok) cache[slug] = await res.json();
        } catch (e) { }
      })).then(function () { if (typeof Dash !== 'undefined') Dash.renderBento(); });
    }
  }

  return { init: init, tab: tab, subTab: subTab, search: search, add: add, remove: remove, copyContract: copyContract, summary: summary, bentoStackHtml: bentoStackHtml };
})();

// ── 11. WALLET X-RAY — real Blockscout + OpenSea composite score ─────────────
var XRay = (function () {
  function short(a) { return a && a.length > 14 ? a.slice(0, 6) + '…' + a.slice(-4) : (a || '-'); }

  function scanExample(addr) {
    document.getElementById('xrayInput').value = addr;
    scan();
  }

  async function scan() {
    var raw = (document.getElementById('xrayInput').value || '').trim();
    if (!raw) return;
    document.getElementById('xrayEmpty').style.display = 'none';
    document.getElementById('xrayOut').style.display = 'none';
    document.getElementById('xrayLoading').style.display = 'block';
    try {
      var res = await fetch(BACKEND_URL + '/toolkit/wallet-xray?address=' + encodeURIComponent(raw));
      if (!res.ok) {
        var errBody = await res.json().catch(function () { return {}; });
        throw new Error(errBody.detail || 'lookup failed');
      }
      var data = await res.json();
      render(data);
    } catch (e) {
      document.getElementById('xrayEmpty').textContent = e.message === 'lookup failed' || !e.message
        ? 'Could not reach the chain explorer right now. Try again in a moment.'
        : e.message;
      document.getElementById('xrayEmpty').style.display = 'block';
    } finally {
      document.getElementById('xrayLoading').style.display = 'none';
    }
  }

  function render(data) {
    var tier = data.tier, next = data.nextTier;
    document.getElementById('xrayEmoji').textContent = tier.emoji;
    document.getElementById('xrayTierName').textContent = tier.name;
    document.getElementById('xrayAddrOut').textContent = data.ensName || short(data.address);
    document.getElementById('xrayFlavor').textContent = tier.flavor;
    document.getElementById('xrayArchetype').textContent = data.archetype;
    document.getElementById('xrayScore').textContent = data.composite;
    document.getElementById('xrayNudge').textContent = next
      ? (next.min - data.composite) + ' pts from ' + next.name
      : 'Top tier reached: apex on-chain presence';

    // A sub-score of null means the underlying data source failed to load
    // (e.g. the chain explorer's activity counters), not that the wallet
    // genuinely scored zero there — those two cases must look different,
    // otherwise a temporary fetch failure reads as a confidently wrong score.
    document.getElementById('xraySubs').innerHTML = (data.subs || []).map(function (s) {
      if (s.v == null) {
        return '<div class="xray-sub-row"><div class="xray-sub-label">' + s.k + '</div><div class="xray-sub-track" style="opacity:.35"></div><div class="xray-sub-val" title="Temporarily unavailable">-</div></div>';
      }
      return '<div class="xray-sub-row"><div class="xray-sub-label">' + s.k + '</div><div class="xray-sub-track"><div class="xray-sub-fill" style="width:' + s.v + '%"></div></div><div class="xray-sub-val">' + s.v + '</div></div>';
    }).join('');

    var noteEl = document.getElementById('xrayDataNote');
    if (noteEl) {
      noteEl.style.display = data.countersOk === false ? 'block' : 'none';
      noteEl.textContent = 'Activity data (transactions, token transfers) was temporarily unavailable from the chain explorer, so those sub-scores are excluded rather than shown as zero. Try scanning again for a complete picture.';
    }

    var crypto = data.crypto || {}, nft = data.nft || {}, defi = data.defi || {}, behavior = data.behavior || {};
    var fmtOrDash = function (v) { return v == null ? '-' : v.toLocaleString('en-US'); };
    // Net worth/balance here are Ethereum-mainnet-only - if the wallet also
    // holds a native balance on other chains (checked via a cheap RPC
    // presence probe, not a full accounting), say so plainly rather than
    // let the ETH-only figure above read as the wallet's whole picture.
    var otherChains = (crypto.otherChains || []);
    var notes = [];
    if (otherChains.length) notes.push('Also active on ' + otherChains.join(', ') + ', not included above.');
    // Net Worth silently dropping tokens Blockscout has no market price
    // for (rather than treating them as zero) is exactly what makes the
    // total read as "wrong" for a wallet holding several obscure/illiquid
    // tokens — say plainly how many were left out and why.
    if (crypto.unpricedTokens) {
      notes.push(crypto.unpricedTokens + (crypto.unpricedTokens === 1 ? ' token holds' : ' tokens hold') + ' a real balance but have no market price available, so ' + (crypto.unpricedTokens === 1 ? "it isn't" : "they aren't") + ' included in Net Worth below.');
    }
    // token-balances is a separate, heavier fetch than everything else
    // (some wallets hold thousands of entries) - if it failed even after
    // a retry, Net Worth/Distinct Tokens reflect ETH only, not "confirmed
    // zero other tokens." Same honesty principle as the countersOk note.
    if (crypto.tokenDataOk === false) {
      notes.push('Token holdings could not be fully loaded this scan. Net Worth and Distinct Tokens may be incomplete. Try scanning again.');
    }
    var cryptoNotes = notes.length ? '<div class="xray-bd-note">' + notes.join(' ') + '</div>' : '';
    document.getElementById('xrayBreakdowns').innerHTML =
      '<div class="xray-bd"><h4>💰 Crypto Holdings</h4>'
      + '<div class="xray-bd-row"><span class="k">Net Worth (est., USD)</span><span class="v">$' + (crypto.netWorthUsd || 0).toLocaleString('en-US') + '</span></div>'
      + '<div class="xray-bd-row"><span class="k">ETH Balance</span><span class="v">' + (crypto.ethBalance || 0) + ' ETH</span></div>'
      + '<div class="xray-bd-row"><span class="k">Distinct Tokens</span><span class="v">' + (crypto.distinctTokens || 0) + '</span></div>'
      + cryptoNotes
      + '</div>'
      + '<div class="xray-bd"><h4>📊 On-Chain Activity</h4>'
      + '<div class="xray-bd-row"><span class="k">Transactions</span><span class="v">' + fmtOrDash(behavior.txCount) + '</span></div>'
      + '<div class="xray-bd-row"><span class="k">Token Transfers</span><span class="v">' + fmtOrDash(defi.tokenTransfers) + '</span></div>'
      + '<div class="xray-bd-row"><span class="k">NFT Collections</span><span class="v">' + (nft.collections || 0) + '</span></div>'
      + '</div>';

    document.getElementById('xrayNftCount').textContent = (nft.collections || 0) + (nft.collections === 1 ? ' collection' : ' collections') + ' · ' + (nft.items || 0) + ' items';
    var top = nft.top || [];
    document.getElementById('xrayNftGrid').innerHTML = top.length
      ? top.map(function (c) {
          return '<div class="xray-nft-card">' + (c.image ? '<img class="xray-nft-art" src="' + c.image + '" alt="" onerror="this.style.display=\'none\'">' : '<div class="xray-nft-art" style="display:grid;place-items:center;background:rgba(255,255,255,.04);color:var(--muted2);font-family:var(--display);font-weight:700">' + ((c.name || '?')[0] || '?').toUpperCase() + '</div>')
            + '<div class="xray-nft-meta"><div class="xray-nft-name">' + c.name + '</div><div class="xray-nft-sub">' + c.count + (c.count === 1 ? ' item' : ' items') + '</div></div></div>';
        }).join('')
      : '<div class="xray-nft-empty">No NFT holdings found on Ethereum mainnet.</div>';

    document.getElementById('xrayOut').style.display = 'block';

    lastResult = data;
    try {
      localStorage.setItem('dashXrayLast', JSON.stringify({
        emoji: tier.emoji, name: tier.name, color: tier.color, score: data.composite, flavor: tier.flavor
      }));
    } catch (e) { }
    if (typeof Dash !== 'undefined') Dash.renderBento();
  }

  var lastResult = null;

  // Result card export — same canvas-drawing approach as the membership
  // card (CardActions.download), laid out as a full stats reveal instead
  // of an ID card. Height isn't fixed: a wallet with 6 sub-scores and long
  // notes needs more room than one with 2, so paint() runs once against an
  // oversized probe canvas purely to measure the real content height, then
  // runs again for real at that exact height - that's what keeps this from
  // either clipping content or leaving dead space at the bottom.
  function download() {
    if (!lastResult) return;
    var data = lastResult, tier = data.tier;
    var accent = tier.color || '#5B9BF8';
    var subs = (data.subs || []).filter(function (s) { return s.v != null; });
    var crypto = data.crypto || {}, nft = data.nft || {}, defi = data.defi || {}, behavior = data.behavior || {};
    var fmtOrDash = function (v) { return v == null ? '-' : v.toLocaleString('en-US'); };
    var notesArr = [];
    if ((crypto.otherChains || []).length) notesArr.push('Also active on ' + crypto.otherChains.join(', ') + '.');
    if (crypto.unpricedTokens) notesArr.push(crypto.unpricedTokens + (crypto.unpricedTokens === 1 ? ' token not priced.' : ' tokens not priced.'));
    var notesText = notesArr.join(' ');
    var W = 680, padX = 48;

    function rr(ctx, px, py, w, h, r) { ctx.beginPath(); ctx.moveTo(px + r, py); ctx.arcTo(px + w, py, px + w, py + h, r); ctx.arcTo(px + w, py + h, px, py + h, r); ctx.arcTo(px, py + h, px, py, r); ctx.arcTo(px, py, px + w, py, r); ctx.closePath(); }

    function paint(x, H, finalize) {
      if (finalize) {
        rr(x, 0, 0, W, H, 28); x.save(); x.clip();
        var g = x.createLinearGradient(0, 0, W, H); g.addColorStop(0, '#10204f'); g.addColorStop(0.55, '#0a1330'); g.addColorStop(1, '#0a1a3e');
        x.fillStyle = g; x.fillRect(0, 0, W, H);
        var sh = x.createLinearGradient(0, 0, W, H); sh.addColorStop(0.30, 'rgba(255,255,255,0)'); sh.addColorStop(0.45, 'rgba(145,190,255,0.18)'); sh.addColorStop(0.52, 'rgba(145,190,255,0.10)'); sh.addColorStop(0.62, 'rgba(255,255,255,0)');
        x.fillStyle = sh; x.fillRect(0, 0, W, H);
      }
      var dy = 46;
      x.textAlign = 'center';
      x.fillStyle = '#5B9BF8'; x.font = '700 13px "JetBrains Mono",monospace'; x.letterSpacing = '2px';
      x.fillText('DASH HQ · WALLET X-RAY', W / 2, dy); x.letterSpacing = '0px';
      dy += 84;
      x.font = '68px sans-serif'; x.fillText(tier.emoji, W / 2, dy);
      dy += 40;
      x.fillStyle = '#fff'; x.font = '800 32px Sora,sans-serif';
      x.fillText(tier.name, W / 2, dy);
      dy += 24;
      x.fillStyle = '#8A9BBF'; x.font = '400 13px "JetBrains Mono",monospace';
      x.fillText(data.ensName || short(data.address), W / 2, dy);
      dy += 92;
      var scoreTxt = String(data.composite);
      x.fillStyle = accent; x.font = '800 80px "JetBrains Mono",monospace';
      x.fillText(scoreTxt, W / 2, dy);
      x.fillStyle = '#8A9BBF'; x.font = '600 14px "JetBrains Mono",monospace';
      x.fillText('/ 100', W / 2, dy + 26);
      dy += 40;
      var badgeTxt = data.archetype;
      x.font = '700 14px "JetBrains Mono",monospace';
      var bw = x.measureText(badgeTxt).width + 34, bh = 40;
      rr(x, (W - bw) / 2, dy, bw, bh, bh / 2); x.fillStyle = 'rgba(91,155,248,.16)'; x.fill();
      x.strokeStyle = 'rgba(91,155,248,.4)'; x.lineWidth = 1.5; x.stroke();
      x.fillStyle = accent; x.fillText(badgeTxt, W / 2, dy + bh / 2 + 5);
      dy += bh + 26;
      x.fillStyle = '#C8D4EE'; x.font = '400 15px "JetBrains Mono",monospace';
      var flavorLines = wrapText(x, tier.flavor, W - padX * 2 - 40);
      flavorLines.forEach(function (line, i) { x.fillText(line, W / 2, dy + i * 22); });
      dy += flavorLines.length * 22 + 30;

      x.strokeStyle = 'rgba(255,255,255,.1)'; x.lineWidth = 1;
      x.beginPath(); x.moveTo(padX, dy); x.lineTo(W - padX, dy); x.stroke();
      dy += 30;

      x.textAlign = 'left';
      if (subs.length) {
        x.fillStyle = '#8A9BBF'; x.font = '700 12px "JetBrains Mono",monospace'; x.letterSpacing = '1.5px';
        x.fillText('SCORE BREAKDOWN', padX, dy); x.letterSpacing = '0px';
        dy += 26;
        subs.forEach(function (s) {
          x.fillStyle = '#C8D4EE'; x.font = '400 13px "JetBrains Mono",monospace';
          x.fillText(s.k, padX, dy);
          x.textAlign = 'right'; x.fillStyle = '#fff'; x.font = '700 13px "JetBrains Mono",monospace';
          x.fillText(String(s.v), W - padX, dy);
          x.textAlign = 'left';
          var trackY = dy + 8, trackW = W - padX * 2, pct = Math.max(0, Math.min(100, s.v)) / 100;
          rr(x, padX, trackY, trackW, 4, 2); x.fillStyle = 'rgba(255,255,255,.08)'; x.fill();
          rr(x, padX, trackY, Math.max(4, trackW * pct), 4, 2); x.fillStyle = accent; x.fill();
          dy += 34;
        });
        dy += 18;
        x.strokeStyle = 'rgba(255,255,255,.1)'; x.beginPath(); x.moveTo(padX, dy); x.lineTo(W - padX, dy); x.stroke();
        dy += 30;
      }

      function kvSection(title, rows) {
        x.fillStyle = '#8A9BBF'; x.font = '700 12px "JetBrains Mono",monospace'; x.letterSpacing = '1.5px';
        x.fillText(title, padX, dy); x.letterSpacing = '0px';
        dy += 26;
        rows.forEach(function (r) {
          x.fillStyle = '#8A9BBF'; x.font = '400 13px "JetBrains Mono",monospace';
          x.fillText(r[0], padX, dy);
          x.textAlign = 'right'; x.fillStyle = '#E8EFFF'; x.font = '700 13px "JetBrains Mono",monospace';
          x.fillText(r[1], W - padX, dy);
          x.textAlign = 'left';
          dy += 28;
        });
      }

      kvSection('💰 CRYPTO HOLDINGS', [
        ['Net Worth (est.)', '$' + (crypto.netWorthUsd || 0).toLocaleString('en-US')],
        ['ETH Balance', (crypto.ethBalance || 0) + ' ETH'],
        ['Distinct Tokens', String(crypto.distinctTokens || 0)]
      ]);
      var noteLines = notesText ? wrapText(x, notesText, W - padX * 2) : [];
      if (noteLines.length) {
        x.fillStyle = '#5A6A8A'; x.font = '400 11px "JetBrains Mono",monospace';
        noteLines.forEach(function (l, i) { x.fillText(l, padX, dy + i * 16); });
        dy += noteLines.length * 16 + 6;
      }
      dy += 22;
      x.strokeStyle = 'rgba(255,255,255,.1)'; x.beginPath(); x.moveTo(padX, dy); x.lineTo(W - padX, dy); x.stroke();
      dy += 30;

      kvSection('📊 ON-CHAIN ACTIVITY', [
        ['Transactions', fmtOrDash(behavior.txCount)],
        ['Token Transfers', fmtOrDash(defi.tokenTransfers)],
        ['NFT Collections', String(nft.collections || 0)]
      ]);
      dy += 12;

      x.textAlign = 'center';
      x.fillStyle = '#8A9BBF'; x.font = '400 11px "JetBrains Mono",monospace'; x.letterSpacing = '1px';
      x.fillText('HEURISTIC ON-CHAIN SCORE · DASHHQ.SITE', W / 2, dy + 20);
      x.letterSpacing = '0px';
      dy += 46;

      if (finalize) {
        x.restore();
        rr(x, 1, 1, W - 2, H - 2, 28); x.strokeStyle = 'rgba(255,255,255,0.16)'; x.lineWidth = 2; x.stroke();
      }
      return dy;
    }

    var probeCv = document.createElement('canvas'); probeCv.width = W; probeCv.height = 3000;
    var H = Math.ceil(paint(probeCv.getContext('2d'), 3000, false));

    var cv = document.createElement('canvas'); cv.width = W; cv.height = H;
    paint(cv.getContext('2d'), H, true);

    var a = document.createElement('a'); a.href = cv.toDataURL('image/png'); a.download = 'dash-hq-wallet-xray.png';
    document.body.appendChild(a); a.click(); a.remove();
  }
  function wrapText(ctx, text, maxWidth) {
    var words = text.split(' '), lines = [], line = '';
    words.forEach(function (w) {
      var test = line ? line + ' ' + w : w;
      if (ctx.measureText(test).width > maxWidth && line) { lines.push(line); line = w; }
      else line = test;
    });
    if (line) lines.push(line);
    return lines;
  }
  function shareX() {
    if (!lastResult) return;
    var data = lastResult;
    var lines = [
      data.tier.emoji + ' ' + data.tier.name + ', ' + data.composite + '/100 on Dash HQ\'s Wallet X-Ray. Archetype: ' + data.archetype + '. Go scan yours, @DASHHQX.',
      'My wallet just scored ' + data.composite + '/100 on Wallet X-Ray. ' + data.tier.emoji + ' ' + data.tier.name + ' tier, ' + data.archetype + '. Try @DASHHQX.',
      'Wallet X-Ray says I\'m a ' + data.tier.emoji + ' ' + data.tier.name + ' (' + data.composite + '/100, ' + data.archetype + '). Scan yours on @DASHHQX.'
    ];
    var text = lines[Math.floor(Math.random() * lines.length)];
    var url = 'https://www.dashhq.site';
    var intent = 'https://twitter.com/intent/tweet?text=' + encodeURIComponent(text) + '&url=' + encodeURIComponent(url);
    window.open(intent, '_blank', 'noopener,width=600,height=520');
  }

  return { scan: scan, scanExample: scanExample, download: download, shareX: shareX };
})();

// ── PROFILE ────────────────────────────────────────────────────────────────
var Profile = (function () {
  var PIN_ICONS = {
    xray: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    ticker: '<path d="M3 17l5-5 4 4 8-9"/><path d="M21 7v5h-5"/>',
    gas: '<path d="M4 21V6a2 2 0 0 1 2-2h5a2 2 0 0 1 2 2v15"/><path d="M3 21h11"/><path d="M13 10h2a2 2 0 0 1 2 2v5.5a1.5 1.5 0 0 0 3 0V9l-3-3"/>',
    wallet: '<rect x="2.5" y="6" width="19" height="13" rx="2.5"/><path d="M16 12.5h3"/>',
    pnl: '<path d="M4 19V5M4 19h16M9 15l3-4 3 2 4-6"/>',
    ape: '<path d="M13 2 4 14h7l-1 8 10-12h-7z"/>',
    pairs: '<circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/>',
    scan: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M20 20l-4.8-4.8"/>',
    nft: '<path d="M6 3.5h12a.5.5 0 0 1 .5.5v17l-6.5-4-6.5 4v-17a.5.5 0 0 1 .5-.5z"/>',
    rug: '<path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"/>',
    slip: '<path d="M2 12c2-3 4-3 6 0s4 3 6 0 4-3 6 0"/><path d="M2 18c2-3 4-3 6 0s4 3 6 0 4-3 6 0"/>'
  };
  var TOOL_LABELS = { xray: 'Wallet X-Ray', ticker: 'Price Ticker', gas: 'Gas Tracker', wallet: 'Wallet Card', pnl: 'DCA / PnL', ape: 'Ape Math', pairs: 'New Pairs', scan: 'CA Scanner', nft: 'NFT Watchlist', rug: 'Rug Check', slip: 'Slippage' };
  function getPins() {
    try { return JSON.parse(localStorage.getItem('dashhq_pinned_tools') || '["ticker","gas","rug"]'); }
    catch (e) { return ['ticker', 'gas', 'rug']; }
  }
  function renderPins() {
    var pins = getPins();
    var el = document.getElementById('profilePins');
    if (!pins.length) { el.innerHTML = '<div class="pin-empty">No pinned tools yet.</div>'; return; }
    el.innerHTML = pins.map(function (k) {
      return '<div class="pin-chip" onclick="Dash.go(\'' + k + '\')"><svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">' + (PIN_ICONS[k] || '') + '</svg><span>' + (TOOL_LABELS[k] || k) + '</span></div>';
    }).join('');
  }
  function saveBio() {
    var v = document.getElementById('profileBio').value;
    try { localStorage.setItem('dashhq_profile_bio', v); } catch (e) { }
    var msg = document.getElementById('profileSaveMsg');
    msg.classList.add('show');
    setTimeout(function () { msg.classList.remove('show'); }, 1800);
  }
  function init() {
    try {
      var saved = localStorage.getItem('dashhq_profile_bio');
      if (saved) document.getElementById('profileBio').value = saved;
    } catch (e) { }
    renderPins();
  }
  return { init: init, saveBio: saveBio };
})();

document.addEventListener('DOMContentLoaded', init);

// ── Particle network (portal's own backdrop, only draws while open) ─────────
(function () {
  const c = document.getElementById('portalParticles');
  const modal = document.getElementById('portalModal');
  if (!c || !modal) return;
  const x = c.getContext('2d');
  let w, h, pts, raf;
  function size() { w = c.width = innerWidth; h = c.height = innerHeight; }
  function init() {
    size();
    const n = Math.min(64, Math.floor(w * h / 18000));
    pts = [];
    for (let i = 0; i < n; i++) pts.push({ x: Math.random() * w, y: Math.random() * h, vx: (Math.random() - .5) * .26, vy: (Math.random() - .5) * .26 });
  }
  function loop() {
    if (modal.classList.contains('open')) {
      x.clearRect(0, 0, w, h);
      for (const p of pts) {
        p.x += p.vx; p.y += p.vy;
        if (p.x < 0 || p.x > w) p.vx *= -1;
        if (p.y < 0 || p.y > h) p.vy *= -1;
      }
      for (let i = 0; i < pts.length; i++) {
        for (let j = i + 1; j < pts.length; j++) {
          const dx = pts[i].x - pts[j].x, dy = pts[i].y - pts[j].y, d = Math.hypot(dx, dy);
          if (d < 130) {
            x.strokeStyle = 'rgba(91,155,248,' + (.14 * (1 - d / 130)) + ')';
            x.lineWidth = 1;
            x.beginPath(); x.moveTo(pts[i].x, pts[i].y); x.lineTo(pts[j].x, pts[j].y); x.stroke();
          }
        }
        x.fillStyle = 'rgba(120,160,255,.6)';
        x.beginPath(); x.arc(pts[i].x, pts[i].y, 1.4, 0, 7); x.fill();
      }
    }
    raf = requestAnimationFrame(loop);
  }
  init(); loop();
  addEventListener('resize', () => { cancelAnimationFrame(raf); init(); loop(); });
})();
