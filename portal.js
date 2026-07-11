// ── Config ──────────────────────────────────────────────────────────────────
// Localhost → local FastAPI server. Production → same domain (Vercel routes /auth/* to Python).
const BACKEND_URL = ['localhost', '127.0.0.1'].includes(window.location.hostname)
  ? 'http://localhost:8000'
  : 'https://www.dashhq.site';
const TOKEN_KEY = 'dashhq_citizen_token';

// ── State machine ────────────────────────────────────────────────────────────
function setState(s) {
  document.querySelectorAll('.state').forEach(el => el.classList.toggle('on', el.dataset.state === s));
  document.getElementById('auth')?.classList.toggle('wide', s === 'member');
  document.querySelector('.portal')?.classList.toggle('in-hub', s === 'member');
  if (s === 'member') Hub.init();
}

// ── Discord OAuth ────────────────────────────────────────────────────────────
function startDiscordAuth() {
  window.location.href = `${BACKEND_URL}/auth/discord`;
}

function signOut() {
  sessionStorage.removeItem(TOKEN_KEY);
  setState('prelogin');
}

// ── Card population (feeds the Card, Hub header, and Profile panel alike) ────
function updateCard(data) {
  const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
  set('cardName', data.display_name);
  set('cardHandle', data.handle);
  set('cardTier', `★ ${data.tier}`);
  set('cardJoined', data.joined);
  set('hubName', data.display_name);
  set('hubTier', `★ ${data.tier}`);
  set('profileName', data.display_name);
  set('profileHandle', data.handle);
  set('profileTierTag', `★ ${data.tier}`);
  set('profileSinceTag', `Citizen since ${data.joined}`);
  ['cardAvatar', 'hubAvatar', 'profileAvatar'].forEach(id => {
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
  // Always open at the top, whatever scroll position it was left at last
  // time it was open — reopening scrolled halfway down reads as the portal
  // randomly jumping around rather than a stable, predictable panel.
  modal.scrollTop = 0;
  // overflow:hidden alone doesn't reliably stop the background page from
  // scrolling/bouncing behind a position:fixed modal on iOS Safari and some
  // Android browsers — pinning the body in place at its current scroll
  // offset is the robust fix, since it removes any scrollable distance for
  // a touch gesture to act on.
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

// ── PORTAL HUB — tab switching ────────────────────────────────────────────────
var Hub = (function () {
  var inited = false;
  function go(tab) {
    document.querySelectorAll('.hub-tab').forEach(function (t) {
      var on = t.dataset.hub === tab;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('.hub-panel').forEach(function (p) { p.classList.toggle('on', p.dataset.hubPanel === tab); });
    // Switching tabs should reliably show the new tab from its own top —
    // otherwise leftover scroll from whichever tab you were just on carries
    // over, landing you mid-scroll on unrelated content.
    var modal = document.getElementById('portalModal');
    if (modal) modal.scrollTop = 0;
  }
  function init() {
    if (inited) return; inited = true;
    Ticker.init(); Gas.init(); Pnl.init(); Ape.calc(); Pairs.init(); Slip.calc();
    Profile.init();
  }
  function openTool(id) {
    document.getElementById('tkLauncher').style.display = 'none';
    document.getElementById('tkFocusbar').style.display = 'flex';
    var stack = document.getElementById('toolsStack');
    stack.style.display = 'block';
    stack.querySelectorAll('.tool-card').forEach(function (c) { c.classList.toggle('active', c.id === 'tool-' + id); });
    document.getElementById('tkFocusbar').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  function backToGrid() {
    document.getElementById('tkLauncher').style.display = 'grid';
    document.getElementById('tkFocusbar').style.display = 'none';
    document.getElementById('toolsStack').style.display = 'none';
    var modal = document.getElementById('portalModal');
    if (modal) modal.scrollTop = 0;
  }
  return { go: go, init: init, openTool: openTool, backToGrid: backToGrid };
})();

// ── CARD ACTIONS — download + share ───────────────────────────────────────────
var CardActions = (function () {
  function download() {
    var card = document.getElementById('memberCard');
    var rect = card.getBoundingClientRect();
    var S = 3, W = Math.round(rect.width * S), H = Math.round(rect.height * S);
    var cv = document.createElement('canvas'); cv.width = W; cv.height = H;
    var x = cv.getContext('2d');
    function rr(px, py, w, h, r) { x.beginPath(); x.moveTo(px + r, py); x.arcTo(px + w, py, px + w, py + h, r); x.arcTo(px + w, py + h, px, py + h, r); x.arcTo(px, py + h, px, py, r); x.arcTo(px, py, px + w, py, r); x.closePath(); }
    rr(0, 0, W, H, 20 * S); x.save(); x.clip();
    var g = x.createLinearGradient(0, 0, W, H); g.addColorStop(0, '#10204f'); g.addColorStop(0.55, '#0a1330'); g.addColorStop(1, '#0a1a3e');
    x.fillStyle = g; x.fillRect(0, 0, W, H);
    var sh = x.createLinearGradient(0, 0, W, H); sh.addColorStop(0.30, 'rgba(255,255,255,0)'); sh.addColorStop(0.45, 'rgba(145,190,255,0.18)'); sh.addColorStop(0.52, 'rgba(145,190,255,0.10)'); sh.addColorStop(0.62, 'rgba(255,255,255,0)');
    x.fillStyle = sh; x.fillRect(0, 0, W, H);
    var P = 26 * S;
    x.fillStyle = '#fff'; x.font = '700 ' + (15 * S) + 'px Sora,sans-serif'; x.textBaseline = 'alphabetic'; x.letterSpacing = (2 * S) + 'px';
    x.fillText('DASH CITIZEN', P, P + 14 * S); x.letterSpacing = '0px';
    // Real tier from the card itself — never hardcode, tiers can change.
    var tierTxt = (document.getElementById('cardTier') || {}).textContent || '★ CITIZEN';
    x.font = '700 ' + (11 * S) + 'px "JetBrains Mono",monospace';
    var tw = x.measureText(tierTxt).width, padX = 12 * S, ph = 28 * S, pw = tw + padX * 2;
    var tg = x.createLinearGradient(W - P - pw, P - 4 * S, W - P, P - 4 * S + ph); tg.addColorStop(0, '#ffe9a8'); tg.addColorStop(1, '#b97d16');
    rr(W - P - pw, P - 4 * S, pw, ph, ph / 2); x.fillStyle = tg; x.fill();
    x.fillStyle = '#251a02'; x.textBaseline = 'middle'; x.fillText(tierTxt, W - P - pw + padX, P - 4 * S + ph / 2 + 1 * S); x.textBaseline = 'alphabetic';
    var nameTxt = (document.getElementById('cardName') || {}).textContent || 'CITIZEN';
    x.fillStyle = '#fff'; x.font = '700 ' + (30 * S) + 'px Sora,sans-serif'; x.fillText(nameTxt, P, H - 70 * S);
    var handleTxt = (document.getElementById('cardHandle') || {}).textContent || '';
    x.fillStyle = '#5B9BF8'; x.font = '400 ' + (13 * S) + 'px "JetBrains Mono",monospace'; x.fillText(handleTxt, P, H - 48 * S);
    function meta(lab, val, mx, color) {
      x.fillStyle = '#8A9BBF'; x.font = '400 ' + (9 * S) + 'px "JetBrains Mono",monospace'; x.letterSpacing = (1 * S) + 'px';
      x.fillText(lab.toUpperCase(), mx, H - 24 * S); x.letterSpacing = '0px';
      x.fillStyle = color || '#E8EFFF'; x.font = '700 ' + (15 * S) + 'px "JetBrains Mono",monospace';
      x.fillText(val, mx, H - 10 * S);
    }
    // Real joined date + the card's actual second stat (Status: Active) —
    // matches what's really on screen, not the design's placeholder stat.
    meta('Member Since', (document.getElementById('cardJoined') || {}).textContent || '—', P);
    meta('Status', 'Active', P + 150 * S, '#10B981');
    x.restore();
    rr(1 * S, 1 * S, W - 2 * S, H - 2 * S, 20 * S); x.strokeStyle = 'rgba(255,255,255,0.16)'; x.lineWidth = 2 * S; x.stroke();
    var a = document.createElement('a'); a.href = cv.toDataURL('image/png'); a.download = 'dash-citizen-card.png';
    document.body.appendChild(a); a.click(); a.remove();
  }
  function shareX() {
    var name = (document.getElementById('cardName') || {}).textContent || 'a Dash Citizen';
    var since = (document.getElementById('cardJoined') || {}).textContent || '2024';
    var lines = [
      "I'm officially a Dash Citizen, verified since " + since + ".",
      "Just verified my Dash Citizen card — HQ since " + since + ".",
      "Dash HQ Citizen since " + since + ". One step ahead of the curve."
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

  // Resolving a symbol and fetching its price used to be two separate
  // CoinGecko calls per token, called directly from the browser — slow, and
  // prone to silently hitting CoinGecko's free-tier rate limit. Now proxied
  // through one batched backend call for every tracked symbol at once.
  async function fetchBatch(syms) {
    if (!syms.length) return {};
    try {
      var res = await fetch(BACKEND_URL + '/toolkit/ticker?symbols=' + encodeURIComponent(syms.join(',')));
      if (!res.ok) return {};
      return await res.json();
    } catch (e) { return {}; }
  }
  function sparkPath(hist) {
    if (hist.length < 2) return '';
    var min = Math.min.apply(null, hist), max = Math.max.apply(null, hist), range = (max - min) || 1;
    var w = 100, h = 24;
    return hist.map(function (v, i) {
      var x = (i / (hist.length - 1)) * w, y = h - ((v - min) / range) * h;
      return (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
  }
  function render() {
    var grid = document.getElementById('tickerGrid');
    var syms = Object.keys(state);
    if (!syms.length) { grid.innerHTML = '<div class="pin-empty">Add a token above to start your watchlist.</div>'; return; }
    grid.innerHTML = syms.map(function (sym) {
      var s = state[sym];
      if (s.loading) return '<div class="tk-item"><button class="rm" onclick="Ticker.remove(\'' + sym + '\')" aria-label="Remove">×</button><div class="sym">' + sym + '</div><div class="px">Loading…</div></div>';
      if (s.error) return '<div class="tk-item"><button class="rm" onclick="Ticker.remove(\'' + sym + '\')" aria-label="Remove">×</button><div class="sym">' + sym + '</div><div class="px" style="cursor:pointer" onclick="Ticker.retry(\'' + sym + '\')" title="Click to retry">Not found — retry</div></div>';
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
    if (!s) return; // removed while the request was in flight
    s.loading = false; // always resolved here — never left hanging, success or not
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
    // Fetch a second data point soon after so the sparkline has something to
    // draw almost immediately, instead of waiting for the next full tick().
    if (state[sym] && !state[sym].error) {
      setTimeout(function () {
        if (!state[sym]) return;
        fetchBatch([sym]).then(function (r) { applyResult(sym, r[sym]); render(); });
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
  }
  function init() {
    seed('ETH'); seed('SOL');
    setInterval(tick, 15000); // one batched call regardless of watchlist size
  }
  return { init: init, add: add, remove: remove, retry: retry };
})();

// ── 2. GAS TRACKER — real public RPC + CoinGecko ──────────────────────────────
var Gas = (function () {
  var gwei = null, nativePrice = null, solanaFees = null;
  var GAS_UNITS = { transfer: 21000, swap: 150000, mint: 220000 };
  // Rough compute-unit estimates for Solana — actual usage varies a lot by
  // program, so this is illustrative, same spirit as the EVM gas-unit table.
  var SOLANA_CU = { transfer: 450, swap: 200000, mint: 300000 };
  var SOLANA_BASE_FEE_LAMPORTS = 5000; // fixed protocol fee per signature
  // Public, keyless RPCs + the CoinGecko id for each chain's native gas token.
  var CHAINS = {
    ethereum: { label: 'Ethereum mainnet', symbol: 'ETH' },
    bsc: { label: 'BNB Chain', symbol: 'BNB' },
    polygon: { label: 'Polygon', symbol: 'POL' },
    arbitrum: { label: 'Arbitrum One', symbol: 'ETH' },
    optimism: { label: 'Optimism', symbol: 'ETH' },
    base: { label: 'Base', symbol: 'ETH' },
    avalanche: { label: 'Avalanche C-Chain', symbol: 'AVAX' },
    solana: { label: 'Solana', symbol: 'SOL', isSolana: true }
  };
  var current = 'ethereum';

  // Public RPCs generally don't send CORS headers for raw browser fetch, so
  // gas price + native token price are fetched through our own backend,
  // which proxies both the RPC and CoinGecko in a single call.
  async function fetchGasData(chain) {
    try {
      var res = await fetch(BACKEND_URL + '/toolkit/gas?chain=' + encodeURIComponent(chain));
      if (!res.ok) return { gwei: null, nativeUsd: null, solanaFees: null };
      var data = await res.json();
      return { gwei: data.gwei, nativeUsd: data.native_usd, solanaFees: data.solana_fees || null };
    } catch (e) { return { gwei: null, nativeUsd: null, solanaFees: null }; }
  }
  // Sub-1-gwei readings are common now (L2s especially, sometimes L1 too) —
  // a fixed decimal count either shows "0" or wastes space, so scale the
  // precision to the actual magnitude instead.
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
    if (gwei == null) { document.getElementById('gasBig').textContent = '—'; document.getElementById('gasTiers').innerHTML = ''; document.getElementById('gasCosts').innerHTML = ''; return; }
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
    if (!solanaFees) { document.getElementById('gasBig').textContent = '—'; document.getElementById('gasTiers').innerHTML = ''; document.getElementById('gasCosts').innerHTML = ''; return; }
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
      // base fee (fixed) + priority fee (micro-lamports/CU × estimated CU)
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
    if (chain !== current) return; // user switched chains mid-fetch — drop the stale result
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

// ── 3. WALLET CARD — real QR (downloadable) + real ENS resolution ────────────
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
      // ENS only resolves Ethereum-style names — the only case where we
      // need to turn user input into a different address before the QR step.
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
      } catch (e) { /* reverse lookup is a nice-to-have, fine if it fails */ }
    }
    // Any other non-empty input (Solana base58, Bitcoin, other chains'
    // native address formats, etc.) is used as-is — a wallet card just
    // needs to display and QR-encode the address, no chain-specific parsing.
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
  function downloadQr() {
    var img = document.getElementById('wcardQr');
    if (!img || !img.src) return;
    fetch(img.src).then(function (res) { return res.blob(); }).then(function (blob) {
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url; a.download = 'dash-wallet-qr.png';
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    }).catch(function () {
      // If the QR host ever blocks a cross-origin blob read, fall back to
      // opening it directly so the user can still save it manually.
      window.open(img.src, '_blank', 'noopener');
    });
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
        // The pool's own address isn't what you'd paste into a swap or a
        // rug check — the base token (the new asset itself) is, and
        // GeckoTerminal always prefixes its relationship ids with the
        // exact network slug we requested, so stripping that gives the
        // raw contract/mint address for any chain (EVM hex or Solana base58).
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
    // Rows are links out to GeckoTerminal — stop the click from also
    // navigating away before the copy has a chance to register.
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
  function init() {
    refresh();
    setInterval(refresh, 45000); // respectful poll interval for a free public API
  }
  return { init: init, render: render, refresh: refresh, copyAddress: copyAddress };
})();

// ── 7. CA SCANNER — real DexScreener data, chain auto-detected from the
//    address itself, so every chain DexScreener indexes works with zero
//    per-chain configuration on our side ─────────────────────────────────
var CaScan = (function () {
  var pairs = [];
  var current = null;

  var CHAIN_LABELS = {
    ethereum: 'Ethereum', bsc: 'BNB Chain', polygon: 'Polygon', arbitrum: 'Arbitrum',
    optimism: 'Optimism', base: 'Base', avalanche: 'Avalanche', solana: 'Solana'
  };
  function chainLabel(id) {
    if (CHAIN_LABELS[id]) return CHAIN_LABELS[id];
    // DexScreener indexes far more chains than our other tools' fixed
    // lists — anything outside the standard set still gets a readable
    // label instead of being dropped.
    return id.charAt(0).toUpperCase() + id.slice(1).replace(/-/g, ' ');
  }
  function fmtPrice(n) {
    if (n == null || isNaN(n)) return '—';
    if (n >= 1) return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (n >= 0.01) return '$' + n.toFixed(4);
    if (n >= 0.000001) return '$' + n.toFixed(8).replace(/0+$/, '').replace(/\.$/, '');
    return '$' + n.toExponential(2);
  }
  function fmtCompact(n) {
    if (n == null || isNaN(n)) return '—';
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
    var btn = document.querySelector('#tool-scan .tk-addrow .btn');
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
      empty.textContent = 'Could not reach the scanner right now — try again in a moment.';
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
    if (n == null || isNaN(n)) return '—';
    if (n >= 1) return n.toLocaleString('en-US', { maximumFractionDigits: 6 });
    if (n >= 0.000001) return n.toFixed(10).replace(/0+$/, '').replace(/\.$/, '');
    return n.toExponential(2);
  }
  function shortAddr(a) { return a && a.length > 14 ? a.slice(0, 6) + '…' + a.slice(-4) : (a || '—'); }
  function copySvg() { return '<svg viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M4 16V5a1 1 0 0 1 1-1h11"/></svg>'; }

  // Everything a trader would otherwise have to open DexScreener itself to
  // find — pairing, native price, pool address, FDV vs circulating market
  // cap, and the full 5m/1h/6h/24h volume + buy/sell breakdown — tucked
  // into one dropdown so the main card above stays clean and scannable.
  function buildMoreDetails(pair) {
    var quote = pair.quoteToken || {};
    var dexLabel = (pair.dexId || '—') + (pair.labels && pair.labels.length ? ' · ' + pair.labels.join('/').toUpperCase() : '');
    var mcap = pair.marketCap, fdv = pair.fdv;

    var pairInfo = '<div class="scan-more-section"><div class="msl">Pair Info</div>'
      + '<div class="scan-detail-row"><span class="dl">Paired with</span><span class="dv">' + (quote.symbol || '—') + '</span></div>'
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
      + row('Buys', function (k) { return (txns[k] && txns[k].buys != null) ? txns[k].buys : '—'; })
      + row('Sells', function (k) { return (txns[k] && txns[k].sells != null) ? txns[k].sells : '—'; })
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
    // "1".."43114" for EVM chains, or the literal string "solana" —
    // the backend branches on this to pick the right scanner.
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

// ── 8. PRICE IMPACT / SLIPPAGE ESTIMATOR — real GeckoTerminal pool depth ──────
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
    // Typing in the pair box fires on every keystroke — debounce so we
    // don't fan out a GeckoTerminal search request per character.
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
    // Approximation using total pool depth (constant-product pools split
    // liquidity ~50/50 by value) — labeled as an estimate throughout, since
    // exact impact needs the pool's raw token reserves, not just USD depth.
    var halfDepth = liq / 2;
    var impact = halfDepth > 0 ? (amountIn / (amountIn + halfDepth)) * 100 : 0;
    var level = impact < 1 ? 'low' : impact < 5 ? 'medium' : 'high';
    var color = level === 'low' ? '#10B981' : level === 'medium' ? '#F59E0B' : '#EF4444';
    var html = '<div class="slip-stat"><div class="sl">Est. Price Impact</div><div class="sv" style="color:' + color + '">' + impact.toFixed(2) + '%</div></div>'
      + '<div class="slip-stat"><div class="sl">Pool Liquidity</div><div class="sv">$' + Math.round(liq).toLocaleString('en-US') + '</div></div>';
    if (impact >= 5) {
      html += '<div class="slip-warn" style="background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.35);color:#EF4444">⚠ High impact — this trade will move the price significantly. Consider splitting it into smaller trades.</div>';
    } else if (impact >= 1) {
      html += '<div class="slip-warn" style="background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.35);color:#F59E0B">Moderate impact — worth keeping an eye on for larger size.</div>';
    }
    resultEl.innerHTML = html;
  }
  return { calc: calc };
})();

// ── PROFILE ────────────────────────────────────────────────────────────────
var Profile = (function () {
  var PIN_ICONS = {
    ticker: '<path d="M3 17l5-5 4 4 8-9"/><path d="M21 7v5h-5"/>',
    gas: '<path d="M4 21V6a2 2 0 0 1 2-2h5a2 2 0 0 1 2 2v15"/><path d="M3 21h11"/><path d="M13 10h2a2 2 0 0 1 2 2v5.5a1.5 1.5 0 0 0 3 0V9l-3-3"/>',
    wallet: '<rect x="2.5" y="6" width="19" height="13" rx="2.5"/><path d="M16 12.5h3"/>',
    pnl: '<path d="M4 19V5M4 19h16M9 15l3-4 3 2 4-6"/>',
    ape: '<path d="M13 2 4 14h7l-1 8 10-12h-7z"/>',
    pairs: '<circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/>',
    scan: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M20 20l-4.8-4.8"/>',
    rug: '<path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"/>',
    slip: '<path d="M2 12c2-3 4-3 6 0s4 3 6 0 4-3 6 0"/><path d="M2 18c2-3 4-3 6 0s4 3 6 0 4-3 6 0"/>'
  };
  var TOOL_LABELS = { ticker: 'Price Ticker', gas: 'Gas Tracker', wallet: 'Wallet Card', pnl: 'DCA / PnL', ape: 'Ape Math', pairs: 'New Pairs', scan: 'CA Scanner', rug: 'Rug Check', slip: 'Slippage' };
  function getPins() {
    try { return JSON.parse(localStorage.getItem('dashhq_pinned_tools') || '["ticker","gas","rug"]'); }
    catch (e) { return ['ticker', 'gas', 'rug']; }
  }
  function renderPins() {
    var pins = getPins();
    var el = document.getElementById('profilePins');
    if (!pins.length) { el.innerHTML = '<div class="pin-empty">No pinned tools yet.</div>'; return; }
    el.innerHTML = pins.map(function (k) {
      return '<div class="pin-chip" onclick="Hub.go(\'toolkit\');Hub.openTool(\'' + k + '\');"><svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">' + (PIN_ICONS[k] || '') + '</svg><span>' + (TOOL_LABELS[k] || k) + '</span></div>';
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
