/* ════ PARTICLE NETWORK ════ */
(function(){
  const c=document.getElementById('particles'); if(!c) return;
  const x=c.getContext('2d'); let w,h,pts,raf;
  function size(){const r=c.parentElement.getBoundingClientRect();w=c.width=r.width;h=c.height=r.height;}
  function init(){size();const n=Math.min(70,Math.floor(w*h/16000));pts=[];for(let i=0;i<n;i++)pts.push({x:Math.random()*w,y:Math.random()*h,vx:(Math.random()-.5)*.28,vy:(Math.random()-.5)*.28});}
  function loop(){
    x.clearRect(0,0,w,h);
    for(const p of pts){p.x+=p.vx;p.y+=p.vy;if(p.x<0||p.x>w)p.vx*=-1;if(p.y<0||p.y>h)p.vy*=-1;}
    for(let i=0;i<pts.length;i++){
      for(let j=i+1;j<pts.length;j++){
        const dx=pts[i].x-pts[j].x,dy=pts[i].y-pts[j].y,d=Math.hypot(dx,dy);
        if(d<132){x.strokeStyle='rgba(91,155,248,'+(.16*(1-d/132))+')';x.lineWidth=1;x.beginPath();x.moveTo(pts[i].x,pts[i].y);x.lineTo(pts[j].x,pts[j].y);x.stroke();}
      }
      x.fillStyle='rgba(120,160,255,.7)';x.beginPath();x.arc(pts[i].x,pts[i].y,1.5,0,7);x.fill();
    }
    raf=requestAnimationFrame(loop);
  }
  init();loop();
  addEventListener('resize',()=>{cancelAnimationFrame(raf);init();loop();});
})();

/* ════ REVEAL ON SCROLL ════ */
(function(){
  const io=new IntersectionObserver((es)=>{
    es.forEach(e=>{ e.target.classList.toggle('in', e.isIntersecting); });
  },{threshold:.16,rootMargin:'0px 0px -8% 0px'});
  // curtain elements travel off-screen, so observing them directly never fires.
  // Observe everything EXCEPT curtains here; curtains are driven by their parent below.
  document.querySelectorAll('[data-reveal]:not([data-reveal^="curtain"])').forEach(el=>io.observe(el));

  // Split-curtain groups: observe a stable parent, toggle the curtain children.
  const gio=new IntersectionObserver((es)=>{
    es.forEach(e=>{
      const on=e.isIntersecting;
      e.target.querySelectorAll('[data-reveal^="curtain"]').forEach(k=>k.classList.toggle('in',on));
    });
  },{threshold:.12,rootMargin:'0px 0px -12% 0px'});
  document.querySelectorAll('.story-copy, .timeline').forEach(g=>gio.observe(g));
})();

/* ════ COUNTERS ════ */
(function(){
  const io=new IntersectionObserver((es)=>{
    es.forEach(e=>{ if(!e.isIntersecting) return; const el=e.target; io.unobserve(el);
      const t=+el.dataset.target, u=el.querySelector('.u'), us=u?u.outerHTML:''; let s=null;
      function step(ts){ if(!s)s=ts; const p=Math.min((ts-s)/1400,1); const ease=1-Math.pow(1-p,3);
        el.innerHTML=Math.round(t*ease)+us; if(p<1)requestAnimationFrame(step); }
      requestAnimationFrame(step);
    });
  },{threshold:.5});
  document.querySelectorAll('.stat-n').forEach(el=>io.observe(el));
})();

/* ════ TIMELINE DRAW ════ */
(function(){
  const tl=document.getElementById('timeline'), prog=document.getElementById('tlProg');
  if(!tl) return; const items=[...tl.querySelectorAll('.tl-item')];
  function upd(){
    const r=tl.getBoundingClientRect(), vh=innerHeight;
    const start=vh*0.8, end=vh*0.25;
    let p=(start-r.top)/(r.height+ (start-end)); p=Math.max(0,Math.min(1,p));
    prog.style.height=(p*100)+'%';
    const lit=r.top+r.height*p;
    items.forEach(it=>{const ir=it.querySelector('.node').getBoundingClientRect();
      it.classList.toggle('lit', ir.top < (window.scrollY? 0:0)+ (innerHeight*0.55));});
  }
  addEventListener('scroll',upd,{passive:true}); addEventListener('resize',upd); upd();
})();

/* ════ MAGNETIC BUTTONS ════ */
(function(){
  document.querySelectorAll('.magnetic').forEach(el=>{
    let rx=0,ry=0,cx=0,cy=0,raf;
    function move(e){const r=el.getBoundingClientRect();rx=(e.clientX-(r.left+r.width/2))/r.width*22;ry=(e.clientY-(r.top+r.height/2))/r.height*22;tick();}
    function tick(){cx+=(rx-cx)*.2;cy+=(ry-cy)*.2;el.style.transform='translate('+cx.toFixed(2)+'px,'+cy.toFixed(2)+'px)';if(Math.abs(rx-cx)>.1||Math.abs(ry-cy)>.1)raf=requestAnimationFrame(tick);}
    el.addEventListener('pointermove',move);
    el.addEventListener('pointerleave',()=>{rx=0;ry=0;tick();});
  });
})();

/* ════ HAPTIC SQUARES (tilt + press + touch jiggle) ════ */
(function(){
  document.querySelectorAll('.haptic').forEach(el=>{
    let tx=0,ty=0,rx=0,ry=0,sc=1, dx=0,dy=0,drx=0,dry=0,dsc=1, raf=null, hov=false;
    function run(){
      tx+=(dx-tx)*.16; ty+=(dy-ty)*.16; rx+=(drx-rx)*.16; ry+=(dry-ry)*.16; sc+=(dsc-sc)*.18;
      el.style.transform='perspective(700px) translate3d('+tx.toFixed(2)+'px,'+ty.toFixed(2)+'px,0) rotateX('+rx.toFixed(2)+'deg) rotateY('+ry.toFixed(2)+'deg) scale('+sc.toFixed(3)+')';
      if(Math.abs(dx-tx)>.1||Math.abs(dy-ty)>.1||Math.abs(drx-rx)>.05||Math.abs(dry-ry)>.05||Math.abs(dsc-sc)>.002){raf=requestAnimationFrame(run);} else {raf=null;}
    }
    function kick(){ if(!raf) raf=requestAnimationFrame(run); }
    el.addEventListener('pointermove',e=>{
      const r=el.getBoundingClientRect(); const px=(e.clientX-(r.left+r.width/2))/(r.width/2); const py=(e.clientY-(r.top+r.height/2))/(r.height/2);
      el.style.setProperty('--mx',((e.clientX-r.left)/r.width*100)+'%'); el.style.setProperty('--my',((e.clientY-r.top)/r.height*100)+'%');
      dry=px*7; drx=-py*7; dx=px*5; dy=py*5; dsc=hov?1.03:1; kick();
    });
    el.addEventListener('pointerenter',()=>{hov=true;dsc=1.03;kick();});
    el.addEventListener('pointerleave',()=>{hov=false;dx=dy=drx=dry=0;dsc=1;kick();});
    el.addEventListener('pointerdown',()=>{dsc=.96;kick();});
    el.addEventListener('pointerup',()=>{dsc=hov?1.03:1;kick();});
    // touch jiggle
    el.addEventListener('touchstart',()=>{dx=6;dy=-6;dsc=.97;kick();setTimeout(()=>{dx=dy=0;dsc=1;kick();},160);},{passive:true});
  });
})();

/* ════ CUBE: drag + emerge-on-scroll (desktop only — see below for touch) ════ */
(function(){
  const stage=document.getElementById('cubeStage'); if(!stage) return;
  const cube=stage.querySelector('.cube-svg-el'); if(!cube) return;
  // Touch devices: skip the whole continuous-transform interactive loop.
  // A heavy multi-gradient SVG getting its transform rewritten every frame
  // forever (idle wobble, even at rest) has proven unreliable on some real
  // iOS Safari devices — the cube silently fails to render at all on some
  // hardware while working fine on others. Rendering it fully static on
  // touch removes that whole risk category; desktop keeps the full effect.
  if(window.matchMedia('(hover:none)').matches) return;
  let tx=0,ty=0,rx=0,ry=0, dtx=0,dty=0,drx=0,dry=0, dragging=false,sx=0,sy=0,bx=0,by=0, emerge=0, t=0;
  const LIM={tx:60,ty:44,rx:14,ry:20};
  const clamp=(v,m)=>v<-m?-m:(v>m?m:v);
  stage.addEventListener('pointermove',e=>{ if(dragging)return; const r=stage.getBoundingClientRect(); const px=(e.clientX-(r.left+r.width/2))/(r.width/2); const py=(e.clientY-(r.top+r.height/2))/(r.height/2); dry=clamp(px*8,LIM.ry); drx=clamp(-py*6,LIM.rx); dtx=clamp(px*12,LIM.tx); dty=clamp(py*9,LIM.ty); });
  stage.addEventListener('pointerleave',()=>{ if(!dragging){dtx=dty=drx=dry=0;} });
  cube.addEventListener('pointerdown',e=>{ dragging=true;sx=e.clientX;sy=e.clientY;bx=tx;by=ty;cube.style.cursor='grabbing'; try{cube.setPointerCapture(e.pointerId)}catch(_){ } e.preventDefault(); });
  addEventListener('pointermove',e=>{ if(!dragging)return; const ddx=e.clientX-sx,ddy=e.clientY-sy; dtx=clamp(bx+ddx,LIM.tx); dty=clamp(by+ddy,LIM.ty); dry=clamp(dtx/LIM.tx*LIM.ry,LIM.ry); drx=clamp(-dty/LIM.ty*LIM.rx,LIM.rx); });
  addEventListener('pointerup',()=>{ if(!dragging)return; dragging=false; cube.style.cursor='grab'; dtx=dty=drx=dry=0; });
  // emerge: cube rises + scales as hero scrolls
  function onScroll(){ const sc=window.scrollY; const vh=innerHeight; emerge=Math.max(0,Math.min(1,sc/(vh*0.9))); }
  addEventListener('scroll',onScroll,{passive:true}); onScroll();
  (function loop(){ t+=0.016;
    const idle=dragging?0:Math.sin(t*0.9)*5, idleR=dragging?0:Math.sin(t*0.7)*1.2, k=dragging?0.22:0.09;
    tx+=(dtx-tx)*k; ty+=((dty+idle)-ty)*k; rx+=(drx-rx)*k; ry+=((dry+idleR)-ry)*k;
    const eScale=1+emerge*0.16, eY=-emerge*70;
    cube.style.transform='translate3d('+tx.toFixed(2)+'px,'+(ty+eY).toFixed(2)+'px,0) rotateX('+rx.toFixed(2)+'deg) rotateY('+ry.toFixed(2)+'deg) scale('+eScale.toFixed(3)+')';
    requestAnimationFrame(loop);
  })();
})();

/* ════ RESEARCH READER ════ */
(function(){
  function init(){
  var POSTS=[
    {id:'nft101',title:'Understanding NFTs and the Blockchain',author:'@LuvThePisces',url:'https://x.com/LuvThePisces',date:'Jul 3, 2026',dateShort:'Jul 3',read:'3 min read',excerpt:'NFTs, Crypto, Blockchain, Web3 — demystified together so you can finally get with the times.',body:'<p>In the 21st century, you must have come across terms like NFTs, Crypto, Blockchain, or as collectively called, Web3, and wondered what it is all about.</p><p>In this article, we will demystify these concepts together so you can finally get with the times.</p><p>To understand these, let&rsquo;s begin with the foundation: Blockchain.</p><h4>The blockchain</h4><p>The blockchain is essentially a digital ledger or a record book that lives entirely on the internet.</p><p>Imagine a physical notebook where you write down every single transaction you make. Now picture that exact same notebook copied thousands of times across computers all over the world.</p><p>So many computers hold the exact same copy, so no single person can secretly change a record without everyone else noticing. This shared setup means the system is completely decentralised.</p><p>As defined by Cambridge Dictionary:</p><div class="rv-def"><div class="t">Decentralised</div><div class="p">Adjective &middot; /diːˈsɚntrəlʌizd/</div><p class="d">Used to describe organisations or their activities which are not controlled from one central place, but happen in many different places.</p></div><p>So, no governing body controls the setup. Instead of a superior, the entire network of computers works together to verify every new piece of data. Once that data is verified, it gets locked into a secure block of information.</p><p>That block is then linked securely to the previous one, creating an unbreakable chain. This chain of blocks is what keeps everything incredibly secure and permanent.</p><h4>So what is an NFT?</h4><p>Building on that permanent foundation, we can start talking about NFTs. NFT stands for Non-Fungible Token.</p><div class="rv-def"><div class="t">Token</div><div class="p">Noun &middot; /ˈtəʊ.kən/</div><p class="d">A piece of paper, a card, or an electronic document with a particular amount of money shown on it that can be exchanged in a shop or online for goods of that value.</p></div><p>To understand what a token like that means, we first need to look at the word fungible. A fungible item is something that can be easily swapped for an identical item of the same value.</p><p>For example, a typical currency bill (Naira, Dollar, Euros) is fungible because you can trade it for any other bill of the same denomination and still have the exact same purchasing power. Cryptocurrencies like Bitcoin, Solana, and Ethereum work in the same way.</p><p>A non-fungible item is the direct opposite of that swapping concept. Something that is non-fungible is entirely unique and cannot be swapped one-for-one with anything else.</p><p>For example, consider an original painting such as the Mona Lisa, or a specific Real Estate property. Each is a one-of-a-kind physical item.</p><p>An NFT take the concept of unique ownership and brings it directly into the digital world. When you buy (the right term is mint) an NFT, you are actually buying a digital certificate of authenticity for that digital item.</p><p>That item could be a piece of art, a video clip, or even a virtual plot of land. Remember, everything is stored permanently on the blockchain, including the digital certificate.</p><p>Storing it there proves beyond a doubt that you are the sole owner of that digital asset. Even if someone right clicks and saves a copy of the image associated with your NFT, they do not own the actual underlying token.</p><p>Owning the verified token on the blockchain is what holds the real value. The blockchain serves as your indisputable proof of ownership.</p><p>Understanding this relationship is the absolute key to seeing why these digital assets have become so massively popular today.</p>'},
    {id:'alpha101',title:'Alpha 101',author:'@0xschlboy',url:'https://x.com/0xschlboy',date:'Jul 8, 2026',dateShort:'Jul 8',read:'4 min read',excerpt:'You\'ve seen “alpha” thrown around Crypto Twitter. What it actually is, where it lives, and why it always seems to reach everyone else first.',body:'<p>You&rsquo;ve probably seen the word &ldquo;alpha&rdquo; thrown around all over Crypto Twitter. Most of the time, it&rsquo;s attached to screenshots of ridiculous green PnLs, people saying &ldquo;free alpha,&rdquo; or someone claiming they &ldquo;caught the move early.&rdquo;</p><p>Naturally, it makes you wonder. What exactly is alpha? Where do people even find it? And why does it always seem like everyone else gets it before you do?</p><p>This article aims to answer those questions and, hopefully, leave you with a completely different way of thinking about alpha.</p><h4>I. Alpha isn&rsquo;t static. It follows attention.</h4><p>Before anything else, here&rsquo;s a simple definition. Alpha is information, insight, or conviction that helps you spot an opportunity before the majority of the market does.</p><p>Now here&rsquo;s where many beginners get it wrong. They think alpha is some secret Discord server, private Telegram group, or hidden wallet they simply haven&rsquo;t discovered yet. It isn&rsquo;t.</p><p>Alpha is constantly moving. It follows wherever attention, narratives, and ultimately, volume decide to go.</p><p>Crypto is one giant ecosystem made up of countless smaller ones. We&rsquo;ve got DeFi, memecoins, NFTs, prediction markets, perpetuals, AI, RWAs, tokenized stocks, privacy protocols and dozens of other sectors, each competing for the market&rsquo;s attention.</p><p>Some narratives emerge because of genuine technological breakthroughs. Others explode because of culture, speculation, memes, or even a single tweet from a founder. The reason doesn&rsquo;t always matter. What matters is that attention creates volume, and volume creates opportunity.</p><p>A collection sells out overnight because collectors can&rsquo;t stop talking about the art. A founder posts an innocent meme that suddenly becomes the face of an entire ecosystem. A new financial primitive launches and everyone rushes to be early. These moments are where alpha tends to appear.</p><p>The mistake most newcomers make is looking for yesterday&rsquo;s alpha. By the time everyone is talking about an opportunity, the market is often preparing to move somewhere else.</p><p>So if you&rsquo;re searching for alpha, don&rsquo;t just ask yourself, &ldquo;What&rsquo;s pumping today?&rdquo; Instead, ask: &ldquo;What is everyone beginning to pay attention to?&rdquo;</p><p>Because in crypto, attention becomes volume, volume creates opportunity, and opportunity is where alpha lives.</p><h4>II. Don&rsquo;t confuse price with being late.</h4><p>One of the biggest misconceptions beginners have is believing that once a token has gone up, the opportunity is gone. &ldquo;I&rsquo;ve missed it.&rdquo; &ldquo;It&rsquo;s already up 10x.&rdquo; &ldquo;I&rsquo;m too late.&rdquo;</p><p>While that can sometimes be true, it&rsquo;s a dangerous mindset to adopt as a rule. Entries are subjective. The &ldquo;perfect early entry&rdquo; you wish you had was likely someone else&rsquo;s exit. Likewise, your entry today could become another person&rsquo;s &ldquo;I wish I bought there&rdquo; a month from now.</p><p>What matters isn&rsquo;t how much a token has already moved. What matters is whether the narrative still has attention. As long as people are still talking about it, volume continues to flow, and new participants are entering the market, opportunities can still exist.</p><p>Don&rsquo;t judge an opportunity solely by how much the price has increased. Judge it by the motion of the story behind it and properly evaluate whether there&rsquo;s still much it could do.</p><h4>III. Alpha rewards those who notice and act.</h4><p>Finally, finding alpha isn&rsquo;t just about seeing opportunities. It&rsquo;s about having the conviction to act on them before the majority does.</p><p>Imagine this. You come across a token sitting at a $200K market cap. You like the idea, but you convince yourself the market is weak and there&rsquo;s no way it goes much higher. So you watch from the sidelines.</p><p>A few days later it&rsquo;s trading at a $4M market cap. Now you&rsquo;re certain you&rsquo;ve missed it. Other people, however, are only just discovering it. They buy because they believe the narrative still has room to run.</p><p>Weeks later, the token reaches a $95M market cap. The opportunity wasn&rsquo;t hidden from you. You saw it, you simply didn&rsquo;t act.</p><p>That doesn&rsquo;t mean you should ape into every token you see, far from it. It means that seeing an opportunity and recognizing an opportunity are two different things.</p><p>Alpha rewards people who pay attention. It rewards people who do the research. And most importantly, it rewards people who are willing to act when their conviction tells them they&rsquo;re early, even if the chart has already moved.</p><p>Because in crypto, hesitation can be just as expensive as being wrong.</p>'},
    {id:'keys',title:'Not Your Keys, Not Your Coins: A Field Guide to Self-Custody',author:'Dash Team',date:'Jun 24, 2026',dateShort:'Jun 24',read:'2 min read',excerpt:'The most repeated phrase in crypto. Here is what self-custody actually asks of you, and how people lose funds anyway.',body:'<p>Every cycle the same sentence ends up on t-shirts and in group chats: not your keys, not your coins. It sounds like a slogan. It is closer to a warning.</p><p>When you hold crypto on an exchange, you do not hold crypto. You hold a promise from a company that says you can withdraw later. Most of the time that promise is good. In November 2022, FTX customers found out what happens when it is not. Balances still showed on the screen. The coins behind them were already gone.</p><p>Self-custody is the alternative. You control the private key, so you control the asset. No support desk, no withdrawal limits, no one standing between you and your money. That freedom arrives with a bill, and the bill is responsibility.</p><h4>What a wallet actually is</h4><p>A wallet does not store coins. The coins live on the blockchain. The wallet stores a private key, a long secret number that proves you are allowed to move funds at your address. Your seed phrase, usually twelve or twenty-four words, is that key written down in a form you can back up. Anyone who reads those words can empty the wallet. That is the entire security model.</p><p>Which is why words on paper, kept offline, beat a screenshot in your camera roll. The screenshot syncs to the cloud. The cloud gets breached.</p><h4>Hot, cold, and the gap between them</h4><p>A hot wallet like MetaMask runs on a device that is connected to the internet. It is convenient and fine for the small amounts you actually spend. A cold wallet like a Ledger or Trezor keeps the key on a chip that never touches the web, signing transactions in isolation. For anything you would be sick to lose, cold storage is the answer.</p><p>The common mistake is treating a hardware wallet as magic. It is not. Type your seed phrase into a website because a pop-up asked politely, and the device did its job while you still got drained. The hardware protects the key. It cannot protect you from approving your own theft.</p><h4>How people really lose funds</h4><p>Rarely through cryptography. Almost always through people. A fake airdrop page asks you to connect and verify. A helpful stranger slides into your DMs and asks for your phrase to fix a problem you did not have. A malicious approval quietly hands a contract permission to spend your USDC for as long as the wallet exists.</p><p>Two habits kill most of the risk. Never enter your seed phrase anywhere except when you are physically restoring a wallet you set up yourself. And check your token approvals now and then, cancelling the ones you stopped using. Self-custody is not hard. It is unforgiving. Learn the handful of rules once and the freedom pays for itself.</p>'}
  ];
  var modal=document.getElementById('researchModal'); if(!modal) return;
  var grid=document.getElementById('rvGrid'), article=document.getElementById('rvArticle');
  var cards=document.getElementById('rvCards'), panel=document.getElementById('rvPanel');
  function cardHTML(p){return '<article class="post" role="button" tabindex="0" onclick="openPost(\''+p.id+'\')"><div class="thumb"><div class="glow"></div><div class="strip"></div></div><div class="pbody"><h3>'+p.title+'</h3><p class="pex">'+p.excerpt+'</p><div class="pmeta"><span>'+p.author+'</span><span class="sep">•</span><span>'+p.read+'</span></div></div></article>';}
  function buildGrid(){cards.innerHTML=POSTS.map(cardHTML).join('');[].forEach.call(cards.children,function(el,i){el.style.animationDelay=(i*55)+'ms';});}
  function openModal(){modal.classList.add('open');modal.setAttribute('aria-hidden','false');document.body.style.overflow='hidden';}
  window.openResearch=function(){buildGrid();article.classList.remove('on');grid.classList.add('on');openModal();requestAnimationFrame(function(){modal.scrollTop=0;});};
  window.showGrid=function(){buildGrid();article.classList.remove('on');grid.classList.add('on');requestAnimationFrame(function(){modal.scrollTop=0;});};
  window.openPost=function(id){var p=null;for(var i=0;i<POSTS.length;i++){if(POSTS[i].id===id){p=POSTS[i];break;}}if(!p)return;
    var initial=p.author.replace(/[^a-zA-Z]/g,'').charAt(0).toUpperCase();
    var authorHTML=p.url?('<a href="'+p.url+'" target="_blank" rel="noopener" style="color:var(--sky)">'+p.author+'</a>'):p.author;
    panel.style.animation='none';
    void panel.offsetWidth;
    panel.innerHTML='<div class="rv-hero"><div class="g"></div><div class="s"></div></div><h1>'+p.title+'</h1><div class="rv-meta"><span>'+authorHTML+'</span><span class="sep">•</span><span>'+p.read+'</span></div><p class="rv-lede">'+p.excerpt+'</p><div class="rv-body">'+p.body+'</div><div class="rv-foot"><div class="av">'+initial+'</div><div><div style="color:#fff;font-weight:600">'+authorHTML+'</div><div style="font-size:12px;color:var(--muted)">Dash HQ Research</div></div></div>';
    grid.classList.remove('on');article.classList.add('on');openModal();
    requestAnimationFrame(function(){panel.style.animation='';modal.scrollTop=0;});};
  window.closeResearch=function(){modal.classList.remove('open');modal.setAttribute('aria-hidden','true');document.body.style.overflow='';};
  // Intentional backdrop dismiss: close only on a deliberate tap on the empty
  // glass area — same element down+up, negligible movement (so scrolls, drags
  // and text selection never trigger it), and never on a card, panel or control.
  var downT=null,downX=0,downY=0;
  modal.addEventListener('pointerdown',function(e){downT=e.target;downX=e.clientX;downY=e.clientY;});
  modal.addEventListener('pointerup',function(e){
    var t=e.target;
    var dead = t===modal || t.classList.contains('rm-scroll') || t.id==='rvGrid' || t.id==='rvArticle' || t.classList.contains('rv-cards') || t.classList.contains('rv-backwrap');
    var moved=Math.hypot(e.clientX-downX,e.clientY-downY);
    if(dead && t===downT && moved<8) closeResearch();
  });
  window.addEventListener('keydown',function(e){if(e.key==='Escape'&&modal.classList.contains('open'))closeResearch();});
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();

/* ════ SCROLL PROGRESS BAR (smooth lerp) ════ */
(function(){
  var bar=document.getElementById('scrollProgress'); if(!bar) return;
  var cur=0, target=0;
  function measure(){
    var st=window.scrollY||document.documentElement.scrollTop;
    var h=document.documentElement.scrollHeight-window.innerHeight;
    target=h>0?(st/h):0; if(target<0)target=0; if(target>1)target=1;
  }
  addEventListener('scroll',measure,{passive:true});
  addEventListener('resize',measure); measure(); cur=target;
  (function loop(){
    cur+=(target-cur)*0.12;
    if(Math.abs(target-cur)<0.0002) cur=target;
    bar.style.transform='scaleX('+cur.toFixed(4)+')';
    requestAnimationFrame(loop);
  })();
})();

/* ════ NAV ACTIVE LINK ════ */
(function(){
  const links=[...document.querySelectorAll('.nav-links a')];
  const map=links.map(a=>{var h=a.getAttribute('href');return {a,sec:(h&&h.length>1&&h.charAt(0)==='#')?document.querySelector(h):null};}).filter(x=>x.sec);
  function upd(){ const y=window.scrollY+140; let cur=map[0];
    for(const m of map){ if(m.sec.offsetTop<=y) cur=m; }
    links.forEach(a=>a.classList.remove('active')); if(cur)cur.a.classList.add('active'); }
  addEventListener('scroll',upd,{passive:true}); upd();
})();

/* ════ MOBILE NAV DRAWER ════ */
(function(){
  const t=document.querySelector('.nav-toggle'), dr=document.querySelector('.nav-drawer'), nav=document.querySelector('nav');
  if(!t||!dr||!nav)return;
  function place(){ dr.style.top=nav.offsetHeight+'px'; }
  function close(){ t.classList.remove('open'); dr.classList.remove('open'); t.setAttribute('aria-expanded','false'); }
  function toggle(){ place(); const open=!dr.classList.contains('open'); dr.classList.toggle('open',open); t.classList.toggle('open',open); t.setAttribute('aria-expanded',open?'true':'false'); }
  t.addEventListener('click',toggle);
  dr.querySelectorAll('a').forEach(a=>a.addEventListener('click',close));
  addEventListener('resize',()=>{ place(); if(innerWidth>1000)close(); });
  place();
})();

/* ════ JS MARQUEE — CSS animation breaks inside mask containers on iOS Safari ════ */
(function(){
  var track=document.querySelector('.voices-track');
  if(!track)return;
  if(window.matchMedia('(prefers-reduced-motion:reduce)').matches)return;
  var half=0,pos=0,paused=false,last=null;
  var marquee=track.parentElement;
  // Prime a GPU layer before the loop starts
  track.style.transform='translate3d(0,0,0)';
  if(window.matchMedia('(hover:hover)').matches){
    marquee.addEventListener('mouseenter',function(){paused=true;});
    marquee.addEventListener('mouseleave',function(){paused=false;});
  }
  // press/touch to pause on mobile — hold a testimonial to read it, release to resume
  marquee.addEventListener('pointerdown',function(){paused=true;});
  window.addEventListener('pointerup',function(){paused=false;});
  window.addEventListener('pointercancel',function(){paused=false;});
  marquee.addEventListener('touchstart',function(){paused=true;},{passive:true});
  window.addEventListener('touchend',function(){paused=false;});
  function step(ts){
    if(!half){
      half=track.scrollWidth/2;
      // scrollWidth not ready yet (layout still pending) — retry next frame
      if(half<200){half=0;last=null;requestAnimationFrame(step);return;}
    }
    if(!last)last=ts;
    var dt=Math.min(ts-last,50);last=ts;
    if(!paused){
      pos+=(half/48000)*dt;
      if(pos>=half)pos-=half;
      // translate3d forces GPU compositing on iOS Safari; translateX does not
      track.style.transform='translate3d(-'+pos.toFixed(2)+'px,0,0)';
    }
    requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
})();

/* ════ CLEAN HASH URLS — intercept every #anchor click site-wide ════ */
(function(){
  document.addEventListener('click',function(e){
    var a=e.target.closest('a[href^="#"]');
    if(!a)return;
    var hash=a.getAttribute('href');
    e.preventDefault();
    if(hash&&hash.length>1){
      var target=document.querySelector(hash);
      if(target)target.scrollIntoView({behavior:'smooth'});
    }else{
      window.scrollTo({top:0,behavior:'smooth'});
    }
    history.replaceState(null,'','/');
  });
})();

/* ════ LEGAL MODALS ════ */
(function(){
  var CONTACT='dashhqmain@gmail.com';
  var content={
    terms:{
      title:'Terms of Service',
      html:''
        +'<p>Welcome to Dash HQ. By accessing our website, joining our Discord community, or subscribing to our newsletter, you agree to these Terms of Service. If you do not agree, please do not use our services.</p>'
        +'<h4>Who We Are</h4>'
        +'<p>Dash HQ is a Web3 community and collective of NFT collectors, on-chain analysts, and crypto-native individuals. We share curated market commentary, on-chain research, and analysis through a private Discord server, our presence on X, and this website.</p>'
        +'<h4>Not Financial Advice</h4>'
        +'<p>Everything shared by Dash HQ, including alpha calls, mint analysis, research, and any other content, is for informational and educational purposes only. It is not financial, investment, legal, or tax advice. Crypto and NFTs are highly volatile, and you can lose your entire investment. You are solely responsible for your own decisions and for doing your own research (DYOR).</p>'
        +'<h4>Membership &amp; Conduct</h4>'
        +'<p>Citizenship is application-based and granted at our discretion. To remain a member, you agree to treat others with respect and to not:</p>'
        +'<ul><li>Harass, threaten, scam, or defraud other members.</li><li>Share paid Dash HQ content, calls, or research outside the community.</li><li>Impersonate the team or other members.</li><li>Post spam, malware, or malicious links.</li></ul>'
        +'<p>We may suspend or remove access at any time for conduct that harms the community, with or without notice.</p>'
        +'<h4>No Guarantees</h4>'
        +'<p>We work hard to bring quality signal, but we make no guarantee about the accuracy, profitability, or outcome of any information shared. Past performance is not indicative of future results. Services are provided on an "as is" and "as available" basis.</p>'
        +'<h4>Intellectual Property</h4>'
        +'<p>The Dash HQ name, logo, written research, and original content are our property and may not be copied or redistributed for commercial use without permission.</p>'
        +'<h4>Third-Party Platforms</h4>'
        +'<p>We rely on third parties such as Discord, X, and Substack. Your use of those platforms is also governed by their own terms, and we are not responsible for their availability or actions.</p>'
        +'<h4>Limitation of Liability</h4>'
        +'<p>To the fullest extent permitted by law, Dash HQ and its team will not be liable for any losses or damages arising from your use of our services or reliance on any content, including trading and investment losses.</p>'
        +'<h4>Changes</h4>'
        +'<p>We may update these Terms from time to time. Continued use of our services after changes means you accept the updated Terms.</p>'
        +'<h4>Contact</h4>'
        +'<p>Questions about these Terms? Reach us at <a href="mailto:'+CONTACT+'">'+CONTACT+'</a>.</p>'
    },
    privacy:{
      title:'Privacy Policy',
      html:''
        +'<p>This Privacy Policy explains what information Dash HQ collects, why, and how we handle it. We keep data collection to a minimum.</p>'
        +'<h4>Information We Collect</h4>'
        +'<ul>'
        +'<li><strong>Newsletter email:</strong> When you subscribe to the Dash Dispatch, your email address is collected and stored by Substack to deliver our newsletter.</li>'
        +'<li><strong>Discord data:</strong> If you join our community, your Discord username and profile are visible to us and other members, as with any Discord server.</li>'
        +'<li><strong>Application details:</strong> Information you choose to share when applying for citizenship.</li>'
        +'</ul>'
        +'<h4>What We Do Not Collect</h4>'
        +'<p>This website does not run invasive tracking or sell data. We never ask for your private keys, seed phrases, or wallet credentials, and we will never DM you asking for them.</p>'
        +'<h4>How We Use Your Information</h4>'
        +'<ul><li>To send you the newsletter you signed up for.</li><li>To review membership applications and run the community.</li><li>To improve our content and communicate with members.</li></ul>'
        +'<h4>Third-Party Services</h4>'
        +'<p>We use Substack (newsletter), Discord (community), and X (social). When you interact with those, their own privacy policies apply. We recommend reviewing them.</p>'
        +'<h4>Your Choices</h4>'
        +'<p>You can unsubscribe from the newsletter at any time using the link in any email, and you may leave the Discord community whenever you like. To request the deletion of information you have shared with us directly, contact us.</p>'
        +'<h4>Security</h4>'
        +'<p>We take reasonable steps to protect the limited information we hold, but no method of transmission or storage is 100% secure.</p>'
        +'<h4>Changes</h4>'
        +'<p>We may update this policy occasionally. Material changes will be reflected by the "last updated" date above.</p>'
        +'<h4>Contact</h4>'
        +'<p>Questions about privacy? Reach us at <a href="mailto:'+CONTACT+'">'+CONTACT+'</a>.</p>'
    }
  };
  var ov=document.getElementById('legalOv'), body=document.getElementById('legalBody'),
      title=document.getElementById('legalTitle'), eyebrow=document.getElementById('legalEyebrow'),
      box=document.querySelector('.legal-box');
  window.openLegal=function(which){
    var c=content[which]; if(!c) return;
    title.textContent=c.title; eyebrow.textContent=which==='terms'?'Legal':'Privacy';
    body.innerHTML=c.html; body.scrollTop=0;
    ov.classList.add('open'); document.body.style.overflow='hidden';
  };
  window.closeLegal=function(){ ov.classList.remove('open'); document.body.style.overflow=''; };
  window.addEventListener('keydown',function(e){ if(e.key==='Escape'&&ov.classList.contains('open'))closeLegal(); });
})();
(function(){
  // Substack's subscribe API blocks direct fetch() from other origins via
  // its own bot protection (confirmed: real 403s). Rather than fight that,
  // this form opens Substack's real subscribe page in a new tab with the
  // email prefilled — a normal navigation, not an API call, so it can't be
  // silently blocked the way the old fetch(mode:'no-cors') version was.
  var SUBSTACK_PUB = "dashhq1";
  const f=document.getElementById('subForm'); if(!f) return;
  const email=document.getElementById('subEmail'), msg=document.getElementById('subMsg');
  const row=f.querySelector('.sub-row');
  const EMAIL_RE=/^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  function show(text,ok){ msg.textContent=text; msg.className='sub-msg show '+(ok?'ok':'err'); }
  function reason(val){
    if(!val) return 'Please enter your email address.';
    if(val.indexOf('@')<0) return 'That email is missing an “@”.';
    const parts=val.split('@');
    if(parts.length>2) return 'An email can only contain one “@”.';
    if(!parts[0]) return 'Add the part before the “@”.';
    if(!parts[1]||parts[1].indexOf('.')<0) return 'Add the domain (e.g. name@gmail.com).';
    if(/\.$/.test(val)||/\.\./.test(val)) return 'That domain looks incomplete.';
    return 'That email address looks incomplete.';
  }
  email.addEventListener('input',function(){ row.classList.remove('valid','invalid'); msg.className='sub-msg'; });
  f.addEventListener('submit',function(e){
    e.preventDefault();
    const val=(email.value||'').trim();
    if(!EMAIL_RE.test(val)){ row.classList.add('invalid'); row.classList.remove('valid'); show(reason(val),false); email.focus(); return; }
    row.classList.add('valid'); row.classList.remove('invalid');
    window.open('https://'+SUBSTACK_PUB+'.substack.com/subscribe?email='+encodeURIComponent(val),'_blank','noopener');
    show('Opening Substack in a new tab — confirm there to finish subscribing.',true);
    f.reset(); row.classList.remove('valid');
  });
})();


(function(){var el=document.getElementById('cursorGhost');if(!el)return;var tx=innerWidth/2,ty=innerHeight/2,cx=tx,cy=ty,shown=false;function m(e){var p=e.touches?e.touches[0]:e;tx=p.clientX;ty=p.clientY;if(!shown){shown=true;el.style.opacity=1;}}window.addEventListener('pointermove',m,{passive:true});window.addEventListener('pointerdown',m,{passive:true});document.addEventListener('mouseleave',function(){el.style.opacity=0;shown=false;});(function loop(){cx+=(tx-cx)*0.14;cy+=(ty-cy)*0.14;el.style.transform='translate('+cx.toFixed(1)+'px,'+cy.toFixed(1)+'px) translate(-50%,-50%)';requestAnimationFrame(loop);})();})();
