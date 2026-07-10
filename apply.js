/* ════════════════════════════════════════
   Dash HQ — Application Form Engine
   One question per step, validation, review, submit.
   ════════════════════════════════════════ */
var Apply = (function(){
  var BACKEND_URL = ['localhost', '127.0.0.1'].includes(window.location.hostname)
    ? 'http://localhost:8000'
    : 'https://www.dashhq.site';
  var SUBMIT_ENDPOINT = BACKEND_URL + '/applications';

  var STEP_ORDER = [0,1,2,3,4,5,6,7,'done'];
  var idx = 0;
  var visited = { dashhqx:false, alvin:false, schoolboy:false, dee:false };
  var submitting = false;

  function wordCount(v){ return v.trim().split(/\s+/).filter(Boolean).length; }

  var FIELDS = [
    { step:1, key:'name', el:'f-name', label:'Name / Alias', type:'input', validate:v=>v.trim().length>0 },
    { step:2, key:'x', el:'f-x', label:'X Profile', type:'input', validate:v=>/^(https?:\/\/)?(www\.)?(x\.com|twitter\.com)\/[A-Za-z0-9_]{2,}\/?$|^@?[A-Za-z0-9_]{2,}$/.test(v.trim()) },
    { step:3, key:'intro', el:'f-intro', label:'Intro & Role', type:'textarea', validate:v=>wordCount(v)>=8 },
    { step:4, key:'communities', el:'f-communities', label:'Communities', type:'textarea', validate:v=>wordCount(v)>=2 },
    { step:5, key:'value', el:'f-value', label:'How You Add Value', type:'textarea', validate:v=>wordCount(v)>=8 }
  ];

  function el(sel){ return document.querySelector(sel); }
  function stepEl(s){ return document.querySelector('.step[data-step="'+s+'"]'); }

  function updateProgress(){
    var pos = STEP_ORDER.indexOf(idx);
    var pct = (pos/(STEP_ORDER.length-1))*100;
    el('#progressFill').style.width = pct+'%';
  }

  function updateNav(){
    var nav = el('#stepNav');
    if(idx===0 || idx==='done'){ nav.classList.add('hide'); return; }
    nav.classList.remove('hide');
    el('#backBtn').style.visibility = idx===1 ? 'hidden' : 'visible';
    el('#nextBtn').textContent = idx===7 ? 'Submit Application →' : 'Continue →';
  }

  function show(newIdx){
    var cur = stepEl(idx);
    var next = stepEl(newIdx);
    if(cur) cur.classList.remove('on','err');
    idx = newIdx;
    if(next) next.classList.add('on');
    if(idx===7) buildReview();
    updateProgress(); updateNav();
    window.scrollTo({top:0,behavior:'smooth'});
  }

  function validateStep(s){
    var f = FIELDS.find(function(x){return x.step===s;});
    if(f){
      var v = el('#'+f.el).value;
      var ok = f.validate(v);
      stepEl(s).classList.toggle('err', !ok);
      return ok;
    }
    if(s===6){
      var ok6 = visited.dashhqx && visited.alvin && visited.schoolboy && visited.dee;
      stepEl(6).classList.toggle('err', !ok6);
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
      {k:'Name / Alias', v:d.name, step:1},
      {k:'X Profile', v:d.x, step:2},
      {k:'Intro & Role', v:d.intro, step:3},
      {k:'Communities', v:d.communities, step:4},
      {k:'Adding Value', v:d.value, step:5},
      {k:'Followed the team', v:'Yes — all three confirmed', step:6}
    ];
    el('#reviewList').innerHTML = rows.map(function(r){
      return '<div class="review-row" onclick="Apply.jump('+r.step+')"><div class="rl"><div class="k">'+r.k+'</div><div class="v">'+escapeHtml(r.v)+'</div></div><span class="redit">Edit</span></div>';
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
      show('done');
    } catch(e) {
      setSubmitError('Could not reach the server. Check your connection and try again.');
    } finally {
      submitting = false;
      if(nextBtn){ nextBtn.disabled = false; nextBtn.textContent = idx===7 ? 'Submit Application →' : 'Continue →'; }
    }
  }

  function next(){
    if(idx==='done' || submitting) return;
    if(idx!==0 && !validateStep(idx)) return;
    var pos = STEP_ORDER.indexOf(idx);
    if(idx===7){ submit(); return; }
    show(STEP_ORDER[pos+1]);
  }
  function back(){
    var pos = STEP_ORDER.indexOf(idx);
    if(pos>0) show(STEP_ORDER[pos-1]);
  }
  function jump(s){ show(s); }

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
      if(idx===0 || idx==='done') { if(idx===0){ e.preventDefault(); next(); } return; }
      var tag = document.activeElement.tagName;
      if(tag==='TEXTAREA') return; // allow newlines
      e.preventDefault();
      next();
    });
  }

  function init(){
    updateProgress(); updateNav();
    bindFollowChips(); bindEnterKey();
  }

  return { next:next, back:back, jump:jump, init:init };
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
