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

/* ════ CUBE: drag + emerge-on-scroll ════ */
(function(){
  const stage=document.getElementById('cubeStage'); if(!stage) return;
  const cube=stage.querySelector('.cube-svg-el'); if(!cube) return;
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
    {id:"nft101",title:"Understanding NFTs and the Blockchain",author:"@LuvThePisces",url:"https://x.com/LuvThePisces",date:"Jul 3, 2026",dateShort:"Jul 3",read:"3 min read",
     excerpt:"NFTs, crypto, blockchain, Web3 — demystified from the ground up. Start with the foundation and finally get with the times.",
     body:"<p>In the 21st century, you've probably come across terms like NFTs, Crypto, Blockchain — or, collectively, Web3 — and wondered what it's all about. In this article we'll demystify these concepts together, so you can finally get with the times.</p><p>To understand them, let's begin with the foundation: the blockchain.</p><h4>Start with the foundation: the blockchain</h4><p>The blockchain is essentially a digital ledger — a record book that lives entirely on the internet. Imagine a physical notebook where you write down every single transaction you make. Now picture that exact same notebook copied thousands of times across computers all over the world.</p><p>So many computers hold the exact same copy that no single person can secretly change a record without everyone else noticing. This shared setup means the system is completely decentralised.</p><div class=\"rv-def\"><div class=\"t\">Decentralised</div><div class=\"p\">adjective · /diːˈsɛntrəlʌɪzd/</div><p class=\"d\">Used to describe organisations or their activities which are not controlled from one central place, but happen in many different places.</p></div><p>So no governing body controls the setup. Instead of a superior, the entire network of computers works together to verify every new piece of data. Once that data is verified, it gets locked into a secure block of information.</p><p>That block is then linked securely to the previous one, creating an unbreakable chain. This chain of blocks is what keeps everything incredibly secure and permanent.</p><h4>So what is an NFT?</h4><p>Building on that permanent foundation, we can start talking about NFTs. NFT stands for Non-Fungible Token.</p><div class=\"rv-def\"><div class=\"t\">Token</div><div class=\"p\">noun · /ˈtəʊ.kən/</div><p class=\"d\">A piece of paper, a card, or an electronic document with a particular amount of money shown on it that can be exchanged in a shop or online for goods of that value.</p></div><p>To understand what such a token means, we first need to look at the word fungible. A fungible item is something that can be easily swapped for an identical item of the same value. A typical currency note (Naira, Dollar, Euro) is fungible — you can trade it for any other note of the same denomination and still have the exact same purchasing power. Cryptocurrencies like Bitcoin, Solana and Ethereum work the same way.</p><p>A non-fungible item is the direct opposite. Something non-fungible is entirely unique and cannot be swapped one-for-one with anything else. Consider an original painting such as the Mona Lisa, or a specific piece of real estate — each is a one-of-a-kind item.</p><h4>Unique ownership, brought onchain</h4><p>An NFT takes the concept of unique ownership and brings it directly into the digital world. When you buy — the correct term is mint — an NFT, you are actually buying a digital certificate of authenticity for that digital item. That item could be a piece of art, a video clip, or even a virtual plot of land.</p><p>Remember, everything is stored permanently on the blockchain, including that digital certificate. Storing it there proves beyond doubt that you are the sole owner of the digital asset.</p><p>Even if someone right-clicks and saves a copy of the image associated with your NFT, they do not own the actual underlying token. Owning the verified token on the blockchain is what holds the real value — the blockchain serves as your indisputable proof of ownership.</p><p>Understanding this relationship is the absolute key to seeing why these digital assets have become so massively popular today.</p>"},
    {id:"keys",title:"Not Your Keys, Not Your Coins: A Field Guide to Self-Custody",author:"Dash Team",date:"Jun 24, 2026",dateShort:"Jun 24",read:"2 min read",
     excerpt:"The most repeated phrase in crypto. Here is what self-custody actually asks of you, and how people lose funds anyway.",body:"<p>Every cycle the same sentence ends up on t-shirts and in group chats: not your keys, not your coins. It sounds like a slogan. It is closer to a warning.</p><p>When you hold crypto on an exchange, you do not hold crypto. You hold a promise from a company that says you can withdraw later. Most of the time that promise is good. In November 2022, FTX customers found out what happens when it is not. Balances still showed on the screen. The coins behind them were already gone.</p><p>Self-custody is the alternative. You control the private key, so you control the asset. No support desk, no withdrawal limits, no one standing between you and your money. That freedom arrives with a bill, and the bill is responsibility.</p><h4>What a wallet actually is</h4><p>A wallet does not store coins. The coins live on the blockchain. The wallet stores a private key, a long secret number that proves you are allowed to move funds at your address. Your seed phrase, usually twelve or twenty-four words, is that key written down in a form you can back up. Anyone who reads those words can empty the wallet. That is the entire security model.</p><p>Which is why words on paper, kept offline, beat a screenshot in your camera roll. The screenshot syncs to the cloud. The cloud gets breached.</p><h4>Hot, cold, and the gap between them</h4><p>A hot wallet like MetaMask runs on a device that is connected to the internet. It is convenient and fine for the small amounts you actually spend. A cold wallet like a Ledger or Trezor keeps the key on a chip that never touches the web, signing transactions in isolation. For anything you would be sick to lose, cold storage is the answer.</p><p>The common mistake is treating a hardware wallet as magic. It is not. Type your seed phrase into a website because a pop-up asked politely, and the device did its job while you still got drained. The hardware protects the key. It cannot protect you from approving your own theft.</p><h4>How people really lose funds</h4><p>Rarely through cryptography. Almost always through people. A fake airdrop page asks you to connect and verify. A helpful stranger slides into your DMs and asks for your phrase to fix a problem you did not have. A malicious approval quietly hands a contract permission to spend your USDC for as long as the wallet exists.</p><p>Two habits kill most of the risk. Never enter your seed phrase anywhere except when you are physically restoring a wallet you set up yourself. And check your token approvals now and then, cancelling the ones you stopped using. Self-custody is not hard. It is unforgiving. Learn the handful of rules once and the freedom pays for itself.</p>"},
    {id:"gas",title:"Gas, Explained: Why Your Transaction Costs What It Costs",author:"Dash Team",date:"Jun 21, 2026",dateShort:"Jun 21",read:"2 min read",
     excerpt:"Gwei, base fees, priority tips. A plain look at what you are actually paying for every time you hit confirm.",body:"<p>Send a transaction on Ethereum during a busy hour and the fee can jump from a couple of dollars to fifty. The number feels random. It is not.</p><p>Every action on Ethereum uses computation, and computation is metered in units called gas. Moving ETH from one address to another costs 21,000 gas. Swapping tokens on Uniswap costs a lot more, because the network is running a lot more code for you. Gas measures work, not money.</p><h4>Turning gas into a bill</h4><p>The price of each unit is quoted in gwei, a tiny slice of an ETH. One gwei is a billionth of one. Your fee is roughly the gas used times the price per gas. Same swap, higher gwei, bigger bill. That is how an identical trade costs three dollars on a quiet Sunday and thirty in the middle of a Monday mint.</p><p>Block space is the reason behind the swings. Each block fits a limited amount of gas. When more people want in than there is room for, they bid against one another and the price climbs until demand cools off.</p><h4>What EIP-1559 changed</h4><p>In August 2021 Ethereum split the fee in two. There is a base fee the network sets on its own, based on how full the previous block was, and that part gets burned, taken out of supply for good. Sitting on top is a priority fee, the tip you offer a validator to include you sooner.</p><p>Pick fast in your wallet and you are raising the tip. When the chain is congested, the base fee rises by itself, block after block, until the crowd thins. Once you can see those two pieces, sticky fees during a hot event stop being a mystery.</p><h4>Paying less without gambling</h4><p>Timing helps. Fees follow human schedules, so weekend and off-peak hours across the US and Europe tend to run cheaper. If a transaction is not urgent, let it wait.</p><p>Layer 2 networks like Arbitrum, Base and Optimism help far more. They bundle many transactions together and settle them on Ethereum in bulk, so each person pays a sliver of the cost. The swap that runs thirty dollars on mainnet often costs a few cents on an L2. Gas is not a tax somebody imposes on you. It is an auction for limited space that reruns every twelve seconds. See it that way and the number stops being scary.</p>"},
    {id:"royalties",title:"Where NFT Royalties Went, and Why It Turned Into a War",author:"Dash Team",date:"Jun 18, 2026",dateShort:"Jun 18",read:"2 min read",
     excerpt:"Creator royalties were the promise that pulled artists into NFTs. Then the marketplaces quietly stopped enforcing them.",body:"<p>The pitch that pulled artists into NFTs was simple and genuinely new. Sell a piece once, then earn a cut every time it changes hands after that, automatically, with no gallery and no chasing invoices. For the first time a digital artist could get paid on the secondary market the way almost no one before them had.</p><p>Then the mechanism turned out to be much softer than everyone assumed.</p><h4>Royalties were never enforced by the chain</h4><p>Here is the part most people skipped past. When a creator sets a 5 or 10 percent royalty, Ethereum does not enforce that number. It is a request that marketplaces choose to honor. OpenSea honored it for years, so it felt like a law of physics. It was always just a convention.</p><p>In late 2022 a wave of new marketplaces competed on price, and the quickest way to undercut everyone was to make royalties optional, then to drop them altogether. Blur, built for professional traders, pushed royalties toward zero and took market share in weeks. The traders followed the cheaper venue, because traders always do. Artists watched a revenue stream they had built businesses around thin out over a single quarter.</p><h4>The counterattacks</h4><p>Creators and platforms fought back in code. OpenSea shipped an on-chain filter that blocked sales on any marketplace ignoring royalties. Blur engineered its way around the filter. The two traded moves for the better part of a year, and collections were stuck in the crossfire, forced to choose which marketplace their art would even work on.</p><p>A few teams went further and enforced payment at the contract level, making a transfer fail unless the fee was paid. It worked, and it irritated traders, who prize the ability to move assets without asking permission.</p><h4>Where it settled</h4><p>The dust left a messier and more honest picture. Pure royalties are hard to force on an open network where anyone can spin up a rival venue overnight. Newer collections lean on other tools instead: holder allowlists, token-gated perks, and primary sales priced with the assumption that secondary income might never show up.</p><p>The lesson is worth sitting with if you build anything onchain. A rule that only works while everyone agrees to follow it is not a rule. It is a handshake. Design as though the handshake will break, because given enough time it does.</p>"},
    {id:"amm",title:"How a Swap Really Works: AMMs and Liquidity Pools",author:"Dash Team",date:"Jun 14, 2026",dateShort:"Jun 14",read:"2 min read",
     excerpt:"There is no order book behind most token swaps. There is a formula, a pool, and math worth knowing before you provide liquidity.",body:"<p>On a normal exchange a trade needs a buyer and a seller who agree on a price, and an order book matches them. Most decentralized swaps do not work like this at all, and the difference matters the moment you think about earning yield by providing liquidity.</p><h4>The pool replaces the counterparty</h4><p>Uniswap and the venues like it run on automated market makers. Instead of pairing two people, you trade against a pool of tokens that other users deposited. Swapping ETH for USDC means adding ETH to the pool and taking USDC out. Nobody has to be standing on the other side at that exact second.</p><p>Price comes from a formula. The classic version is x times y equals k, where x and y are the two token balances and k stays fixed. Pull USDC out and its balance falls while ETH rises, so the pool quotes a slightly worse ETH price on your next unit. Large trades move the price more, and that movement is what people mean by slippage.</p><h4>Who fills the pool, and why</h4><p>Liquidity providers do. They deposit both tokens and collect a share of every swap fee in return, often 0.3 percent per trade. In an active pool those fees pile up, and that is the yield pulling capital into DeFi.</p><p>There is a catch with an awkward name: impermanent loss. When one token moves sharply against the other, the pool rebalances in a way that leaves you holding less value than if you had simply kept both tokens in your wallet. Fees can outweigh the gap. Sometimes they do not.</p><h4>A concrete picture</h4><p>Say you provide ETH and USDC while ETH sits at 2,000 dollars, and ETH then doubles to 4,000. The pool sold some of your ETH into USDC on the way up, because that is how it holds the formula together. You still made money, just less than the person who did nothing and held. That gap is impermanent loss, and it only turns permanent when you withdraw.</p><p>None of this asks you to trust a company. The pool is a public contract, the math is out in the open, and anyone can read the balances. Before you deposit, run the numbers on a volatile pair against a stablecoin pair. The boring pools are often the ones worth being in.</p>"},
    {id:"stablecoins",title:"Stablecoins Are Not All the Same",author:"Dash Team",date:"Jun 10, 2026",dateShort:"Jun 10",read:"2 min read",
     excerpt:"They all say one dollar. What backs that dollar is where they split, and where they break.",body:"<p>A stablecoin is a token that aims to stay worth one dollar. That is the easy part. The hard part, the part that decides whether it survives a bad week, is what actually stands behind the dollar.</p><h4>Backed by real dollars</h4><p>USDC and USDT are the big ones. The idea is plain. For every token in circulation the issuer claims to hold a dollar, or something close to it like short-term Treasuries, in a bank. Redeem a token, get a dollar back, and the peg holds because the reserve is real.</p><p>The risk here is trust and disclosure. You are relying on the issuer to hold what they say and to let you redeem it. USDC briefly slipped to 88 cents in March 2023 when part of its reserves sat inside Silicon Valley Bank as the bank collapsed. It recovered once the deposits were guaranteed, but the scare was a reminder that backed by dollars still depends entirely on where those dollars are parked.</p><h4>Backed by crypto</h4><p>DAI takes another route. It is backed by crypto assets locked in smart contracts, and because crypto swings hard, the system insists on more collateral than the value it issues. Lock 150 dollars of ETH, mint 100 DAI. If your collateral falls too far, the contract sells it automatically to keep the system solvent. More transparent than a bank, and more exposed to a violent crash.</p><h4>Backed by faith</h4><p>Then came algorithmic stablecoins, which tried to hold the peg with code and incentives rather than real reserves. Terra's UST is the one everyone remembers. It leaned on a sister token, LUNA, to soak up pressure. In May 2022 confidence cracked, the mechanism spun the wrong way, and something close to 40 billion dollars vanished in a matter of days.</p><p>The wreck taught a lesson that has aged well. A peg held together by belief alone works right up until people stop believing, and then it goes all at once. When you hold a stablecoin you are not holding a dollar. You are holding a claim, and the quality of that claim is the only thing that counts when the market turns violent.</p>"},
    {id:"explorer",title:"Read the Chain: Using a Block Explorer Like You Mean It",author:"Dash Team",date:"Jun 5, 2026",dateShort:"Jun 5",read:"2 min read",
     excerpt:"Etherscan is public, free, and tells you more than most paid dashboards. Here is how to actually read it.",body:"<p>One of the stranger gifts of public blockchains is that everything is visible. Every transaction, every wallet, every contract sits out in the open. The tool for reading it is a block explorer, and Etherscan is where most people start. Learning to use it properly is a real edge.</p><h4>Reading a transaction</h4><p>Paste any transaction hash and the page lays out the whole story. Who sent it, who received it, how much moved, what it cost in gas, and whether it went through or reverted. A failed transaction that still charged a fee confuses newcomers constantly. The network did the work of trying, so the attempt still costs money even when the action never completed.</p><p>The from and to fields matter more than they look. When to points at a contract rather than a wallet, you were talking to code, and the logs underneath show exactly what that code did with your funds.</p><h4>Following the money</h4><p>Drop in a wallet address and you can walk through its entire history. This is how people track what a project treasury is really doing, or notice that a supposedly anonymous founder funded their wallet from an exchange account that ties back to a real name. Nothing is hidden. It just takes patience to read.</p><p>Holder lists tell their own story. If ten wallets sit on ninety percent of a token supply, no amount of marketing changes what that means for you as a late buyer.</p><h4>The safety habit that saves wallets</h4><p>Etherscan has a token approvals page, and it is the one most people never open. Every time you use a dapp you often grant a contract permission to move a token for you, and plenty of those permissions are unlimited and stay live until you cancel them. Months later a forgotten approval on a contract that later gets exploited is how old wallets get emptied long after the owner moved on.</p><p>Look at your approvals every so often. Revoke anything you do not recognise or no longer use. It costs a little gas and shuts a door that attackers count on you leaving open. You do not need to be a developer to read the chain. You need the willingness to look. The information that separates a careful holder from exit liquidity is sitting right there, free, for anyone who bothers.</p>"},
    {id:"bridges",title:"What Actually Happens When You Bridge Tokens",author:"Dash Team",date:"Jun 2, 2026",dateShort:"Jun 2",read:"2 min read",
     excerpt:"Bridges move value between chains, and they have lost more money to hackers than almost anything else in crypto.",body:"<p>You have ETH on Ethereum and you want to use it on Arbitrum or Solana. The chains do not talk to each other natively, so you reach for a bridge. Understanding what a bridge actually does explains why so many of them have been robbed.</p><h4>Your coins do not travel</h4><p>A token cannot leave the chain it lives on. When you bridge ETH to another network, the ETH does not move. It gets locked in a contract on Ethereum, and the bridge mints a matching token on the other side that stands in for your claim. That new token is an IOU, and its value rests entirely on the locked collateral staying safe.</p><p>This is why bridged assets often carry a different name, like a wrapped or chain-specific version. You are not holding the original. You are holding a receipt that can be redeemed for it.</p><h4>Why bridges get drained</h4><p>All that locked collateral sits in one place, which turns a bridge into a vault with a target painted on it. If an attacker finds a flaw in the contract or steals the keys that authorise minting, they can print IOUs backed by nothing, or empty the vault directly.</p><p>The numbers are brutal. The Ronin bridge lost around 600 million dollars in 2022 after attackers compromised validator keys. Wormhole lost roughly 320 million the same year through a signature flaw. Bridges have produced some of the largest single thefts the industry has seen.</p><h4>Bridging with your eyes open</h4><p>Not every bridge carries the same risk. A native bridge run by a chain's own team tends to be more battle-tested than a random third party promising the lowest fee. Check how long it has run, whether it has been audited, and how much value it holds.</p><p>For large amounts, moving in stages beats sending everything in one shot. And once your funds land on the destination chain, there is rarely a reason to leave them in a wrapped form longer than you need. A bridge is a way to get somewhere, not a place to keep money. Treat the crossing as the dangerous part and get off it quickly.</p>"},
    {id:"consensus",title:"Proof of Work vs Proof of Stake",author:"Dash Team",date:"May 29, 2026",dateShort:"May 29",read:"2 min read",
     excerpt:"Two ways to agree on who owns what, with very different costs. One burns electricity, the other locks up capital.",body:"<p>Every blockchain has to solve one problem before anything else works. With no central authority, how does the network agree on which transactions are real? The answer is a consensus mechanism, and the two that matter are proof of work and proof of stake.</p><h4>Proof of work</h4><p>Bitcoin uses this. Computers around the world race to solve a hard puzzle, and the first to crack it adds the next block and takes the reward. The puzzle is expensive on purpose. Solving it burns real electricity on specialised machines, and that cost is the security. To rewrite history an attacker would have to out-spend the entire honest network, which is wildly impractical at Bitcoin's scale.</p><p>The trade-off is energy. Bitcoin mining draws about as much power as a mid-sized country, and that has drawn steady criticism.</p><h4>Proof of stake</h4><p>Ethereum switched to this in September 2022, in the upgrade called the Merge. Rather than burning electricity, validators lock up ETH as collateral, and the network chooses who proposes each block. Behave honestly and you earn rewards. Cheat and the network destroys part of your stake, a penalty called slashing.</p><p>Security here comes from money at risk instead of energy spent. The Merge cut Ethereum's power use by roughly 99.9 percent overnight, one of the largest efficiency jumps any network has managed.</p><h4>Which is better</h4><p>It depends on what you value. Proof of work is simpler, older, and has never been broken on Bitcoin, but it is heavy on power. Proof of stake is efficient and flexible, though it can concentrate influence among those who already hold the most coins.</p><p>Neither is obviously right. They are different bets on how to buy security, one paid in electricity and the other in locked capital. Knowing which a chain uses tells you a lot about its priorities before you read a word of its marketing.</p>"},
    {id:"airdrops",title:"Airdrops: Free Money With Strings Attached",author:"Dash Team",date:"May 26, 2026",dateShort:"May 26",read:"1 min read",
     excerpt:"Projects hand out tokens to early users. The catch is that thousands of people now game the process for a living.",body:"<p>An airdrop is when a project gives free tokens to people who used it early. Uniswap did the famous one in 2020, dropping 400 UNI on every past user, worth thousands of dollars at the time. That single event rewired how people approach new protocols.</p><h4>Why projects give tokens away</h4><p>It is marketing and decentralisation at once. Handing tokens to real users spreads ownership, rewards the people who showed up early, and creates a crowd with a reason to care. Done well, an airdrop turns users into stakeholders overnight.</p><h4>The farming problem</h4><p>Once people realised early activity could pay out, a whole practice grew around it. Airdrop farming means using a protocol specifically to qualify for a token that may not even be announced yet. Serious farmers run dozens or hundreds of wallets to multiply their share, a tactic called a sybil attack.</p><p>Projects fight back by studying behaviour and cutting wallets that look automated or duplicated. The result is a quiet arms race. Farmers try to look human across many wallets, and teams try to tell genuine users from a spreadsheet of bots.</p><h4>How to think about it</h4><p>Chasing airdrops can pay, but treat it as speculation with your time rather than free money. Genuine use of protocols you would touch anyway gives the best odds, because it survives whatever anti-sybil filter the team runs later.</p><p>Two things people forget. In many countries an airdrop is taxable as income the moment it lands, valued on that day, whether or not you sell. And a surprising number of claim pages are scams that drain wallets through a malicious approval. If a claim site asks you to sign something you cannot read, the free money was the bait.</p>"},
    {id:"rugpull",title:"The Anatomy of a Rug Pull",author:"Dash Team",date:"May 22, 2026",dateShort:"May 22",read:"1 min read",
     excerpt:"Most rug pulls are not sophisticated. They rely on you skipping three checks before you buy.",body:"<p>A rug pull is when a team takes the money and vanishes, leaving holders with a worthless token. They are common because they are cheap to run and feed on the fear of missing out. The good news is that most follow a pattern you can learn to read.</p><h4>The liquidity rug</h4><p>When a token launches, someone pairs it with real money in a liquidity pool so people can trade it. If the team controls that liquidity, they can pull it out in a single transaction, draining the pool and leaving holders with tokens nobody can sell. The price hits zero in seconds.</p><p>The defence is locked or burned liquidity. Honest teams often lock it for months or years through a third party, and you can verify the lock on-chain instead of trusting a promise in the group chat.</p><h4>The code rug</h4><p>Some rugs live in the contract. A hidden mint function lets the team print unlimited tokens and dump them. A transfer restriction lets everyone buy but quietly blocks selling, a design people call a honeypot. Both are visible if the contract is verified and someone you trust can read it.</p><h4>The slow rug</h4><p>Not every exit is instant. Sometimes the team simply stops working, sells their allocation into every rally, and lets the project rot. A large team allocation with no vesting schedule is the warning sign.</p><p>Three checks catch most of them. Is the liquidity locked. Can you actually sell a small amount after buying. Who holds the supply, and are the team tokens vested. None of this guarantees safety, since a determined scammer can fake trust signals too. But skipping the checks is how most people get caught, and running them takes ten minutes with a block explorer and a token scanner.</p>"},
    {id:"washtrading",title:"Wash Trading and Why NFT Volume Lies",author:"Dash Team",date:"May 19, 2026",dateShort:"May 19",read:"1 min read",
     excerpt:"A collection can manufacture millions in fake volume with one wallet trading itself. Here is how to see through it.",body:"<p>Trading volume is supposed to signal demand. On NFT marketplaces, a large share of it is theatre. Understanding wash trading stops you from mistaking noise for interest.</p><h4>How it works</h4><p>Wash trading is buying and selling an asset to yourself to fake activity. One person controls two wallets, sends an NFT back and forth at rising prices, and the marketplace records every sale as real volume. To an outsider the collection looks hot. In reality nothing changed hands.</p><h4>Why anyone bothers</h4><p>Usually incentives. When a marketplace rewards traders with tokens based on the volume they generate, wash trading becomes a way to farm those rewards. Blur's token rewards drove enormous volumes in 2023, and a serious chunk was wallets trading with themselves to climb the leaderboard. Fake volume also pushes a collection onto trending lists, where real buyers might spot it.</p><h4>Spotting it</h4><p>The tells sit on-chain. The same NFT bouncing between a small cluster of wallets at oddly regular prices is the classic pattern. Volume that spikes on a marketplace with token rewards but stays flat everywhere else is another. Sales at prices disconnected from every other listing should make you suspicious.</p><p>Some data platforms now filter wash trades out and report an adjusted number, and the gap between raw and filtered volume can be enormous. When you size up a collection, look at holder count, how many distinct wallets are buying, and whether interest shows across venues rather than one. A single wallet can fake volume. It is far harder to fake a broad, growing base of separate holders who paid real money.</p>"},
    {id:"contracts",title:"What Is a Smart Contract, Really?",author:"Dash Team",date:"May 15, 2026",dateShort:"May 15",read:"2 min read",
     excerpt:"Code that runs exactly as written and cannot be stopped. That is the strength and the danger at once.",body:"<p>The phrase smart contract makes people picture legal paperwork. It is closer to a vending machine. Put the right input in and it releases the output automatically, with no clerk and no discretion.</p><h4>Code that runs itself</h4><p>A smart contract is a program stored on a blockchain. It holds funds and rules, and when someone meets the conditions it acts on its own. Send the right amount, receive the token. No bank approves it, no company can reverse it. Once deployed it does exactly what its code says, every time, for anyone who calls it.</p><p>This is what makes DeFi possible. A lending protocol, an exchange, an NFT mint, all of it is smart contracts holding money and following rules anyone can read before interacting.</p><h4>The catch is permanence</h4><p>A contract usually cannot be edited once it is live. That immutability is a feature, since no one can quietly change the rules on you. It is also a trap, because a bug is frozen in place with everything else.</p><p>The clearest example is The DAO in 2016. A flaw in its code let an attacker siphon out around 60 million dollars of ETH. The fallout was so large that Ethereum split into two chains over how to respond, a decision people still argue about. The money was not taken by breaking cryptography. It was taken by using the code exactly as written, in a way the authors never intended.</p><h4>What this means for you</h4><p>When you approve a contract, you are trusting its code, not a brand. Audits by reputable firms lower the risk, because more expert eyes have checked for flaws, but an audit is not a guarantee. Plenty of audited contracts have still been exploited. Treat a contract's age, its audit history, and how much value it has safely held as your evidence. Old and boring, in this world, is a compliment.</p>"},
    {id:"ordinals",title:"Bitcoin Ordinals and Inscriptions",author:"Dash Team",date:"May 12, 2026",dateShort:"May 12",read:"1 min read",
     excerpt:"NFTs arrived on Bitcoin, and the community is still arguing about whether they belong there.",body:"<p>For most of its life Bitcoin did one thing: move bitcoin. Then in early 2023 a developer named Casey Rodarmor launched Ordinals, and people started putting images, text, and even small games directly onto the Bitcoin blockchain. The reaction split the community.</p><h4>How it works</h4><p>Bitcoin's smallest unit is a satoshi, one hundred millionth of a bitcoin. Ordinal theory is a way of numbering every satoshi in the order it was mined, which makes each one individually trackable. Once you can point to a specific satoshi, you can attach data to it. That data is called an inscription, and the result behaves like an NFT, except the content lives fully on Bitcoin instead of linking out to a server.</p><h4>Why it is controversial</h4><p>This is where it gets heated. Bitcoin blockspace is scarce, and inscriptions compete with ordinary payments for room in each block. During busy stretches that pushes fees up for everyone. One camp sees this as spam clogging a payment network. The other sees paying for blockspace as exactly how Bitcoin is meant to work, and treats inscriptions as a fair new use that also pays miners.</p><p>The fee point cuts both ways. Higher fees annoy people sending payments, but they also help fund Bitcoin's security as the block reward shrinks over the years, which is a real long-term concern.</p><h4>Why it matters</h4><p>Ordinals proved that the oldest and most conservative chain in crypto could host something its creators never planned for. Whether you call that innovation or vandalism, it reopened the question of what Bitcoin is for. A collectible on Bitcoin also carries a certain weight, since it inherits the security of the most proven network in existence. The debate is not settled, and that is part of what makes it worth watching.</p>"},
    {id:"mev",title:"MEV: The Invisible Tax on Your Trades",author:"Dash Team",date:"May 8, 2026",dateShort:"May 8",read:"1 min read",
     excerpt:"Bots reorder the transactions in a block to profit off yours. It is automated, legal, and has probably cost you money already.",body:"<p>You place a trade, it goes through, and it fills at a slightly worse price than you expected. Some of that gap has a name: MEV, or maximal extractable value. It is one of the least understood costs in crypto, and it is everywhere.</p><h4>Where it comes from</h4><p>When you send a transaction it does not execute instantly. It waits in a public queue called the mempool, visible to anyone. Whoever builds the next block decides which transactions go in and in what order. That ordering power is worth money, and bots compete hard to capture it.</p><h4>The sandwich attack</h4><p>The clearest example targets swaps. A bot spots your large buy sitting in the mempool. It buys the same token just before you, pushing the price up. Your trade fills at that higher price. Then the bot sells right after, pocketing the difference. Your transaction is the filling in the sandwich, and you paid for the bot's profit through worse execution.</p><p>This is not a bug anyone will patch. It follows naturally from transactions being public before they settle, and from block builders being free to order them for profit.</p><h4>Protecting yourself</h4><p>Set a tight slippage tolerance so a swap reverts instead of filling at a price a bot manufactured. On large trades that one setting saves real money. Some wallets and tools now route transactions through private channels that skip the public mempool, hiding your trade from the bots watching it. Flashbots and similar services grew up around managing this whole market.</p><p>You will not escape MEV entirely, and small trades rarely attract the bots. But knowing why your fill drifted, and setting slippage on purpose instead of leaving the default, turns an invisible tax into something you can mostly sidestep.</p>"},
    {id:"tokenomics",title:"Tokenomics: Reading a Project's Supply Before You Buy",author:"Dash Team",date:"May 5, 2026",dateShort:"May 5",read:"1 min read",
     excerpt:"A token can have a great story and a supply schedule built to dump on you. The numbers are public. Read them.",body:"<p>Tokenomics is the supply and demand design of a token: how many exist, who holds them, and when more enter circulation. It is boring next to the narrative, and it decides more outcomes than the narrative ever will.</p><h4>Supply is two numbers, not one</h4><p>Circulating supply is what trades today. Total or fully diluted supply is what will exist once everything unlocks. The gap between them is where people get hurt. A token can look small by market cap while a mountain of locked tokens waits offstage. When those unlock they can flood the market and crush the price, even if nothing about the project changed.</p><h4>The unlock cliff</h4><p>Early investors and teams usually receive tokens on a vesting schedule, locked for a period and then released. A common structure is a one-year cliff followed by gradual monthly unlocks. The dates are typically public. If a large tranche unlocks next month, the people holding it at a low cost basis have every reason to sell, and you would be buying straight into that supply.</p><h4>What to actually check</h4><p>Look at how supply is allocated. A project that handed half its tokens to insiders is built differently from one with a wide community distribution. Look at the emissions rate, because a token printing high rewards to attract liquidity is diluting existing holders to do it. And look at the unlock calendar before you buy, not after.</p><p>Demand matters too, but demand is a story you have to believe. Supply is math you can verify. When the two conflict, supply usually wins, because selling pressure from unlocks is mechanical and does not care how good the project sounds. Read the schedule first. It is the least exciting research you will do and often the most useful.</p>"},
    {id:"staking",title:"Liquid Staking: Earning on ETH Without Locking It Away",author:"Dash Team",date:"May 1, 2026",dateShort:"May 1",read:"1 min read",
     excerpt:"Staking ETH earns yield but freezes your capital. Liquid staking hands you a token you can still use, and that token has its own risks.",body:"<p>Since the Merge, you can stake ETH to help secure Ethereum and earn a reward for it. The catch is that running a validator needs 32 ETH and locks the funds up. Liquid staking is the workaround most people actually use, and it comes with trade-offs worth understanding.</p><h4>The core idea</h4><p>Instead of staking directly, you deposit ETH with a liquid staking protocol like Lido or Rocket Pool. It stakes on your behalf and hands you a token that represents your staked position, such as stETH. That token earns the staking yield, and you can still trade it, lend it, or post it as collateral. Your capital works in two places at once, which is the whole appeal.</p><h4>Where the risk hides</h4><p>The staking token is a claim, and it can trade slightly away from the price of ETH. In June 2022 stETH drifted below ETH during a liquidity crunch, and leveraged players who assumed the two were interchangeable were forced to sell into a falling market. The gap closed in the end, but the wobble hurt people who ignored the possibility.</p><p>There is also smart contract risk, since your ETH sits in a protocol's code, and a concentration concern. When one provider stakes a very large slice of all ETH, it becomes a single point of influence over a network that is supposed to be decentralised.</p><h4>Is it worth it</h4><p>For most holders who want staking yield without running hardware, liquid staking is a reasonable option, and it powers a large part of DeFi. Just hold it knowing what the token is. It is not ETH. It is a receipt that usually behaves like ETH and occasionally reminds you that usually is not always.</p>"}
  ];
  var modal=document.getElementById('researchModal'); if(!modal) return;
  var grid=document.getElementById('rvGrid'), article=document.getElementById('rvArticle');
  var cards=document.getElementById('rvCards'), panel=document.getElementById('rvPanel');
  function cardHTML(p){return '<article class="post" role="button" tabindex="0" onclick="openPost(\''+p.id+'\')"><div class="thumb"><div class="glow"></div><div class="strip"></div></div><div class="pbody"><h3>'+p.title+'</h3><p class="pex">'+p.excerpt+'</p><div class="pmeta"><span>'+p.author+'</span><span class="sep">•</span><span>'+p.read+'</span><span class="sep">•</span><span>'+p.dateShort+'</span></div></div></article>';}
  function buildGrid(){cards.innerHTML=POSTS.map(cardHTML).join('');[].forEach.call(cards.children,function(el,i){el.style.animationDelay=(i*55)+'ms';});}
  function openModal(){modal.classList.add('open');modal.setAttribute('aria-hidden','false');document.body.style.overflow='hidden';}
  window.openResearch=function(){buildGrid();article.classList.remove('on');grid.classList.add('on');openModal();requestAnimationFrame(function(){modal.scrollTop=0;});};
  window.showGrid=function(){buildGrid();article.classList.remove('on');grid.classList.add('on');requestAnimationFrame(function(){modal.scrollTop=0;});};
  window.openPost=function(id){var p=null;for(var i=0;i<POSTS.length;i++){if(POSTS[i].id===id){p=POSTS[i];break;}}if(!p)return;
    var initial=p.author.replace(/[^a-zA-Z]/g,'').charAt(0).toUpperCase();
    var authorHTML=p.url?('<a href="'+p.url+'" target="_blank" rel="noopener" style="color:var(--sky)">'+p.author+'</a>'):p.author;
    panel.style.animation='none';
    void panel.offsetWidth;
    panel.innerHTML='<div class="rv-hero"><div class="g"></div><div class="s"></div></div><h1>'+p.title+'</h1><div class="rv-meta"><span>'+authorHTML+'</span><span class="sep">•</span><span>'+p.date+'</span><span class="sep">•</span><span>'+p.read+'</span></div><p class="rv-lede">'+p.excerpt+'</p><div class="rv-body">'+p.body+'</div><div class="rv-foot"><div class="av">'+initial+'</div><div><div style="color:#fff;font-weight:600">'+authorHTML+'</div><div style="font-size:12px;color:var(--muted)">Dash HQ Research</div></div></div>';
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
  if(window.matchMedia('(hover:hover)').matches){
    marquee.addEventListener('mouseenter',function(){paused=true;});
    marquee.addEventListener('mouseleave',function(){paused=false;});
  }
  function step(ts){
    if(!half)half=track.scrollWidth/2;
    if(!last)last=ts;
    var dt=Math.min(ts-last,50);last=ts;
    if(!paused){pos+=(half/48000)*dt;if(pos>=half)pos-=half;track.style.transform='translateX(-'+pos+'px)';}
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
        +'<h4>1. Who we are</h4>'
        +'<p>Dash HQ is a Web3 community and collective of NFT collectors, onchain analysts and crypto-native individuals. We share curated market commentary, onchain research and analysis through a private Discord server, our presence on X, and this website.</p>'
        +'<h4>2. Not financial advice</h4>'
        +'<p>Everything shared by Dash HQ — including alpha calls, mint analysis, research and any other content — is for informational and educational purposes only. It is <strong>not</strong> financial, investment, legal or tax advice. Crypto and NFTs are highly volatile and you can lose your entire investment. You are solely responsible for your own decisions and for doing your own research (DYOR).</p>'
        +'<h4>3. Membership &amp; conduct</h4>'
        +'<p>Citizenship is application-based and granted at our discretion. To remain a member you agree to treat others with respect and to not:</p>'
        +'<ul><li>Harass, threaten, scam or defraud other members;</li><li>Share paid Dash HQ content, calls or research outside the community;</li><li>Impersonate the team or other members;</li><li>Post spam, malware or malicious links.</li></ul>'
        +'<p>We may suspend or remove access at any time for conduct that harms the community, with or without notice.</p>'
        +'<h4>4. No guarantees</h4>'
        +'<p>We work hard to bring quality signal, but we make no guarantee about the accuracy, profitability or outcome of any information shared. Past performance is not indicative of future results. Services are provided on an "as is" and "as available" basis.</p>'
        +'<h4>5. Intellectual property</h4>'
        +'<p>The Dash HQ name, logo, written research and original content are our property and may not be copied or redistributed for commercial use without permission.</p>'
        +'<h4>6. Third-party platforms</h4>'
        +'<p>We rely on third parties such as Discord, X and Substack. Your use of those platforms is also governed by their own terms, and we are not responsible for their availability or actions.</p>'
        +'<h4>7. Limitation of liability</h4>'
        +'<p>To the fullest extent permitted by law, Dash HQ and its team will not be liable for any losses or damages arising from your use of our services or reliance on any content, including trading and investment losses.</p>'
        +'<h4>8. Changes</h4>'
        +'<p>We may update these Terms from time to time. Continued use of our services after changes means you accept the updated Terms.</p>'
        +'<h4>9. Contact</h4>'
        +'<p>Questions about these Terms? Reach us at <a href="mailto:'+CONTACT+'">'+CONTACT+'</a>.</p>'
    },
    privacy:{
      title:'Privacy Policy',
      html:''
        +'<p>This Privacy Policy explains what information Dash HQ collects, why, and how we handle it. We keep data collection to a minimum.</p>'
        +'<h4>1. Information we collect</h4>'
        +'<ul>'
        +'<li><strong>Newsletter email:</strong> when you subscribe to the Dash Dispatch, your email address is collected and stored by Substack to deliver our newsletter.</li>'
        +'<li><strong>Discord data:</strong> if you join our community, your Discord username and profile are visible to us and other members, as with any Discord server.</li>'
        +'<li><strong>Application details:</strong> information you choose to share when applying for citizenship.</li>'
        +'</ul>'
        +'<h4>2. What we do not collect</h4>'
        +'<p>This website does not run invasive tracking or sell data. We never ask for your private keys, seed phrase or wallet credentials, and we will never DM you asking for them.</p>'
        +'<h4>3. How we use your information</h4>'
        +'<ul><li>To send you the newsletter you signed up for;</li><li>To review membership applications and run the community;</li><li>To improve our content and communicate with members.</li></ul>'
        +'<h4>4. Third-party services</h4>'
        +'<p>We use Substack (newsletter), Discord (community) and X (social). When you interact with those, their own privacy policies apply. We recommend reviewing them.</p>'
        +'<h4>5. Your choices</h4>'
        +'<p>You can unsubscribe from the newsletter at any time using the link in any email, and you may leave the Discord community whenever you like. To request deletion of information you have shared with us directly, contact us.</p>'
        +'<h4>6. Security</h4>'
        +'<p>We take reasonable steps to protect the limited information we hold, but no method of transmission or storage is 100% secure.</p>'
        +'<h4>7. Changes</h4>'
        +'<p>We may update this policy occasionally. Material changes will be reflected by the "last updated" date above.</p>'
        +'<h4>8. Contact</h4>'
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
  // DEV: set your Substack publication handle here → dashhq.substack.com ⇒ "dashhq"
  var SUBSTACK_PUB = "dashhq1";
  const f=document.getElementById('subForm'); if(!f) return;
  const email=document.getElementById('subEmail'), msg=document.getElementById('subMsg');
  const row=f.querySelector('.sub-row');
  const btn=f.querySelector('button');
  const EMAIL_RE=/^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  function show(text,ok){ msg.textContent=text; msg.className='sub-msg show '+(ok?'ok':'err'); }
  function clearMsg(){ msg.className='sub-msg'; }
  function setRow(state){ row.classList.remove('valid','invalid','checking'); if(state) row.classList.add(state); }
  function reason(val){
    if(!val) return 'Please enter your email address.';
    if(val.indexOf('@')<0) return 'That email is missing an “@”.';
    const parts=val.split('@');
    if(parts.length>2) return 'An email can only contain one “@”.';
    if(!parts[0]) return 'Add the part before the “@”.';
    if(!parts[1]||parts[1].indexOf('.')<0) return 'Add the domain (e.g. name@gmail.com).';
    if(/\.$/.test(val)||/\.\./.test(val)) return 'That domain looks incomplete.';
    if(!EMAIL_RE.test(val)) return 'That email address looks incomplete.';
    return '';
  }
  // Confirm the email's domain actually has mail servers — DNS MX lookup, no backend.
  function domainOK(domain){
    return fetch('https://dns.google/resolve?name='+encodeURIComponent(domain)+'&type=MX')
      .then(function(r){return r.json();})
      .then(function(d){
        if(!d||typeof d.Status!=='number') return null; // unknown → don't block
        if(d.Status===3) return false;                  // NXDOMAIN → domain doesn't exist
        if(d.Status!==0) return null;                    // SERVFAIL etc → don't block
        return !!(Array.isArray(d.Answer)&&d.Answer.some(function(a){return a.type===15;})); // needs MX
      })
      .catch(function(){ return null; }); // offline / blocked → don't hard-block
  }
  let seq=0;
  // validate(val, cb) → cb(ok:boolean, message:string)
  function validate(val, cb){
    if(!EMAIL_RE.test(val)){ cb(false, reason(val)); return; }
    const domain=val.split('@')[1].toLowerCase(), mine=++seq;
    setRow('checking');
    domainOK(domain).then(function(ok){
      if(mine!==seq) return; // a newer check superseded this one
      if(ok===false) cb(false, 'We couldn’t find that email domain — check the spelling.');
      else cb(true, ''); // true, or null when offline → accept
    });
  }
  email.addEventListener('input',function(){ setRow(''); if(msg.classList.contains('err')) clearMsg(); });
  email.addEventListener('blur',function(){
    const val=email.value.trim(); if(!val){ setRow(''); return; }
    validate(val,function(ok,message){ if(ok){ setRow('valid'); clearMsg(); } else { setRow('invalid'); show(message,false); } });
  });
  function doSubscribe(val){
    btn.disabled=true; btn.textContent='Subscribing…';
    const endpoint='https://'+SUBSTACK_PUB+'.substack.com/api/v1/free';
    fetch(endpoint,{
      method:'POST', mode:'no-cors',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:val, first_url:location.href, first_referrer:document.referrer, current_url:location.href, current_referrer:document.referrer, referral_code:'', source:'embed'})
    }).then(function(){
      show('You’re in — welcome to the Dash Dispatch. Check your inbox to confirm. 🎉',true); f.reset(); setRow('');
    }).catch(function(){
      window.open('https://'+SUBSTACK_PUB+'.substack.com/subscribe?email='+encodeURIComponent(val),'_blank','noopener');
      show('Opening Substack to finish your subscription…',true);
    }).finally(function(){ btn.disabled=false; btn.textContent='Subscribe'; });
  }
  f.addEventListener('submit',function(e){
    e.preventDefault();
    const val=(email.value||'').trim();
    btn.disabled=true; btn.textContent='Checking…';
    validate(val,function(ok,message){
      btn.disabled=false; btn.textContent='Subscribe';
      if(!ok){ setRow('invalid'); show(message,false); email.focus(); return; }
      setRow('valid'); doSubscribe(val);
    });
  });
})();


(function(){var el=document.getElementById('cursorGhost');if(!el)return;var tx=innerWidth/2,ty=innerHeight/2,cx=tx,cy=ty,shown=false;function m(e){var p=e.touches?e.touches[0]:e;tx=p.clientX;ty=p.clientY;if(!shown){shown=true;el.style.opacity=1;}}window.addEventListener('pointermove',m,{passive:true});window.addEventListener('pointerdown',m,{passive:true});document.addEventListener('mouseleave',function(){el.style.opacity=0;shown=false;});(function loop(){cx+=(tx-cx)*0.14;cy+=(ty-cy)*0.14;el.style.transform='translate('+cx.toFixed(1)+'px,'+cy.toFixed(1)+'px) translate(-50%,-50%)';requestAnimationFrame(loop);})();})();
