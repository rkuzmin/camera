"use strict";

// --------------------------------------------------------------------------
// small helpers
// --------------------------------------------------------------------------
const app = document.getElementById("app");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const TAG_RU = {
  person: "человек", car: "машина", truck: "грузовик", bus: "автобус",
  motorcycle: "мотоцикл", bicycle: "велосипед", dog: "собака", cat: "кошка",
};
const tagRu = (t) => TAG_RU[t] || t;
const FILTER_TAGS = ["person", "car", "dog", "cat", "truck"];

// object class -> timeline block colour (grey fallback = manual / untagged)
const TAG_COLOR = {
  person: "#58a6ff", car: "#e3883e", truck: "#d97706", bus: "#d97706",
  motorcycle: "#e3883e", bicycle: "#e3883e", dog: "#3fb950", cat: "#3fb950", bird: "#2ea043",
};
const clipColor = (c) => { for (const t of (c.tags || [])) if (TAG_COLOR[t]) return TAG_COLOR[t]; return "#7d8590"; };

// ---- date / time helpers (server timestamps are already local) ----
const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const pad2 = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
const todayStr = () => ymd(new Date());
const yesterdayStr = () => { const d = new Date(); d.setDate(d.getDate() - 1); return ymd(d); };
const secOfDay = (iso) => { const t = (iso.split("T")[1] || "0:0:0").split(":"); return (+t[0]) * 3600 + (+t[1]) * 60 + (+t[2] || 0); };
const hms = (iso) => (iso.split("T")[1] || "").slice(0, 8);
const secNow = () => { const d = new Date(); return d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds(); };
function fmtDur(sec) {
  sec = Math.round(sec || 0);
  if (sec <= 0) return "";
  return sec < 60 ? sec + " с" : Math.floor(sec / 60) + ":" + pad2(sec % 60);
}
function plural(n, one, few, many) {
  const a = n % 10, b = n % 100;
  if (a === 1 && b !== 11) return one;
  if (a >= 2 && a <= 4 && (b < 10 || b >= 20)) return few;
  return many;
}
function dayTitle(ds) {
  if (ds === todayStr()) return "Сегодня";
  if (ds === yesterdayStr()) return "Вчера";
  return new Date(ds + "T00:00:00").toLocaleDateString("ru-RU", { day: "numeric", month: "long", weekday: "long" });
}

const state = {
  view: "rec",
  token: localStorage.getItem("token") || "",
  cameras: [],
  cam: "",
  tag: "",
  day: "",            // selected calendar day 'YYYY-MM-DD'
  calMonth: null,     // Date at day 1 of the visible month
  days: {},           // 'YYYY-MM-DD' -> clip count (calendar dots)
  dayClips: [],       // clips of the selected day
  lastSync: null,     // Date of the last manual sync
};

// clips carry only cam_id; resolve the display name from the loaded camera list
const camName = (id) => (state.cameras.find((c) => c.id === id) || {}).name || id;

function toast(msg) {
  let t = document.querySelector(".toast");
  if (!t) { t = document.createElement("div"); t.className = "toast"; document.body.appendChild(t); }
  t.textContent = msg;
  requestAnimationFrame(() => t.classList.add("show"));
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 2200);
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(path, { method: opts.method || (opts.json !== undefined ? "POST" : "GET"), headers, body: opts.body });
  if (res.status === 401) { state.token = ""; localStorage.removeItem("token"); showLogin(); throw new Error("unauthorized"); }
  if (!res.ok) throw new Error("HTTP " + res.status);
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res.text();
}

// --------------------------------------------------------------------------
// login
// --------------------------------------------------------------------------
function showLogin() {
  app.innerHTML = `
    <form class="login" id="loginform">
      <div class="logo">📷</div>
      <h1>Камеры</h1>
      <p>Войдите, чтобы смотреть записи и камеры</p>
      <div class="err" id="err"></div>
      <input type="text" name="username" autocomplete="username" value="admin" hidden>
      <input class="field" id="pw" type="password" autocomplete="current-password"
             inputmode="text" placeholder="Пароль" enterkeyhint="go">
      <button class="btn" id="go" type="submit">Войти</button>
    </form>`;
  const form = document.getElementById("loginform");
  const pw = document.getElementById("pw");
  const go = document.getElementById("go");
  const err = document.getElementById("err");
  const submit = async () => {
    err.textContent = ""; go.disabled = true; go.textContent = "…";
    try {
      const r = await api("/api/login", { json: { password: pw.value } });
      state.token = r.token; localStorage.setItem("token", r.token);
      showApp();
    } catch (e) {
      err.textContent = "Неверный пароль";
      go.disabled = false; go.textContent = "Войти";
    }
  };
  form.onsubmit = (e) => { e.preventDefault(); submit(); };
  pw.focus();
}

// --------------------------------------------------------------------------
// app shell + nav
// --------------------------------------------------------------------------
const TABS = [
  { id: "rec", label: "Записи", ico: "🎞️" },
  { id: "cam", label: "Камеры", ico: "📹" },
  { id: "set", label: "Настройки", ico: "⚙️" },
];

function showApp() {
  app.innerHTML = `
    <div class="topbar">
      <h1 id="ttl">Записи</h1>
      <div style="flex:1"></div>
      <button id="refresh" class="modal-x-like" style="background:none;border:0;color:var(--muted);font-size:22px">⟳</button>
    </div>
    <div class="content" id="content"></div>
    <nav class="bottomnav" id="nav"></nav>`;
  document.getElementById("refresh").onclick = () => renderView(true);
  const nav = document.getElementById("nav");
  nav.innerHTML = TABS.map((t) =>
    `<button data-tab="${t.id}"><span class="ico">${t.ico}</span>${t.label}</button>`).join("");
  nav.querySelectorAll("button").forEach((b) => {
    b.onclick = () => { state.view = b.dataset.tab; renderView(); };
  });
  // deep link from a push notification: /?cam=<id>
  const q = new URLSearchParams(location.search);
  if (q.get("cam")) state.cam = q.get("cam");
  renderView();
}

function setActiveNav() {
  document.querySelectorAll("#nav button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === state.view));
  document.getElementById("ttl").textContent =
    TABS.find((t) => t.id === state.view)?.label || "";
  document.getElementById("refresh").style.display = state.view === "rec" ? "" : "none";
}

function renderView(refresh) {
  setActiveNav();
  if (state.view === "rec") renderRecordings(refresh);
  else if (state.view === "cam") renderLive();
  else if (state.view === "set") renderSettings();
}

// --------------------------------------------------------------------------
// recordings
// --------------------------------------------------------------------------
async function renderRecordings(refresh) {
  const content = document.getElementById("content");
  if (!state.cameras.length || refresh) {
    try { state.cameras = await api("/api/cameras"); } catch (_) { return; }
  }
  content.innerHTML = `
    <div class="chips" id="camchips"></div>
    <div class="chips" id="tagchips"></div>
    <div class="calendar" id="calendar"></div>
    <div class="timeline" id="timeline"></div>
    <div id="daylist"><div class="spinner"></div></div>`;
  renderChips();
  await loadDays();
  await loadDay();
}

function renderChips() {
  const cc = document.getElementById("camchips");
  cc.innerHTML =
    `<button class="chip ${state.cam ? "" : "active"}" data-cam="">Все камеры</button>` +
    state.cameras.map((c) =>
      `<button class="chip ${state.cam === c.id ? "active" : ""}" data-cam="${esc(c.id)}">${esc(c.name || c.id)}</button>`).join("");
  cc.querySelectorAll("button").forEach((b) => b.onclick = () => {
    state.cam = b.dataset.cam; renderChips(); onFilterChange();
  });
  const tc = document.getElementById("tagchips");
  tc.innerHTML =
    `<button class="chip ${state.tag ? "" : "active"}" data-tag="">Любой объект</button>` +
    FILTER_TAGS.map((t) =>
      `<button class="chip ${state.tag === t ? "active" : ""}" data-tag="${t}">${tagRu(t)}</button>`).join("");
  tc.querySelectorAll("button").forEach((b) => b.onclick = () => {
    state.tag = b.dataset.tag; renderChips(); onFilterChange();
  });
}

async function onFilterChange() {
  await loadDays();
  await loadDay();
}

// which days have clips (calendar dots) + choose the day to show
async function loadDays() {
  const p = new URLSearchParams();
  if (state.cam) p.set("cam", state.cam);
  if (state.tag) p.set("tag", state.tag);
  let days = [];
  try { days = await api("/api/recording-days?" + p.toString()); } catch (_) {}
  state.days = {};
  for (const d of days) state.days[d.day] = d.count;
  const sorted = Object.keys(state.days).sort();
  if (!state.day || !state.days[state.day]) {
    state.day = sorted.length ? sorted[sorted.length - 1] : todayStr();
  }
  state.calMonth = new Date(state.day + "T00:00:00");
  state.calMonth.setDate(1);
  renderCalendar();
}

function renderCalendar() {
  const el = document.getElementById("calendar");
  if (!el) return;
  const m = state.calMonth, year = m.getFullYear(), month = m.getMonth();
  const startWd = (new Date(year, month, 1).getDay() + 6) % 7;   // Monday-first
  const nDays = new Date(year, month + 1, 0).getDate();
  const mName = m.toLocaleDateString("ru-RU", { month: "long" });
  const title = mName.charAt(0).toUpperCase() + mName.slice(1) + " " + year;
  let cells = "";
  for (let i = 0; i < startWd; i++) cells += `<span class="cal-cell"></span>`;
  for (let d = 1; d <= nDays; d++) {
    const ds = `${year}-${pad2(month + 1)}-${pad2(d)}`;
    const has = state.days[ds];
    const cls = ["cal-cell"];
    if (has) cls.push("has");
    if (ds === state.day) cls.push("sel");
    if (ds === todayStr()) cls.push("today");
    cells += `<button class="${cls.join(" ")}" ${has ? `data-day="${ds}"` : "disabled"}>${d}${has ? '<i class="cal-dot"></i>' : ""}</button>`;
  }
  el.innerHTML = `
    <div class="cal-head">
      <button class="cal-nav" data-nav="-1" aria-label="Предыдущий месяц">‹</button>
      <span class="cal-title">${esc(title)}</span>
      <button class="cal-nav" data-nav="1" aria-label="Следующий месяц">›</button>
    </div>
    <div class="cal-wd">${WEEKDAYS.map((w) => `<span>${w}</span>`).join("")}</div>
    <div class="cal-grid">${cells}</div>`;
  el.querySelector('[data-nav="-1"]').onclick = () => shiftMonth(-1);
  el.querySelector('[data-nav="1"]').onclick = () => shiftMonth(1);
  el.querySelectorAll(".cal-cell.has").forEach((b) => b.onclick = () => {
    state.day = b.dataset.day; renderCalendar(); loadDay();
  });
}

function shiftMonth(delta) {
  state.calMonth = new Date(state.calMonth.getFullYear(), state.calMonth.getMonth() + delta, 1);
  renderCalendar();
}

// load the selected day's clips, then draw the timeline + list
async function loadDay() {
  const list = document.getElementById("daylist");
  if (list) list.innerHTML = `<div class="spinner"></div>`;
  const p = new URLSearchParams({ day: state.day, limit: "2000" });
  if (state.cam) p.set("cam", state.cam);
  if (state.tag) p.set("tag", state.tag);
  try {
    state.dayClips = await api("/api/recordings?" + p.toString());
  } catch (_) {
    state.dayClips = [];
    if (list) list.innerHTML = `<div class="empty">Ошибка загрузки</div>`;
    return;
  }
  renderTimeline();
  renderDayList();
}

// 24-hour DVR-style track: each clip is a block placed by start time, width = duration
function renderTimeline() {
  const el = document.getElementById("timeline");
  if (!el) return;
  const clips = state.dayClips;
  const ticks = [0, 3, 6, 9, 12, 15, 18, 21, 24].map((h) =>
    `<span class="tl-tick" style="left:${h / 24 * 100}%">${h}</span>`).join("");
  const blocks = clips.map((c) => {
    const left = secOfDay(c.started_iso) / 864;              // seconds -> % of day
    const w = Math.max(0.3, (c.duration || 0) / 864);
    return `<button class="tl-block" data-name="${esc(c.name)}" data-cam="${esc(c.cam_id)}"
             title="${esc(hms(c.started_iso))} · ${esc((c.tags || []).map(tagRu).join(", ") || "запись")}"
             style="left:${left}%;width:${w}%;background:${clipColor(c)}"></button>`;
  }).join("");
  const now = state.day === todayStr()
    ? `<span class="tl-now" style="left:${secNow() / 864}%"></span>` : "";
  el.innerHTML = `
    <div class="tl-title">${esc(dayTitle(state.day))} · ${clips.length} ${plural(clips.length, "запись", "записи", "записей")}</div>
    <div class="tl-track">${blocks}${now}</div>
    <div class="tl-axis">${ticks}</div>`;
  el.querySelectorAll(".tl-block").forEach((b) => b.onclick = () => openClipByRef(b.dataset.cam, b.dataset.name));
}

function renderDayList() {
  const list = document.getElementById("daylist");
  if (!list) return;
  const clips = state.dayClips.slice().sort((a, b) => b.started_at - a.started_at);
  if (!clips.length) {
    list.innerHTML = `<div class="empty"><div class="big">🎬</div>В этот день записей нет</div>`;
    return;
  }
  list.innerHTML = clips.map(clipRowDay).join("");
  list.querySelectorAll("[data-name]").forEach((el) =>
    el.onclick = () => openClipByRef(el.dataset.cam, el.dataset.name));
}

function openClipByRef(camId, name) {
  const c = state.dayClips.find((x) => x.cam_id === camId && x.name === name);
  if (c) openPlayer(c);
}

function clipRowDay(c) {
  const tags = (c.tags || []).map((t) => `<span class="tag">${esc(tagRu(t))}</span>`).join("");
  const dur = fmtDur(c.duration);
  return `
    <div class="clip" data-name="${esc(c.name)}" data-cam="${esc(c.cam_id)}">
      <div class="thumb" style="color:${clipColor(c)}">▶</div>
      <div class="meta">
        <div class="time">${hms(c.started_iso)}${dur ? ` · ${dur}` : ""}</div>
        <div class="cam">${esc(camName(c.cam_id))}</div>
        ${tags ? `<div class="tags">${tags}</div>` : ""}
      </div>
      <div class="right">${c.size_mb} МБ<br>▶</div>
    </div>`;
}

// --------------------------------------------------------------------------
// video player modal
// --------------------------------------------------------------------------
function openPlayer(c) {
  const m = document.createElement("div");
  m.className = "modal";
  m.innerHTML = `
    <div class="bar">
      <div class="title"><div class="t">${esc(camName(c.cam_id))}</div>
        <div class="s">${esc(c.started_iso.replace("T", " "))}</div></div>
      <button class="x" aria-label="Закрыть">✕</button>
    </div>
    <video controls autoplay playsinline preload="auto" src="${esc(c.url)}"></video>
    <div class="foot">
      <a class="btn secondary" style="display:block;text-align:center;text-decoration:none"
         href="${esc(c.url)}" download="${esc(c.name)}">Скачать</a>
    </div>`;
  document.body.appendChild(m);
  document.body.style.overflow = "hidden";
  const close = () => { m.remove(); document.body.style.overflow = ""; };
  m.querySelector(".x").onclick = close;
  m.querySelector("video").onerror = () => toast("Не удалось загрузить видео");
}

// --------------------------------------------------------------------------
// live view (HLS stream, with the snapshot relay as instant poster + fallback)
// --------------------------------------------------------------------------
async function renderLive() {
  const content = document.getElementById("content");
  if (!state.cameras.length) {
    try { state.cameras = await api("/api/cameras"); } catch (_) { return; }
  }
  const cams = state.cameras;
  if (!cams.length) {
    content.innerHTML = `<div class="empty"><div class="big">📹</div>Камер пока нет</div>`;
    return;
  }
  content.innerHTML = cams.map((c) => `
    <div class="camcard" data-id="${esc(c.id)}">
      <div class="camicon">📹</div>
      <div class="caminfo">
        <div class="camname">${esc(c.name || c.id)}</div>
        <div class="camsub">${c.clip_count || 0} записей</div>
      </div>
      <div class="camplay">LIVE ▶</div>
    </div>`).join("");
  content.querySelectorAll("[data-id]").forEach((el) => el.onclick = () => {
    const c = cams.find((x) => x.id === el.dataset.id);
    if (c) openLive(c);
  });
}

// hls.js is only needed where the browser can't play HLS natively (i.e. not
// Safari/iOS). Loaded lazily and cached, so the snapshot-only path never pays for it.
let _hlsPromise = null;
function loadHls() {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (_hlsPromise) return _hlsPromise;
  _hlsPromise = new Promise((resolve) => {
    const s = document.createElement("script");
    s.src = "/hls.min.js";
    s.onload = () => resolve(window.Hls || null);
    s.onerror = () => { _hlsPromise = null; resolve(null); };
    document.head.appendChild(s);
  });
  return _hlsPromise;
}

function openLive(cam) {
  const m = document.createElement("div");
  m.className = "modal";
  m.innerHTML = `
    <div class="bar">
      <div class="title"><div class="t">${esc(cam.name || cam.id)}</div>
        <div class="s"><span class="livedot"></span>В эфире</div></div>
      <button class="x" aria-label="Закрыть">✕</button>
    </div>
    <div class="livewrap">
      <video class="livevideo" playsinline muted autoplay></video>
      <img class="liveimg" alt="">
      <div class="livemsg" id="livemsg"><div class="spinner"></div>Подключение к камере…</div>
    </div>
    <div class="foot"></div>`;
  document.body.appendChild(m);
  document.body.style.overflow = "hidden";
  const video = m.querySelector(".livevideo");
  const img = m.querySelector(".liveimg");
  const msg = m.querySelector("#livemsg");
  let misses = 0, got = false, closed = false, streaming = false, hls = null;

  // Keep telling the backend we're watching — the edge runs the HLS ffmpeg (and
  // the snapshot encoder) only for cameras that are currently "wanted".
  const want = () => api("/api/live/want", { json: { cam_id: cam.id } }).catch(() => {});

  // Snapshot relay: gives an instant first image while HLS warms up, and is the
  // fallback when HLS never comes good (camera offline, or H.265 on a non-Apple
  // browser). Suspended once the video is actually playing.
  const frame = async () => {
    if (closed || streaming) return;
    try {
      const res = await fetch("/api/live/frame/" + encodeURIComponent(cam.id),
        { headers: { Authorization: "Bearer " + state.token } });
      if (res.status === 200) {
        const url = URL.createObjectURL(await res.blob());
        const old = img.src;
        img.src = url; img.style.display = "block"; msg.style.display = "none";
        got = true; misses = 0;
        if (old && old.startsWith("blob:")) URL.revokeObjectURL(old);
      } else if (!got && ++misses > 18) {
        msg.innerHTML = "Нет сигнала.<br><span style='font-size:13px'>Проверьте, что домашнее приложение подключено к серверу и камера онлайн.</span>";
      }
    } catch (_) { misses++; }
  };

  // Once the stream is actually rolling, switch to it and drop snapshot polling.
  video.addEventListener("playing", () => {
    if (closed) return;
    streaming = true;
    video.style.display = "block";
    img.style.display = "none";
    msg.style.display = "none";
    if (img.src && img.src.startsWith("blob:")) { URL.revokeObjectURL(img.src); img.src = ""; }
  });

  // HLS gave up — tear it down and lean on the snapshot relay (still polling).
  const fallbackToSnapshot = () => {
    streaming = false;
    video.style.display = "none";
    if (hls) { try { hls.destroy(); } catch (_) {} hls = null; }
  };

  const startHls = async () => {
    let info;
    try { info = await api("/api/live/hls/start", { json: { cam_id: cam.id } }); }
    catch (_) { return; }   // no stream URL — the snapshot fallback stays on
    if (closed || !info || !info.url) return;
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = info.url;                        // native HLS (Safari / iOS)
      video.onerror = fallbackToSnapshot;
      video.play().catch(() => {});
    } else {
      const Hls = await loadHls();
      if (closed || streaming) return;
      if (!Hls || !Hls.isSupported()) return;      // can't stream here — stay on snapshots
      hls = new Hls({ lowLatencyMode: true, liveSyncDurationCount: 2, maxBufferLength: 6, backBufferLength: 0 });
      hls.on(Hls.Events.ERROR, (_e, data) => { if (data && data.fatal) fallbackToSnapshot(); });
      hls.loadSource(info.url);
      hls.attachMedia(video);
    }
  };

  want();
  const wantTimer = setInterval(want, 4000);
  const frameTimer = setInterval(frame, 300);
  frame();          // kick an immediate snapshot so something shows up fast
  startHls();
  const close = () => {
    closed = true;
    clearInterval(wantTimer); clearInterval(frameTimer);
    if (hls) { try { hls.destroy(); } catch (_) {} hls = null; }
    try { video.pause(); video.removeAttribute("src"); video.load(); } catch (_) {}
    if (img.src && img.src.startsWith("blob:")) URL.revokeObjectURL(img.src);
    m.remove(); document.body.style.overflow = "";
  };
  m.querySelector(".x").onclick = close;
}

// --------------------------------------------------------------------------
// settings
// --------------------------------------------------------------------------
async function renderSettings() {
  const content = document.getElementById("content");
  const pushOK = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const standalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone;
  content.innerHTML = `
    <div class="card">
      <h2>Уведомления</h2>
      <p>Пуш на телефон, когда камера что-то обнаружила.${!standalone ? " Для iOS сначала добавьте приложение на домашний экран." : ""}</p>
      <div id="pushbox">${pushOK
        ? `<button class="btn" id="pushbtn">Включить уведомления</button>`
        : `<div class="row"><span class="k">Не поддерживается этим браузером</span></div>`}</div>
    </div>
    <div class="card">
      <h2>Сервер</h2>
      <div class="row"><span class="k">Адрес</span><span class="v">${esc(location.host)}</span></div>
      <div class="row"><span class="k">Камер</span><span class="v">${state.cameras.length}</span></div>
      <div class="row"><span class="k">Последняя синхронизация</span><span class="v">${state.lastSync ? state.lastSync.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }) : "—"}</span></div>
      <button class="btn secondary" id="syncbtn" style="margin-top:8px">Синхронизировать с сервером</button>
    </div>
    <div class="card">
      <h2>Камеры</h2>
      <p>Активные камеры приходят с домашнего приложения. Старые камеры можно удалить из списка — вместе с их записями или без.</p>
      <div id="cammanage"><div class="spinner"></div></div>
    </div>
    <div class="card">
      <h2>Приложение</h2>
      <p>Добавьте на домашний экран, чтобы открывать как обычное приложение и получать уведомления.</p>
      <button class="btn danger" id="logout">Выйти</button>
    </div>`;
  document.getElementById("logout").onclick = () => {
    state.token = ""; localStorage.removeItem("token");
    try { navigator.serviceWorker?.ready.then((r) => r.pushManager.getSubscription().then((s) => s && s.unsubscribe())); } catch (_) {}
    showLogin();
  };
  document.getElementById("syncbtn").onclick = (e) => syncNow(e.currentTarget);
  renderCamManage();
  const pb = document.getElementById("pushbtn");
  if (pb) {
    const reg = await navigator.serviceWorker?.ready.catch(() => null);
    const sub = reg && await reg.pushManager.getSubscription();
    if (sub) { pb.textContent = "Уведомления включены ✓"; pb.classList.add("secondary"); pb.disabled = true; }
    pb.onclick = enablePush;
  }
}

// camera management — list every camera (incl. stale ones) and let the user
// delete cameras that no longer exist on the edge, optionally with their clips
async function renderCamManage() {
  const box = document.getElementById("cammanage");
  if (!box) return;
  let cams = [];
  try { cams = await api("/api/cameras?all=1"); }
  catch (_) { box.innerHTML = `<div class="row"><span class="k">Не удалось загрузить</span></div>`; return; }
  if (!cams.length) { box.innerHTML = `<div class="row"><span class="k">Камер нет</span></div>`; return; }
  box.innerHTML = cams.map((c) => {
    const active = c.active !== 0;
    const n = c.clip_count || 0;
    const sub = `${n} ${plural(n, "запись", "записи", "записей")}${active ? "" : " · неактивна"}`;
    return `
      <div class="camrow">
        <div class="camrow-info">
          <div class="camrow-name">${esc(c.name || c.id)}</div>
          <div class="camrow-sub">${sub}</div>
        </div>
        ${active
          ? `<span class="camrow-badge on">активна</span>`
          : `<button class="camrow-del" data-id="${esc(c.id)}" data-name="${esc(c.name || c.id)}" data-clips="${n}">Удалить</button>`}
      </div>`;
  }).join("");
  box.querySelectorAll(".camrow-del").forEach((b) => b.onclick = () =>
    deleteCamera(b.dataset.id, b.dataset.name, +b.dataset.clips));
}

async function deleteCamera(id, name, clipCount) {
  if (!confirm(`Удалить камеру «${name}» из списка?`)) return;
  let withClips = false;
  if (clipCount > 0) {
    withClips = confirm(
      `Удалить также ${clipCount} ${plural(clipCount, "запись", "записи", "записей")} этой камеры?\n\n` +
      `ОК — удалить записи с сервера. Отмена — оставить записи.`);
  }
  try {
    await api(`/api/cameras/${encodeURIComponent(id)}?with_clips=${withClips ? 1 : 0}`, { method: "DELETE" });
    toast(withClips ? "Камера и записи удалены" : "Камера удалена");
    try { state.cameras = await api("/api/cameras"); } catch (_) {}
    renderCamManage();
  } catch (_) {
    toast("Не удалось удалить камеру");
  }
}

function urlB64ToUint8(base64) {
  const pad = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + pad).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function enablePush() {
  try {
    const { public_key } = await api("/api/push/vapid_public");
    if (!public_key) { toast("Пуши ещё не настроены на сервере"); return; }
    const perm = await Notification.requestPermission();
    if (perm !== "granted") { toast("Разрешение не выдано"); return; }
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlB64ToUint8(public_key),
    });
    await api("/api/push/subscribe", { json: sub.toJSON() });
    toast("Уведомления включены");
    renderSettings();
  } catch (e) {
    toast("Не удалось включить уведомления");
  }
}

// --------------------------------------------------------------------------
// sync — re-pull everything from the server on demand
// --------------------------------------------------------------------------
async function syncNow(btn) {
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Синхронизация…"; }
  try {
    state.cameras = await api("/api/cameras");
    const days = await api("/api/recording-days");
    const total = days.reduce((s, d) => s + d.count, 0);
    state.lastSync = new Date();
    toast(`Синхронизировано: ${total} ${plural(total, "запись", "записи", "записей")}`);
    if (state.view === "rec") renderView(true);
    else if (state.view === "set") renderSettings();
  } catch (_) {
    toast("Не удалось синхронизировать");
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

// --------------------------------------------------------------------------
// boot
// --------------------------------------------------------------------------
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
if (state.token) showApp(); else showLogin();
