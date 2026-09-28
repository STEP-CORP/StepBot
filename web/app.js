"use strict";
/* Standalone web cabinet — buy a VPN subscription from a browser, no Telegram.
   Talks to /api/cabinet/auth/* (email/OAuth/guest) and /api/cabinet/* (me/plans/purchase). */

const $ = (sel) => document.querySelector(sel);
const app = () => $("#app");
const A = "/api/cabinet/auth";
const C = "/api/cabinet";

const store = {
  get access() { return localStorage.getItem("wc_access") || ""; },
  get refresh() { return localStorage.getItem("wc_refresh") || ""; },
  set(access, refresh) {
    if (access) localStorage.setItem("wc_access", access);
    if (refresh) localStorage.setItem("wc_refresh", refresh);
  },
  clear() { localStorage.removeItem("wc_access"); localStorage.removeItem("wc_refresh"); },
};

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
}

function money(minor) {
  const v = (minor || 0) / 100;
  return (Number.isInteger(v) ? v : v.toFixed(2)).toLocaleString("ru-RU") + " ₽";
}

async function api(method, path, body, auth, _retried) {
  const headers = { "Content-Type": "application/json" };
  if (auth && store.access) headers.Authorization = "Bearer " + store.access;
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  if (res.status === 401 && auth && store.refresh && !_retried) {
    // Refresh once. Without the _retried guard a still-401 response after a "successful"
    // refresh would loop forever (valid refresh token, but the request stays unauthorized).
    const ok = await tryRefresh();
    if (ok) return api(method, path, body, auth, true);
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.detail || `Ошибка ${res.status}`);
    err.status = res.status;  // callers distinguish auth failures from transient 5xx
    throw err;
  }
  return data;
}

async function tryRefresh() {
  try {
    const r = await fetch(`${A}/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: store.refresh }),
    });
    if (!r.ok) return false;
    const d = await r.json();
    store.set(d.access_token, d.refresh_token);
    return true;
  } catch { return false; }
}

function el(tag, attrs = {}, children = []) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v != null) n.setAttribute(k, v);
  }
  for (const c of [].concat(children)) if (c != null) n.append(c.nodeType ? c : document.createTextNode(c));
  return n;
}

// Owner brand title from /api/cabinet/config (falls back to «STEP BOT» until loaded / if unset).
let brandTitle = "";
function brand() {
  if (brandTitle) return el("div", { class: "brand" }, [el("span", {}, brandTitle)]);
  return el("div", { class: "brand" }, [el("span", {}, "STEP"), el("span", { class: "b" }, "BOT")]);
}
// Social logins the owner actually configured; until /config answers we show none (an
// unconfigured button used to 400 with "provider not configured" — a dead button).
let oauthProviders = [];
async function loadBrand() {
  try {
    const c = await api("GET", `${C}/config`);
    if (c && c.title) {
      brandTitle = String(c.title);
      document.title = brandTitle;
    }
    if (c && Array.isArray(c.oauth_providers)) {
      oauthProviders = c.oauth_providers;
      render();
    }
  } catch {}
}

/* ---------- auth screens ---------- */

let authTab = "login";

async function oauthGo(provider, link) {
  try {
    const d = await api("GET", `${A}/oauth/${provider}/authorize${link ? "?link=1" : ""}`, null, !!link);
    if (d.authorize_url) {
      // Remember which provider (and mode) we launched — the callback URL may not carry
      // ?provider=, and defaulting to "google" would exchange the code against the wrong endpoint.
      try {
        localStorage.setItem("wc_oauth_provider", provider);
        if (link) localStorage.setItem("wc_oauth_link", "1");
        else localStorage.removeItem("wc_oauth_link");
      } catch (e) {}
      window.location.href = d.authorize_url;
    }
  } catch (e) { toast(e.message); }
}

const OAUTH_LABELS = { google: "Google", yandex: "Яндекс", vk: "ВКонтакте" };

let tgPollTimer = null;

async function tgLoginGo(holder) {
  clearInterval(tgPollTimer);
  try {
    const d = await api("POST", `${A}/telegram/start`);
    holder.innerHTML = "";
    holder.append(
      el("div", { class: "hint" }, "Открой бота и нажми «Да, это я» — вход подтвердится сам."),
      el("a", { class: "btn primary", href: d.url, target: "_blank", rel: "noopener" }, "Открыть бота"),
      el("div", { class: "hint center" }, "Ждём подтверждение…"),
    );
    const deadline = Date.now() + (d.expires_in || 300) * 1000;
    tgPollTimer = setInterval(async () => {
      if (Date.now() > deadline) { clearInterval(tgPollTimer); holder.innerHTML = ""; toast("Время вышло — попробуй ещё раз"); return; }
      try {
        const r = await api("POST", `${A}/telegram/poll`, { code: d.code });
        if (r.access_token) {
          clearInterval(tgPollTimer);
          store.set(r.access_token, r.refresh_token);
          route("cabinet");
        }
      } catch (e) {
        // Only a real "code expired" (400) is terminal. A network blip or a 429 from the
        // shared-IP rate limiter must NOT kill the poll — the user may have already tapped
        // «Да, это я» in the bot, and giving up would waste that confirmation. Keep polling
        // until the deadline; those transient ticks just retry on the next interval.
        if (e && e.status === 400) {
          clearInterval(tgPollTimer);
          holder.innerHTML = "";
          toast(e.message);
        }
      }
    }, 2500);
  } catch (e) { toast(e.message); }
}

function oauthButtons() {
  const tgHolder = el("div", {});
  const labels = { vk: "Войти через ВКонтакте", yandex: "Войти через Яндекс", google: "Войти через Google" };
  const social = oauthProviders
    .filter((p) => labels[p])
    .map((p) => el("button", { class: "btn oauth", onclick: () => oauthGo(p) }, labels[p]));
  return el("div", {}, [
    el("div", { class: "divider" }, "или"),
    el("button", { class: "btn oauth", onclick: () => tgLoginGo(tgHolder) }, "Войти через Telegram"),
    tgHolder,
    ...social,
  ]);
}

function authView() {
  const root = el("div", {}, [brand()]);

  const tabs = el("div", { class: "tabs" }, [
    el("div", { class: `tab${authTab === "login" ? " on" : ""}`, onclick: () => { authTab = "login"; render(); } }, "Вход"),
    el("div", { class: `tab${authTab === "register" ? " on" : ""}`, onclick: () => { authTab = "register"; render(); } }, "Регистрация"),
    el("div", { class: `tab${authTab === "guest" ? " on" : ""}`, onclick: () => { authTab = "guest"; render(); } }, "Купить сразу"),
  ]);
  root.append(tabs);

  if (authTab === "guest") { root.append(guestCard()); return root; }

  const email = el("input", { type: "email", placeholder: "you@example.com", autocomplete: "email" });
  const pass = el("input", { type: "password", placeholder: "Пароль", autocomplete: authTab === "login" ? "current-password" : "new-password" });
  const card = el("div", { class: "card" }, [
    el("label", {}, "E-mail"), email,
    el("label", {}, "Пароль"), pass,
  ]);

  if (authTab === "login") {
    card.append(el("button", {
      class: "btn primary",
      onclick: async () => {
        try {
          const d = await api("POST", `${A}/login`, { email: email.value.trim(), password: pass.value });
          store.set(d.access_token, d.refresh_token);
          route("cabinet");
        } catch (e) { toast(e.message); }
      },
    }, "Войти"));
  } else {
    card.append(el("button", {
      class: "btn primary",
      onclick: async () => {
        if (pass.value.length < 8) return toast("Пароль от 8 символов");
        try {
          const d = await api("POST", `${A}/register`, { email: email.value.trim(), password: pass.value });
          if (d.requires_verification) {
            toast("Проверьте почту — отправили ссылку подтверждения");
          } else if (d.access_token) {
            store.set(d.access_token, d.refresh_token);
            route("cabinet");
          }
        } catch (e) { toast(e.message); }
      },
    }, "Создать аккаунт"));
  }
  card.append(oauthButtons());
  root.append(card);
  return root;
}

function guestCard() {
  const email = el("input", { type: "email", placeholder: "you@example.com" });
  const card = el("div", { class: "card" }, [
    el("h2", {}, "Купить без регистрации"),
    el("div", { class: "hint" }, "Укажи e-mail — подписка придёт письмом, аккаунт создастся сам."),
    el("label", {}, "E-mail"), email,
  ]);
  const holder = el("div", {});
  card.append(holder);
  loadPlans(false).then((plans) => {
    if (!plans.length) { holder.append(el("div", { class: "hint" }, "Тарифы ещё не настроены.")); return; }
    holder.append(planPicker(plans, async (planId, days, method) => {
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.value.trim())) return toast("Введите корректный e-mail");
      try {
        const d = await api("POST", `${A}/guest/purchase`, {
          email: email.value.trim(), plan_id: planId, days, method,
        });
        if (d.auto_login_token) localStorage.setItem("wc_auto", d.auto_login_token);
        if (d.redirect_url) window.location.href = d.redirect_url;
      } catch (e) { toast(e.message); }
    }, true));
  });
  return card;
}

/* ---------- plan picker (shared by guest + cabinet) ---------- */

async function loadPlans(auth) {
  const path = auth ? `${C}/plans` : `${C}/public/plans`;
  try {
    const d = await api("GET", path, null, auth);
    if (!auth && d.payment_methods) window.__pm__ = d.payment_methods;  // guests need methods too
    return d.items || [];
  } catch { return []; }
}

function planPicker(plans, onPay, guest) {
  const state = { plan: 0, dur: 0, method: "" };
  const wrap = el("div", {});
  function draw() {
    wrap.innerHTML = "";
    const plan = plans[state.plan];
    wrap.append(el("div", { class: "cap" }, "Тариф"));
    plans.forEach((p, i) => {
      wrap.append(el("div", {
        class: `plan${i === state.plan ? " on" : ""}`,
        onclick: () => { state.plan = i; state.dur = 0; draw(); },
      }, [el("span", { class: "name" }, p.name), el("span", { class: "muted" }, ((p.durations || [])[0] ? money(p.durations[0].price_minor) : ""))]));
    });
    const durs = el("div", { class: "durs" });
    (plan.durations || []).forEach((d, i) => {
      durs.append(el("div", {
        class: `dur${i === state.dur ? " on" : ""}`,
        onclick: () => { state.dur = i; draw(); },
      }, [el("div", { class: "m" }, `${d.months} мес`), el("div", { class: "p" }, money(d.price_minor))]));
    });
    wrap.append(durs);
    // payment method (guest & web users use online gateways)
    const methods = (window.__pm__ || []);
    wrap.append(el("div", { class: "cap", style: "margin-top:8px" }, "Оплата"));
    const chips = el("div", { class: "durs" });
    methods.forEach((m) => {
      chips.append(el("div", {
        class: `dur${state.method === m.id ? " on" : ""}`,
        onclick: () => { state.method = m.id; draw(); },
      }, m.label));
    });
    wrap.append(methods.length ? chips : el("div", { class: "hint" }, "Онлайн-оплата не настроена."));
    const sel = (plan.durations || [])[state.dur];
    wrap.append(el("button", {
      class: "btn primary",
      disabled: !state.method || !sel ? "" : null,
      onclick: () => sel && state.method && onPay(plan.id, sel.days, state.method),
    }, sel ? `Оплатить · ${money(sel.price_minor)}` : "Выбери срок"));
  }
  draw();
  return wrap;
}

/* ---------- cabinet ---------- */

async function cabinetView() {
  const root = el("div", {}, [brand()]);
  let me;
  try { me = await api("GET", `${C}/me`, null, true); }
  catch (e) {
    // Only a real auth failure should destroy the session. A transient 5xx (e.g. backend
    // restart) must NOT wipe valid tokens and force a re-login — show a retry instead.
    if (e && (e.status === 401 || e.status === 403)) { store.clear(); route("auth"); return el("div", {}); }
    root.append(el("div", { class: "card" }, [
      el("div", { class: "hint" }, "Не удалось загрузить кабинет. Проверь соединение."),
      el("button", { class: "btn", onclick: () => route("cabinet") }, "Обновить"),
    ]));
    return root;
  }
  window.__pm__ = (me.app && me.app.payment_methods) || [];

  const sub = me.subscription;
  const usable = sub && ["active", "trial", "limited"].includes(sub.status);
  root.append(el("div", { class: "card" }, [
    el("div", { class: "li" }, [el("span", { class: "muted" }, me.user.username || me.user.first_name || "Аккаунт"),
      el("span", { class: `pill ${usable ? "ok" : "warn"}` }, usable ? "Подписка активна" : "Нет подписки")]),
    el("div", { class: "li" }, [el("span", { class: "muted" }, "Баланс"), el("b", {}, money(me.user.balance_minor))]),
    sub && sub.expire_at ? el("div", { class: "li" }, [el("span", { class: "muted" }, "Действует до"), el("b", {}, new Date(sub.expire_at).toLocaleDateString("ru-RU"))]) : null,
  ]));

  if (usable && sub.subscription_url) {
    root.append(el("div", { class: "card" }, [
      el("h2", {}, "Подключение"),
      el("div", { class: "hint" }, "Вставь ссылку в Happ / v2RayTun / Hiddify:"),
      el("div", { class: "sublink" }, sub.subscription_url),
      el("button", { class: "btn ghost", onclick: () => { navigator.clipboard.writeText(sub.subscription_url); toast("Ссылка скопирована"); } }, "Скопировать ссылку"),
    ]));
  }

  const plans = await loadPlans(true);
  if (plans.length) {
    root.append(el("div", { class: "card" }, [
      el("h2", {}, usable ? "Продлить / сменить тариф" : "Купить подписку"),
      planPicker(plans, async (planId, days, method) => {
        try {
          const d = await api("POST", `${C}/purchase`, { plan_id: planId, days, method }, true);
          if (d.redirect_url) window.location.href = d.redirect_url;
          else if (d.paid_with) { toast("Оплачено!"); route("cabinet"); }
        } catch (e) { toast(e.message); }
      }),
    ]));
  }

  root.append(await linkedCard());

  root.append(el("div", { class: "center" }, [
    el("span", { class: "link", onclick: async () => { try { await api("POST", `${A}/logout`, { refresh_token: store.refresh }); } catch {} store.clear(); route("auth"); } }, "Выйти"),
  ]));
  return root;
}

/* ---------- account & sign-in methods (linking) ---------- */

async function linkedCard() {
  let d;
  try { d = await api("GET", `${C}/linked`, null, true); }
  catch { return el("div", {}); }

  const card = el("div", { class: "card" }, [el("h2", {}, "Аккаунт и входы")]);

  // e-mail
  card.append(el("div", { class: "li" }, [
    el("span", { class: "muted" }, "Почта"),
    d.email
      ? el("b", {}, d.email + (d.email_verified ? "" : " (не подтверждена)"))
      : el("span", { class: "link", onclick: () => linkEmailForm(card) }, "Привязать"),
  ]));

  // telegram
  card.append(el("div", { class: "li" }, [
    el("span", { class: "muted" }, "Telegram"),
    d.telegram
      ? el("b", {}, d.telegram.username ? "@" + d.telegram.username : "привязан")
      : el("span", { class: "link", onclick: async () => {
          try {
            const r = await api("POST", `${C}/link/telegram`, null, true);
            card.append(el("div", { class: "hint" }, "Открой бота — аккаунты свяжутся автоматически. Ссылка одноразовая, живёт 15 минут."));
            card.append(el("a", { class: "btn primary", href: r.url, target: "_blank" }, "Открыть бота и привязать"));
          } catch (e) { toast(e.message); }
        } }, "Привязать"),
  ]));

  // oauth providers
  const linkedSet = new Set((d.oauth || []).map((o) => o.provider));
  (d.oauth || []).forEach((o) => {
    card.append(el("div", { class: "li" }, [
      el("span", { class: "muted" }, OAUTH_LABELS[o.provider] || o.provider),
      el("span", {}, [
        el("b", {}, o.display_name || o.email || "привязан"),
        el("span", { class: "link", style: "margin-left:10px", onclick: async () => {
          try { await api("DELETE", `${C}/link/oauth/${o.provider}`, null, true); toast("Отвязано"); route("cabinet"); }
          catch (e) { toast(e.message); }
        } }, "отвязать"),
      ]),
    ]));
  });
  (d.available_providers || []).filter((p) => !linkedSet.has(p)).forEach((p) => {
    card.append(el("div", { class: "li" }, [
      el("span", { class: "muted" }, OAUTH_LABELS[p] || p),
      el("span", { class: "link", onclick: () => oauthGo(p, true) }, "Привязать"),
    ]));
  });

  return card;
}

function linkEmailForm(card) {
  const email = el("input", { type: "email", placeholder: "you@example.com", autocomplete: "email" });
  const pass = el("input", { type: "password", placeholder: "Пароль для входа на сайте", autocomplete: "new-password" });
  const box = el("div", {}, [
    el("label", {}, "E-mail"), email,
    el("label", {}, "Пароль"), pass,
  ]);
  const send = el("button", { class: "btn primary", onclick: async () => {
    if (pass.value.length < 8) return toast("Пароль от 8 символов");
    try {
      await api("POST", `${C}/link/email`, { email: email.value.trim(), password: pass.value }, true);
      box.innerHTML = "";
      const code = el("input", { type: "text", inputmode: "numeric", placeholder: "Код из письма" });
      box.append(el("div", { class: "hint" }, `Отправили код на ${email.value.trim()}`), code,
        el("button", { class: "btn primary", onclick: async () => {
          try {
            await api("POST", `${C}/link/email/confirm`, { code: code.value.trim() }, true);
            toast("Почта привязана"); route("cabinet");
          } catch (e) { toast(e.message); }
        } }, "Подтвердить"));
    } catch (e) { toast(e.message); }
  } }, "Получить код");
  box.append(send);
  card.append(box);
}

/* ---------- router ---------- */

let view = "auth";
let renderSeq = 0;
function route(v) { view = v; render(); }

async function render() {
  // A render is async (cabinetView awaits /me). If it fails auth mid-flight it calls
  // route("auth"), which starts a NESTED render; without this guard the outer render would
  // then overwrite the freshly drawn auth screen with cabinetView's empty fallback node —
  // a blank white page. The seq check makes a superseded render bail instead of clobbering.
  const seq = ++renderSeq;
  const a = app();
  a.innerHTML = "";
  a.append(el("div", { class: "muted center" }, "…"));
  let node;
  if (view === "cabinet") node = await cabinetView();
  else node = authView();
  if (seq !== renderSeq) return;  // a newer render superseded us
  a.innerHTML = "";
  a.append(node);
}

async function boot() {
  const params = new URLSearchParams(location.search);
  // The provider can bounce back with an error instead of a code (user pressed «Отмена»,
  // redirect URI mismatch…). Say so — silently showing the login form again reads as
  // «авторизация не проходит».
  if (params.get("error") && !params.get("code")) {
    const desc = params.get("error_description") || params.get("error");
    toast(`Вход не удался: ${desc}`);
    history.replaceState({}, "", location.pathname);
  }
  // OAuth callback: /web?code=...&state=... (VK ID also appends &device_id=...)
  if (params.get("code") && params.get("state")) {
    const provider = params.get("provider") || localStorage.getItem("wc_oauth_provider") || "google";
    const linking = localStorage.getItem("wc_oauth_link") === "1";
    try {
      const d = await api("POST", `${A}/oauth/callback`, {
        provider,
        code: params.get("code"),
        state: params.get("state"),
        device_id: params.get("device_id") || null,
      });
      if (d.access_token) store.set(d.access_token, d.refresh_token);
      if (linking && d.ok) toast(`${OAUTH_LABELS[d.linked] || d.linked} привязан`);
    } catch (e) { toast(e.message); }
    localStorage.removeItem("wc_oauth_link");
    history.replaceState({}, "", location.pathname);
  }
  // guest success returns with an auto-login token stashed before redirect
  const auto = localStorage.getItem("wc_auto");
  if (auto && !store.access) {
    try {
      const d = await api("POST", `${A}/login/auto`, { token: auto });
      store.set(d.access_token, d.refresh_token);
      localStorage.removeItem("wc_auto");  // spent — remove only on success
    } catch (e) {
      // A guest has no password: wc_auto is their ONLY way into the freshly bought account.
      // Burn it only when the server says it's truly invalid (401); on a transient 5xx /
      // network error keep it so a reload can retry, instead of stranding a paying user.
      if (e && e.status === 401) localStorage.removeItem("wc_auto");
    }
  }
  await loadBrand();  // owner title before first paint (falls back to STEP BOT)
  view = store.access ? "cabinet" : "auth";
  render();
}

boot();
