// eLib interactions — Turbo-Drive aware (works with or without Turbo).
// Per-render bindings run on first load AND every Turbo visit (`turbo:load`).
// One-time document listeners are boot-guarded so nothing double-binds.
(function(){
  if (window.__elibBooted) { window.__elibInitPage(); return; }
  window.__elibBooted = true;

  const once = (el, key) => {
    if(!el || el.dataset['bound_' + key]) return false;
    el.dataset['bound_' + key] = '1';
    return true;
  };
  const escapeHtml = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  /* ---------------- toasts ---------------- */
  const TOAST_STYLE = {
    ok:    {box:'border-emerald-400/40 bg-emerald-950/90 text-emerald-100', icon:'fa-circle-check'},
    error: {box:'border-rose-400/40 bg-rose-950/90 text-rose-100', icon:'fa-circle-exclamation'},
    warn:  {box:'border-amber-400/40 bg-amber-950/90 text-amber-100', icon:'fa-triangle-exclamation'},
    info:  {box:'border-sky-400/40 bg-sky-950/90 text-sky-100', icon:'fa-circle-info'},
  };
  function armToast(el){
    if(!el || el.dataset.armed) return;
    el.dataset.armed = '1';
    const kill = () => { el.classList.add('hide'); setTimeout(()=>el.remove(), 380); };
    el.querySelector('[data-close]')?.addEventListener('click', kill);
    setTimeout(kill, 5200);
  }
  function showToast(message, category='info'){
    const box = document.getElementById('toasts');
    if(!box) return;
    const s = TOAST_STYLE[category] || TOAST_STYLE.info;
    const el = document.createElement('div');
    el.className = `toast glass-deep border ${s.box}`;
    const icon = document.createElement('span');
    icon.className = 'mt-0.5';
    icon.innerHTML = `<i class="fa-solid ${s.icon}"></i>`;
    const txt = document.createElement('span');
    txt.className = 'flex-1 leading-6';
    txt.textContent = message;
    const btn = document.createElement('button');
    btn.setAttribute('data-close', '');
    btn.setAttribute('aria-label', 'Dismiss');
    btn.className = 'opacity-60 hover:opacity-100';
    btn.innerHTML = '<i class="fa-solid fa-xmark"></i>';
    el.append(icon, txt, btn);
    box.appendChild(el);
    armToast(el);
  }
  window.showToast = showToast;

  /* ---------------- AJAX loan actions (request / return) — no refresh ---------------- */
  const PILL_CLASS = {available:'pill-ok', mine:'pill-info', pending:'pill-warn', borrowed:'pill-bad'};

  function paintPill(bookId, kind, label){
    if(!kind) return;
    document.querySelectorAll(`[data-status-pill="${bookId}"]`).forEach(p => {
      p.className = `pill ${PILL_CLASS[kind] || 'pill-neutral'}`;
      p.innerHTML = '';
      const dot = document.createElement('span');
      dot.className = 'dot';
      p.append(dot, document.createTextNode(label || kind));
    });
  }

  function paintRequestBtn(bookId, kind, label){
    document.querySelectorAll(`[data-request-btn="${bookId}"]`).forEach(b => {
      if(kind === 'available'){
        b.disabled = false;
        b.removeAttribute('title');
        b.classList.remove('btn-ghost','opacity-40','cursor-not-allowed');
        b.classList.add('btn-gold');
        b.innerHTML = '<i class="fa-solid fa-hand-holding-hand mr-1"></i>Request';
      } else if(kind){
        b.disabled = true;
        if(label) b.title = label;
        b.classList.remove('btn-gold');
        b.classList.add('btn-ghost','opacity-40','cursor-not-allowed');
        b.innerHTML = kind === 'pending'
          ? '<i class="fa-solid fa-hourglass-half mr-1"></i>Pending'
          : '<i class="fa-solid fa-hand-holding-hand mr-1"></i>Request';
      }
    });
  }

  function paintCounts(data){
    if(data.active == null) return;
    const maxLoans = parseInt(document.querySelector('[data-max-loans]')?.dataset.maxLoans || '10', 10);
    document.querySelectorAll('[data-stat-active]').forEach(el => el.textContent = Number(data.active).toLocaleString('en-US'));
    document.querySelectorAll('[data-stat-pending]').forEach(el => el.textContent = Number(data.pending ?? 0).toLocaleString('en-US'));
    document.querySelectorAll('[data-stat-slots]').forEach(el => el.textContent = (maxLoans - data.active).toLocaleString('en-US'));
    document.querySelectorAll('[data-capacity-text]').forEach(el => el.textContent = `${data.active} / ${maxLoans}`);
    document.querySelectorAll('[data-capacity-bar]').forEach(bar => {
      bar.style.width = Math.min(100, Math.round(data.active / maxLoans * 100)) + '%';
      bar.parentElement.classList.toggle('danger', data.active >= maxLoans - 2);
    });
    document.querySelectorAll('[data-capacity-wrap]').forEach(w => w.setAttribute('aria-valuenow', data.active));
    if(data.open != null) document.querySelectorAll('[data-active-count]').forEach(el => el.textContent = Number(data.open).toLocaleString('en-US'));
  }

  function historyRowHtml(row){
    return `<tr><td><b>${escapeHtml(row.title)}</b><br><span class="text-[11px] text-slate-500">${escapeHtml(row.author)}</span></td>`
      + `<td class="text-xs text-slate-300 whitespace-nowrap">${escapeHtml(row.borrowed)}</td>`
      + `<td class="text-xs text-slate-300 whitespace-nowrap">${escapeHtml(row.returned)}</td>`
      + `<td><span class="pill pill-ok"><span class="dot"></span>${escapeHtml(row.status)}</span></td></tr>`;
  }

  function removeLoanCard(bookId, row){
    const card = document.querySelector(`[data-loan-card="${bookId}"]`);
    const done = () => {
      card?.remove();
      if(row){
        const body = document.querySelector('[data-history-body]');
        if(body){
          body.insertAdjacentHTML('afterbegin', historyRowHtml(row));
        } else {
          const wrap = document.querySelector('[data-history-wrap]');
          const empty = wrap?.querySelector('[data-history-empty]');
          if(wrap && empty){
            empty.outerHTML = `<div class="overflow-x-auto rounded-2xl border border-white/10"><table class="tbl w-full min-w-[620px]">`
              + `<thead><tr><th>Book</th><th>Borrowed</th><th>Returned</th><th>Status</th></tr></thead>`
              + `<tbody data-history-body>${historyRowHtml(row)}</tbody></table></div>`;
          }
        }
        document.querySelectorAll('[data-history-count]').forEach(el => {
          el.textContent = (parseInt(el.textContent.replace(/\D/g,'') || '0', 10) + 1).toLocaleString('en-US');
        });
      }
      if(!document.querySelector('[data-loan-card]')){
        document.querySelectorAll('[data-shelf-grid]').forEach(g => {
          g.outerHTML = `<div class="text-center py-12"><div class="icon-badge mx-auto !w-16 !h-16 !rounded-2xl bg-white/5 border border-white/10 text-2xl text-slate-500"><i class="fa-solid fa-feather"></i></div><div class="font-bold mt-4 text-lg">Your shelf is empty</div><p class="text-xs text-slate-400 mt-1">Borrow something great from the dashboard.</p><a href="/dashboard" class="btn-gold inline-block mt-4 px-6 py-2.5 rounded-xl text-xs">Go to dashboard <i class="fa-solid fa-arrow-right ml-1"></i></a></div>`;
        });
      }
    };
    if(card){
      card.style.transition = 'opacity .35s, transform .35s';
      card.style.opacity = '0';
      card.style.transform = 'translateY(8px) scale(.98)';
      setTimeout(done, 360);
    } else done();
  }

  async function loanSubmit(form){
    const action = form.dataset.loanAction; // 'request' | 'return'
    const bookId = form.dataset.bookId;
    const btn = form.querySelector('button');
    const origHtml = btn?.innerHTML;
    if(btn){ btn.disabled = true; btn.setAttribute('aria-busy', 'true'); btn.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin mr-1"></i>Working...'; }
    const restoreBtn = () => { if(btn){ btn.disabled = false; btn.removeAttribute('aria-busy'); if(origHtml != null) btn.innerHTML = origHtml; } };
    try{
      const res = await fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        headers: {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
      });
      if(res.redirected && /\/login/.test(res.url)){ window.location.href = res.url; return; }
      const ct = res.headers.get('content-type') || '';
      let data = null;
      if(ct.includes('application/json')) data = await res.json();
      if(!data){ window.location.reload(); return; } // non-JS fallback
      showToast(data.message, data.ok ? 'ok' : 'warn');
      if(!data.ok){ restoreBtn(); return; }
      if(action === 'request'){
        paintPill(bookId, data.status_kind, data.status_label);
        paintRequestBtn(bookId, data.status_kind, data.status_label);
        paintCounts(data);
      } else {
        // restore the return button itself first: on My Books the whole card
        // is removed below, but on Dashboard the button stays and must not
        // keep spinning after a successful return
        if(btn){ btn.disabled = false; btn.removeAttribute('aria-busy'); if(origHtml != null) btn.innerHTML = origHtml; }
        paintPill(bookId, 'available', 'Available');
        paintRequestBtn(bookId, 'available');
        paintCounts(data);
        removeLoanCard(bookId, data.history_row || null);
      }
    }catch(err){
      showToast('Connection error. Please try again.', 'error');
      restoreBtn();
    }
  }

  /* ---------------- assistant (fresh DOM lookups: Turbo-safe) ---------------- */
  function chatScroll(chat){ if(chat) chat.scrollTop = chat.scrollHeight; }
  function bubbleBot(chat, text){
    const w = document.createElement('div'); w.className='flex gap-2.5 justify-start';
    w.innerHTML = `<span class="icon-badge !rounded-xl shrink-0 text-base text-white" style="background:linear-gradient(135deg,#8b5cf6,#4c1d95)"><i class="fa-solid fa-robot"></i></span>`
      + `<div class="chat-bot px-4 py-3 max-w-[85%] leading-7"></div>`;
    w.lastElementChild.textContent = text;
    chat.appendChild(w); chatScroll(chat); return w.lastElementChild;
  }
  function bubbleMe(chat, text){
    const w = document.createElement('div'); w.className='flex gap-2.5 justify-end';
    const b = document.createElement('div'); b.className='chat-me px-4 py-3 max-w-[85%] leading-7 font-medium';
    b.textContent = text; w.appendChild(b); chat.appendChild(w); chatScroll(chat);
  }
  function bubbleChips(chat, suggestions){
    if(!suggestions || !suggestions.length) return;
    const w = document.createElement('div');
    w.className = 'flex gap-1.5 justify-start flex-wrap pl-11';
    suggestions.slice(0, 4).forEach(s => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'chat-chip';
      b.textContent = s;
      b.setAttribute('aria-label', 'Ask: ' + s);
      b.addEventListener('click', () => ask(s));
      w.appendChild(b);
    });
    chat.appendChild(w); chatScroll(chat);
  }
  async function ask(preset){
    const chat = document.getElementById('chat');
    const input = document.getElementById('q');
    if(!chat || !input) return;
    const q = (preset !== undefined ? preset : input.value).trim();
    if(!q) return;
    bubbleMe(chat, q); input.value='';
    const target = bubbleBot(chat, '');
    target.innerHTML = '<span class="typing flex gap-1 py-1"><span></span><span></span><span></span></span>';
    chatScroll(chat);
    try{
      const r = await fetch('/api/assistant',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({q})});
      const j = await r.json();
      target.textContent = j.answer;
      bubbleChips(chat, j.suggestions);
    }catch(e){ target.textContent = 'Connection error. Please try again.'; }
    chatScroll(chat);
  }
  window.ask = ask;

  /* ---------------- one-time delegated listeners ---------------- */
  document.addEventListener('submit', (e) => {
    const loanForm = e.target.closest?.('form[data-loan-action]');
    if(loanForm){ e.preventDefault(); loanSubmit(loanForm); return; }
    if(e.target.closest?.('#ask-form')){ e.preventDefault(); ask(); }
  });
  const updateNav = () => {
    const nav = document.getElementById('topnav');
    if(nav) nav.classList.toggle('docked', window.scrollY > 24);
  };
  window.addEventListener('scroll', updateNav, {passive:true});

  /* ---------------- per-render bindings ---------------- */
  function initPage(){
    updateNav();

    const btn = document.getElementById('menu-btn');
    const panel = document.getElementById('mobile-menu');
    if(btn && panel && once(btn, 'menu')) btn.addEventListener('click', () => {
      const open = panel.classList.toggle('hidden');
      btn.setAttribute('aria-expanded', String(!open));
      btn.innerHTML = open ? '<i class="fa-solid fa-bars"></i>' : '<i class="fa-solid fa-xmark"></i>';
    });

    document.querySelectorAll('#toasts .toast').forEach(armToast);

    const io = new IntersectionObserver(es => es.forEach(en => {
      if(en.isIntersecting){ en.target.classList.add('in'); io.unobserve(en.target); }
    }), {threshold:.08});
    document.querySelectorAll('.reveal').forEach(el => { if(once(el, 'rev')) io.observe(el); });

    const cio = new IntersectionObserver(es => es.forEach(en => {
      if(!en.isIntersecting) return; cio.unobserve(en.target);
      const el = en.target, target = parseInt(el.dataset.count||'0',10);
      const t0 = performance.now(), dur = 1200;
      const step = t => {
        const p = Math.min(1,(t-t0)/dur), ease = 1-Math.pow(1-p,3);
        el.textContent = Math.round(target*ease).toLocaleString('en-US');
        if(p<1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    }), {threshold:.4});
    document.querySelectorAll('[data-count]').forEach(el => { if(once(el, 'count')) cio.observe(el); });

    document.querySelectorAll('[data-live-search]').forEach(input => {
      if(!once(input, 'live')) return;
      const sel = input.getAttribute('data-live-search');
      input.addEventListener('input', () => {
        const q = input.value.trim().toLowerCase();
        document.querySelectorAll(sel).forEach(card => {
          const hay = (card.dataset.search||'').toLowerCase();
          card.style.display = (!q || hay.includes(q)) ? '' : 'none';
        });
        const visible = [...document.querySelectorAll(sel)].filter(c=>c.style.display!=='none').length;
        document.querySelectorAll('[data-live-count]').forEach(el=>{ el.textContent = visible.toLocaleString('en-US'); });
      });
    });

    document.querySelectorAll('[data-pw-toggle]').forEach(b => {
      if(!once(b, 'pw')) return;
      b.addEventListener('click', () => {
        const inp = document.querySelector(b.getAttribute('data-pw-toggle'));
        if(!inp) return;
        const show = inp.type === 'password';
        inp.type = show ? 'text' : 'password';
        const icon = b.querySelector('i');
        if(icon){ icon.classList.toggle('fa-eye', !show); icon.classList.toggle('fa-eye-slash', show); }
        b.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
      });
    });

    const tilt = document.getElementById('tilt');
    if(tilt && once(tilt, 'tilt') && matchMedia('(pointer:fine)').matches){
      tilt.addEventListener('mousemove', e => {
        const r = tilt.getBoundingClientRect();
        const x = (e.clientX-r.left)/r.width-.5, y=(e.clientY-r.top)/r.height-.5;
        tilt.style.transform = `perspective(900px) rotateY(${x*10}deg) rotateX(${-y*10}deg)`;
      });
      tilt.addEventListener('mouseleave', ()=>{ tilt.style.transform='perspective(900px)'; });
    }

    const path = location.pathname;
    document.querySelectorAll('.navlink').forEach(a => {
      if(a.getAttribute('href')===path) a.classList.add('active');
    });

    if(path === '/assistant') document.getElementById('q')?.focus({preventScroll:true});
  }
  window.__elibInitPage = initPage;
  initPage();
  document.addEventListener('turbo:load', initPage);
})();
