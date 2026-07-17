/* ════════════════════════════════════════
   Dash HQ — Application Form Engine
   One question per step, validation, review, submit.
   ════════════════════════════════════════ */
var Apply = (function(){
  var BACKEND_URL = ['localhost', '127.0.0.1'].includes(window.location.hostname)
    ? 'http://localhost:8000'
    : 'https://www.dashhq.site';
  var SUBMIT_ENDPOINT = BACKEND_URL + '/applications';

  var STEP_ORDER = [0,'connect',1,2,3,4,5,6,'done'];
  var idx = 0;
  var visited = { dashhqx:false, alvin:false, schoolboy:false, dee:false };
  var submitting = false;
  var identity = null; // { x_user_id, x_username, token }

  function wordCount(v){ return v.trim().split(/\s+/).filter(Boolean).length; }

  var FIELDS = [
    { step:1, key:'name', el:'f-name', label:'Name / Alias', type:'input', validate:v=>v.trim().length>0 },
    { step:2, key:'intro', el:'f-intro', label:'Intro & Role', type:'textarea', validate:v=>wordCount(v)>=8 },
    { step:3, key:'communities', el:'f-communities', label:'Communities', type:'textarea', validate:v=>wordCount(v)>=2 },
    { step:4, key:'value', el:'f-value', label:'How You Add Value', type:'textarea', validate:v=>wordCount(v)>=8 }
  ];

  function el(sel){ return document.querySelector(sel); }
  function stepEl(s){ return document.querySelector('#applyCard .step[data-step="'+s+'"]'); }

  function updateProgress(){
    var pos = STEP_ORDER.indexOf(idx);
    var pct = (pos/(STEP_ORDER.length-1))*100;
    el('#progressFill').style.width = pct+'%';
  }

  function updateNav(){
    var nav = el('#stepNav');
    if(idx===0 || idx==='done' || idx==='connect' || idx==='blocked'){ nav.classList.add('hide'); return; }
    nav.classList.remove('hide');
    el('#backBtn').style.visibility = idx===1 ? 'hidden' : 'visible';
    el('#nextBtn').textContent = idx===6 ? 'Submit Application →' : 'Continue →';
  }

  function show(newIdx){
    var cur = stepEl(idx);
    var next = stepEl(newIdx);
    if(cur) cur.classList.remove('on','err');
    idx = newIdx;
    if(next) next.classList.add('on');
    if(idx===6) buildReview();
    if(typeof idx === 'number' && idx>=1) renderConnectedChip();
    updateProgress(); updateNav();
    window.scrollTo({top:0,behavior:'smooth'});
  }

  function renderConnectedChip(){
    var chip = document.getElementById('connectedChip');
    if(!chip) return;
    chip.textContent = identity ? ('Connected as @' + identity.x_username) : '';
  }

  function validateStep(s){
    var f = FIELDS.find(function(x){return x.step===s;});
    if(f){
      var v = el('#'+f.el).value;
      var ok = f.validate(v);
      stepEl(s).classList.toggle('err', !ok);
      return ok;
    }
    if(s===5){
      var ok6 = visited.dashhqx && visited.alvin && visited.schoolboy && visited.dee;
      stepEl(5).classList.toggle('err', !ok6);
      return ok6;
    }
    return true;
  }

  function getData(){
    var d = {};
    FIELDS.forEach(function(f){ d[f.key] = el('#'+f.el).value.trim(); });
    return d;
  }

  function buildReview(){
    var d = getData();
    var rows = [
      {k:'Connected as', v:'@' + (identity ? identity.x_username : ''), step:null},
      {k:'Name / Alias', v:d.name, step:1},
      {k:'Intro & Role', v:d.intro, step:2},
      {k:'Communities', v:d.communities, step:3},
      {k:'Adding Value', v:d.value, step:4},
      {k:'Followed the team', v:'Yes, all four confirmed', step:5}
    ];
    el('#reviewList').innerHTML = rows.map(function(r){
      var editable = r.step!==null;
      return '<div class="review-row'+(editable?'':' no-edit')+'"'+(editable?' onclick="Apply.jump('+r.step+')"':'')+'><div class="rl"><div class="k">'+r.k+'</div><div class="v">'+escapeHtml(r.v)+'</div></div>'+(editable?'<span class="redit">Edit</span>':'')+'</div>';
    }).join('');
  }

  function escapeHtml(s){
    return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function setSubmitError(msg){
    var err = document.getElementById('submitError');
    if(!err) return;
    err.textContent = msg || '';
    err.style.display = msg ? 'block' : 'none';
  }

  async function submit(){
    if(submitting) return;
    var hp = document.getElementById('f-hp');
    var payload = getData();
    payload.followedTeam = true;
    payload.token = identity ? identity.token : '';

    var nextBtn = document.getElementById('nextBtn');
    submitting = true;
    setSubmitError(null);
    if(nextBtn){ nextBtn.disabled = true; nextBtn.textContent = 'Submitting…'; }

    try {
      // Honeypot: a hidden field real users never fill. If it's populated,
      // pretend to succeed without actually submitting — don't tip off bots.
      if(hp && hp.value){ show('done'); return; }

      var res = await fetch(SUBMIT_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if(!res.ok){
        var msg = 'Something went wrong submitting your application. Please try again.';
        try { var body = await res.json(); if(body.detail && typeof body.detail === 'string') msg = body.detail; } catch(e){}
        setSubmitError(msg);
        return;
      }
      clearSavedToken();
      show('done');
    } catch(e) {
      setSubmitError('Could not reach the server. Check your connection and try again.');
    } finally {
      submitting = false;
      if(nextBtn){ nextBtn.disabled = false; nextBtn.textContent = idx===6 ? 'Submit Application →' : 'Continue →'; }
    }
  }

  function next(){
    if(idx==='done' || idx==='connect' || idx==='blocked' || submitting) return;
    if(idx!==0 && !validateStep(idx)) return;
    var pos = STEP_ORDER.indexOf(idx);
    if(idx===6){ submit(); return; }
    show(STEP_ORDER[pos+1]);
  }
  function back(){
    var pos = STEP_ORDER.indexOf(idx);
    if(pos>0) show(STEP_ORDER[pos-1]);
  }
  function jump(s){ show(s); }

  function connectX(){
    window.location.href = BACKEND_URL + '/auth/x?intent=apply';
  }

  function reapply(){
    Status.close();
    clearSavedToken();
    show(0);
    next(); // straight into the connect step
  }

  var TOKEN_KEY = 'dashhq_apply_identity';
  function saveIdentity(tok){
    try {
      var payload = JSON.parse(atob(tok.split('.')[1]));
      identity = { x_user_id: payload.x_user_id, x_username: payload.x_username, token: tok };
      sessionStorage.setItem(TOKEN_KEY, tok);
    } catch(e){ identity = null; }
  }
  function clearSavedToken(){
    identity = null;
    try { sessionStorage.removeItem(TOKEN_KEY); } catch(e){}
  }

  function bindFollowChips(){
    document.querySelectorAll('.follow-chip').forEach(function(chip){
      chip.addEventListener('click',function(){
        var key = chip.dataset.key;
        visited[key] = true;
        chip.classList.add('visited');
      });
    });
  }

  function bindEnterKey(){
    document.addEventListener('keydown',function(e){
      if(e.key!=='Enter') return;
      if(document.getElementById('statusCard').style.display !== 'none') return;
      if(idx===0 || idx==='done' || idx==='connect' || idx==='blocked') { if(idx===0){ e.preventDefault(); next(); } return; }
      var tag = document.activeElement.tagName;
      if(tag==='TEXTAREA') return; // allow newlines
      e.preventDefault();
      next();
    });
  }

  function bindCharCounts(){
    ['intro','communities','value'].forEach(function(key){
      var f = FIELDS.find(function(x){ return x.key === key; });
      var input = document.getElementById(f.el);
      var counter = document.getElementById('cc-' + key);
      if(!input || !counter) return;
      function update(){
        var len = input.value.length;
        counter.textContent = len + ' / 600';
        counter.classList.toggle('near-limit', len >= 480 && len < 600);
        counter.classList.toggle('at-limit', len >= 600);
      }
      input.addEventListener('input', update);
      update();
    });
  }

  function handleUrlParams(){
    var params = new URLSearchParams(window.location.search);
    var token = params.get('token');
    var xerror = params.get('xerror');

    if(params.get('view')==='status'){
      history.replaceState({}, '', window.location.pathname);
      Status.open(token);
      return;
    }

    if(token){
      history.replaceState({}, '', window.location.pathname);
      saveIdentity(token);
      show(1); // identity confirmed, skip straight to the first real question
      return;
    }

    if(xerror === 'already_applied'){
      var handle = params.get('handle') || '';
      var status = params.get('status') || 'pending';
      history.replaceState({}, '', window.location.pathname);
      var word = status === 'accepted' ? 'accepted' : 'still pending review';
      document.getElementById('blockedHandle').textContent = '@' + handle;
      document.getElementById('blockedText').innerHTML = 'Connected as <strong>@'+escapeHtml(handle)+'</strong>. Your existing application is <strong>'+word+'</strong>, so a new one can\'t be started right now.';
      show('blocked');
      return;
    }

    if(xerror){
      history.replaceState({}, '', window.location.pathname);
      show('connect');
      stepEl('connect').classList.add('err');
    }
  }

  function init(){
    updateProgress(); updateNav();
    bindFollowChips(); bindEnterKey(); bindCharCounts();
    handleUrlParams();
  }

  return { next:next, back:back, jump:jump, init:init, connectX:connectX, reapply:reapply };
})();

/* ════ APPLICATION STATUS CHECK ════ */
var Status = (function(){
  var BACKEND_URL = ['localhost', '127.0.0.1'].includes(window.location.hostname)
    ? 'http://localhost:8000'
    : 'https://www.dashhq.site';

  function statusStepEl(s){ return document.querySelector('#statusCard .step[data-status="'+s+'"]'); }
  function show(s){
    document.querySelectorAll('#statusCard .step').forEach(function(e){ e.classList.remove('on'); });
    var next = statusStepEl(s);
    if(next) next.classList.add('on');
  }

  function connectX(){
    window.location.href = BACKEND_URL + '/auth/x?intent=status';
  }

  function fmtDate(iso){
    try { return new Date(iso).toLocaleDateString(undefined, { year:'numeric', month:'long', day:'numeric' }); }
    catch(e){ return ''; }
  }

  async function fetchStatus(token){
    show('loading');
    try {
      var res = await fetch(BACKEND_URL + '/applications/status?token=' + encodeURIComponent(token));
      if(!res.ok){
        var msg = 'We couldn\'t check your status just now. Please try again.';
        try { var body = await res.json(); if(body.detail) msg = body.detail; } catch(e){}
        document.getElementById('statusErrorText').textContent = msg;
        show('error');
        return;
      }
      var data = await res.json();
      render(data);
    } catch(e){
      document.getElementById('statusErrorText').textContent = 'Could not reach the server. Check your connection and try again.';
      show('error');
    }
  }

  function render(data){
    if(!data.found){
      document.getElementById('nfHandle').textContent = data.x_username ? ('@' + data.x_username) : 'that account';
      show('notfound');
      return;
    }
    if(data.status === 'pending'){
      document.getElementById('pendingName').textContent = data.name;
      document.getElementById('pendingMeta').textContent = 'Submitted ' + fmtDate(data.submitted_at) + ' · connected as @' + data.x_username;
      show('pending');
      return;
    }
    if(data.status === 'accepted'){
      document.getElementById('acceptName').textContent = data.name;
      var inviteBtn = document.getElementById('inviteBtn');
      if(data.invite_url){
        inviteBtn.href = data.invite_url;
        inviteBtn.style.display = '';
      } else {
        inviteBtn.style.display = 'none';
      }
      show('accepted');
      fireConfetti();
      return;
    }
    if(data.status === 'declined'){
      document.getElementById('declineReason').textContent = data.decline_reason || 'No reason provided.';
      show('declined');
      return;
    }
  }

  function open(existingToken){
    document.getElementById('applyCard').style.display = 'none';
    document.getElementById('statusCard').style.display = 'flex';
    document.getElementById('stepNav').classList.add('hide');
    document.getElementById('progressTrack').style.display = 'none';
    if(existingToken){
      fetchStatus(existingToken);
    } else {
      show('entry');
    }
  }

  function close(){
    document.getElementById('statusCard').style.display = 'none';
    document.getElementById('applyCard').style.display = 'flex';
    document.getElementById('progressTrack').style.display = '';
  }

  /* One-shot confetti burst for the accepted screen: small tinted squares
     falling with light drift and gravity, fading out - not a persistent
     background effect, just a celebratory moment. */
  function fireConfetti(){
    var c = document.getElementById('confetti');
    if(!c) return;
    var x = c.getContext('2d');
    c.width = innerWidth; c.height = innerHeight;
    var colors = ['#5B9BF8','#4D72FF','#22D3EE','#10B981','#F59E0B','#fff'];
    var pieces = [];
    var n = Math.min(140, Math.floor(innerWidth/8));
    for(var i=0;i<n;i++){
      pieces.push({
        x: Math.random()*c.width,
        y: -20 - Math.random()*c.height*0.5,
        w: 6+Math.random()*6,
        h: 8+Math.random()*10,
        rot: Math.random()*Math.PI*2,
        vr: (Math.random()-0.5)*0.2,
        vy: 2+Math.random()*3,
        vx: (Math.random()-0.5)*2,
        color: colors[Math.floor(Math.random()*colors.length)],
        life: 1
      });
    }
    var start = performance.now();
    function loop(t){
      var elapsed = t - start;
      x.clearRect(0,0,c.width,c.height);
      var alive = false;
      pieces.forEach(function(p){
        if(elapsed < 5200){
          p.x += p.vx; p.y += p.vy; p.rot += p.vr;
          if(elapsed > 3200) p.life = Math.max(0, 1 - (elapsed-3200)/2000);
          if(p.y < c.height+20) alive = true;
          x.save();
          x.globalAlpha = p.life;
          x.translate(p.x,p.y); x.rotate(p.rot);
          x.fillStyle = p.color;
          x.fillRect(-p.w/2,-p.h/2,p.w,p.h);
          x.restore();
        }
      });
      if(alive && elapsed < 5200) requestAnimationFrame(loop);
      else x.clearRect(0,0,c.width,c.height);
    }
    requestAnimationFrame(loop);
  }

  return { open:open, close:close, connectX:connectX };
})();

document.addEventListener('DOMContentLoaded', Apply.init);

/* ════ PARTICLE NETWORK (matches site background) ════ */
(function(){
  const c=document.getElementById('particles');if(!c)return;const x=c.getContext('2d');let w,h,pts,raf;
  function size(){w=c.width=innerWidth;h=c.height=innerHeight;}
  function init(){size();const n=Math.min(60,Math.floor(w*h/20000));pts=[];for(let i=0;i<n;i++)pts.push({x:Math.random()*w,y:Math.random()*h,vx:(Math.random()-.5)*.24,vy:(Math.random()-.5)*.24});}
  function loop(){x.clearRect(0,0,w,h);for(const p of pts){p.x+=p.vx;p.y+=p.vy;if(p.x<0||p.x>w)p.vx*=-1;if(p.y<0||p.y>h)p.vy*=-1;}
    for(let i=0;i<pts.length;i++){for(let j=i+1;j<pts.length;j++){const dx=pts[i].x-pts[j].x,dy=pts[i].y-pts[j].y,d=Math.hypot(dx,dy);if(d<128){x.strokeStyle='rgba(91,155,248,'+(.13*(1-d/128))+')';x.lineWidth=1;x.beginPath();x.moveTo(pts[i].x,pts[i].y);x.lineTo(pts[j].x,pts[j].y);x.stroke();}}x.fillStyle='rgba(120,160,255,.55)';x.beginPath();x.arc(pts[i].x,pts[i].y,1.3,0,7);x.fill();}
    raf=requestAnimationFrame(loop);}
  init();loop();addEventListener('resize',()=>{cancelAnimationFrame(raf);init();loop();});
})();
