// TGAds: collector of the TGStat channel catalog of one country.
// Run on https://uz.tgstat.com/channels/search while logged in to TGStat (paste into the console or
// inject). It walks the catalog search language by language (category comes with every card), keeps
// the rows in IndexedDB of tgstat (survives reloads) and posts them to the panel with a plain form:
// TGStat's CSP forbids fetch() to other sites but allows form submissions.
//
//   TGAdsCollector.start()        collect (resumes where it stopped)
//   TGAdsCollector.status()       progress
//   TGAdsCollector.stop()
//   TGAdsCollector.send(0)        post rows [0, CHUNK) to the panel; the page navigates to the panel
//   TGAdsCollector.reset()        forget collected rows and progress
(() => {
  const INGEST_URL = '__INGEST_URL__';
  const COUNTRY = 4;          // Uzbekistan
  const CHUNK = 15000;        // rows per form post
  const DELAY = [2000, 4000]; // pause between requests, ms
  const LANGS = [13, 3, 12, 68, 51, 46, 16, 88, 9, 40, 2, 42, 54, 52, 55, 56, 57, 90, 85, 19, 62, 91, 50, 77, 21,
    48, 41, 36, 74, 69, 25, 64, 7, 45, 18, 8, 80, 72, 66, 75, 15, 43, 34, 44, 67, 84, 81, 49, 31, 71, 32, 82, 83, 5,
    11, 14, 6, 17, 29, 30, 78, 39, 26, 28, 76, 35, 24, 65, 70, 79, 4, 27, 73, 33, 23, 10, 38, 87, 47, 37, 86, 20,
    89, 53];

  // Timers of a hidden tab are throttled to about one per minute after 5 minutes; a worker's are not.
  let timer = null;
  try {
    timer = new Worker(URL.createObjectURL(new Blob(
      ['onmessage = e => setTimeout(() => postMessage(e.data.id), e.data.ms)'], {type: 'text/javascript'})));
  } catch (e) { /* no workers: plain timers */ }
  const waiting = new Map();
  let seq = 0;
  if (timer) timer.onmessage = e => { const done = waiting.get(e.data); waiting.delete(e.data); if (done) done(); };
  const sleep = ms => new Promise(r => {
    if (!timer) return setTimeout(r, ms);
    const id = ++seq;
    waiting.set(id, r);
    timer.postMessage({id, ms});
  });
  const pause = () => sleep(DELAY[0] + Math.random() * (DELAY[1] - DELAY[0]));

  let dbp = null;
  const db = () => dbp || (dbp = new Promise((ok, fail) => {
    const rq = indexedDB.open('tgads-catalog', 1);
    rq.onupgradeneeded = () => { rq.result.createObjectStore('rows'); rq.result.createObjectStore('state'); };
    rq.onsuccess = () => ok(rq.result);
    rq.onerror = () => fail(rq.error);
  }));
  const tx = async (store, mode, fn) => {
    const d = await db();
    return new Promise((ok, fail) => {
      const t = d.transaction(store, mode);
      const res = fn(t.objectStore(store));
      t.oncomplete = () => ok(res && 'result' in res ? res.result : res);
      t.onerror = () => fail(t.error);
    });
  };
  const getState = async () => (await tx('state', 'readonly', s => s.get('s'))) ||
    {lang: 0, page: 1, pages: 0, requests: 0, error: '', finished: false, startedAt: new Date().toISOString()};
  const putState = st => tx('state', 'readwrite', s => s.put(st, 's'));
  const putRows = rows => tx('rows', 'readwrite', s => rows.forEach(r => s.put(r, r[0])));
  const countRows = () => tx('rows', 'readonly', s => s.count());
  const allRows = () => tx('rows', 'readonly', s => s.getAll());

  const form = () => document.querySelector('form[action="/channels/search"]');
  const categoryIds = () => {
    const map = {};
    const sel = form() && form().querySelector('select[name="categories[]"]');
    if (sel) for (const o of sel.options) map[o.textContent.trim()] = Number(o.value);
    return map;
  };

  async function query(lang, page) {
    const fd = new FormData(form());
    for (const k of ['countries[]', 'languages[]', 'categories[]']) fd.delete(k);
    fd.append('countries[]', String(COUNTRY));
    fd.append('languages[]', String(lang));
    fd.set('page', String(page));
    fd.set('offset', '0');
    for (let attempt = 1; ; attempt++) {
      try {
        const r = await fetch('/channels/search', {method: 'POST', body: fd, headers: {'X-Requested-With': 'XMLHttpRequest'}});
        const j = await r.json();
        if (r.status === 200 && j.status === 'ok') return j;
        throw new Error(`HTTP ${r.status}, status=${j && j.status}`);
      } catch (e) {
        if (attempt >= 4) throw e;
        await sleep(30000 * attempt);  // TGStat is busy or rate-limits: back off
      }
    }
  }

  function parseCards(html, lang, cats) {
    const box = document.createElement('div');
    box.innerHTML = html;
    const rows = [];
    for (const card of box.querySelectorAll('.peer-item-row')) {
      const a = card.querySelector('a[href*="/channel/"]');
      if (!a) continue;
      const m = a.getAttribute('href').match(/\/channel\/([^/?#]+)/);
      if (!m) continue;
      const ref = m[1];
      const title = (card.querySelector('.font-16') || {}).textContent || '';
      const nums = [...card.querySelectorAll('h4')].map(h => h.textContent.trim());
      const catLabel = ((card.querySelector('span.border') || {}).textContent || '').trim();
      const img = card.querySelector('img');
      rows.push([ref, title.trim(), nums[0] || null, nums[1] || null, nums[2] || null,
                 cats[catLabel] || null, lang, img ? img.getAttribute('src') : '']);
    }
    return rows;
  }

  let running = false;
  async function start() {
    if (running) return 'уже идёт';
    if (!form()) return 'откройте страницу /channels/search';
    running = true;
    const cats = categoryIds();
    const st = await getState();
    st.error = '';
    try {
      while (running && st.lang < LANGS.length) {
        const lang = LANGS[st.lang];
        const j = await query(lang, st.page);
        st.requests++;
        const rows = parseCards(j.html || '', lang, cats);
        if (rows.length) await putRows(rows);
        st.pages++;
        if (j.hasMore && rows.length) {
          st.page++;
        } else {
          st.lang++;
          st.page = 1;
        }
        await putState(st);
        await pause();
      }
      if (st.lang >= LANGS.length) st.finished = true;
    } catch (e) {
      st.error = String(e);
    } finally {
      running = false;
      await putState(st);
    }
    return status();
  }

  async function status() {
    const st = await getState();
    return {running, rows: await countRows(), language: LANGS[st.lang] || null, languageIndex: st.lang,
            of: LANGS.length, page: st.page, requests: st.requests, finished: st.finished, error: st.error};
  }

  async function send(from = 0) {
    const rows = (await allRows()).slice(from, from + CHUNK);
    if (!rows.length) return 'нечего отправлять';
    const f = document.createElement('form');
    f.method = 'POST';
    f.action = INGEST_URL;
    f.enctype = 'text/plain';
    f.acceptCharset = 'utf-8';
    const t = document.createElement('textarea');
    t.name = 'd';
    t.value = JSON.stringify({country: 'uz', note: `rows ${from}-${from + rows.length}`, items: rows});
    f.appendChild(t);
    document.body.appendChild(f);
    f.submit();
    return `отправляю ${rows.length} строк`;
  }

  async function reset() {
    running = false;
    await tx('rows', 'readwrite', s => s.clear());
    await tx('state', 'readwrite', s => s.clear());
    return 'сброшено';
  }

  window.TGAdsCollector = {start, status, stop: () => { running = false; return 'остановится после текущего запроса'; },
                           send, reset};
})();
