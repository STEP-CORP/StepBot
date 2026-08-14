/* Mini-app v2 runtime: 3 tabs (Home / Connect / Account), 8 themes, RU/EN.
   Data: /api/cabinet/* with `Authorization: tma <initData>`; falls back to mock.js
   outside Telegram. Theme: admin's template (a..h) from /api/cabinet/config, override
   with ?variant= for preview; light/dark follows Telegram colorScheme. */

(function () {
  "use strict";

  const wa = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const inTg = !!(wa && wa.initData);
  const params = new URLSearchParams(location.search);
  const mock = params.get("mock") === "1" || !inTg;

  // ---------- i18n ----------
  const RU = {
    tabHome: "Главная", tabConnect: "Подключение", tabAccount: "Кабинет",
    active: "Подписка активна", inactive: "Нет подписки", trial: "Пробный период",
    daysLeft: "дней осталось", till: "до", renew: "Продлить", buy: "Купить",
    choosePlan: "Тариф", payMethod: "Оплата", refTitle: "Пригласи друга",
    refText: (d) => `+${d} дней тебе и другу`, share: "Поделиться",
    step1: "Скачай приложение", step1sub: "iOS · Android · macOS · Windows",
    download: "Скачать", step2: "Получи персональную ссылку",
    getLink: "Получить ссылку", openApp: "Открыть в приложении", copy: "Скопировать",
    copied: "Скопировано", step3: "Нажми «Подключить» в приложении",
    step3sub: "Приложение импортирует конфиг и включит защиту",
    profile: "Профиль", subscription: "Подписка", devices: "Устройства",
    myDevices: "Мои устройства",
    deviceRemoved: "Устройство отвязано",
    history: "История платежей", promo: "Промокод", promoPh: "Введи код",
    apply: "Применить", promoOk: "Промокод применён", support: "Поддержка",
    send: "Отпр.", supportPh: "Опишите вопрос…", supportHint: "Напишите нам — ответим здесь.",
    supportTyping: "печатает…", supportEscalated: "Подключаем оператора",
    balance: "Баланс", upTo: "до", noSub: "Сначала оформи подписку",
    topupBtn: "Пополнить", topupTitle: "Пополнение баланса", topupAmount: "Сумма",
    topupSubmit: "Пополнить", topupDone: "Баланс пополнен",
    topupCustom: "Своя сумма", topupCustomBad: "Введите сумму числом",
    topupBelowMin: (m) => `Минимум — ${m}`, topupAboveMax: (m) => `Максимум — ${m}`,
    topupInvoiceUnsupported: "Обнови Telegram или выбери другой способ оплаты — счёт не открылся",
    payBalance: "С баланса", payStars: "Stars", trialBtn: "Попробовать бесплатно",
    bought: "Готово! Подписка активна", error: "Ошибка, попробуй ещё раз",
    version: "v2 · VLESS", loading: "Загрузка…",
    period: "Срок", traffic: "Трафик", unlimited: "∞ безлимит",
    soon: "Тарифы скоро появятся", soonSub: "Мы уже готовим планы — загляните позже.",
    wizTitle: (os) => `Настройка на ${os}`,
    wizIntroSub: "Настройка VPN происходит\nв 3 шага и занимает пару минут",
    wizStart: "Начать настройку на этом устройстве",
    wizOther: "Установить на другом устройстве",
    wizThisDevice: "Настроить это устройство",
    wizPickPlatform: "Какое устройство настраиваем?",
    wizPickApp: "Приложение",
    wizAppTitle: "Приложение",
    wizAppSub: (a) => `Установите приложение ${a}\nи вернитесь к этому экрану`,
    wizInstall: "Установить приложение",
    wizNext: "Следующий шаг",
    wizSubTitle: "Подписка",
    wizSubSub: (a) => `Добавьте подписку в приложение\n${a} с помощью кнопки ниже`,
    wizAddSub: "Добавить подписку",
    wizDoneTitle: "Готово!",
    wizDoneSub: (a) => `Нажмите на круглую кнопку\nвключения VPN в приложении ${a}`,
    wizFinish: "Завершить настройку",
    wizBack: "Назад",
    wizCopyHint: "Или добавьте подписку вручную — скопируйте ссылку и вставьте её в приложении:",
    wizOtherSub: (a) => `Установите ${a} на нужном устройстве, затем откройте эту ссылку там или вставьте её в приложение`,
    wizNeedSub: "Для подключения нужна подписка",
    wizToPlans: "К тарифам",
    wizRetry: "Повторить",
    wizSetupBtn: "Установка и настройка",
    siteLogin: "Вход на сайте", siteLinked: (m) => `Почта ${m} привязана — на сайте входи по ней`,
    siteHint: "Привяжи почту и пароль — сможешь заходить в кабинет с любого браузера, даже когда Telegram недоступен.",
    linkEmailBtn: "Привязать почту", emailPh: "you@example.com", passPh: "Пароль (мин. 8 символов)",
    sendCode: "Получить код", codePh: "Код из письма", confirm: "Подтвердить",
    codeSent: (m) => `Код отправлен на ${m}`, emailLinked: "Почта привязана",
    passShort: "Пароль от 8 символов",
  };
  const EN = {
    ...RU,
    tabHome: "Home", tabConnect: "Connect", tabAccount: "Account",
    active: "Subscription active", inactive: "No subscription", trial: "Trial",
    daysLeft: "days left", till: "till", renew: "Renew", buy: "Buy",
    choosePlan: "Plan", payMethod: "Payment", refTitle: "Invite a friend",
    refText: (d) => `+${d} days for you and a friend`, share: "Share",
    step1: "Download the app", step1sub: "iOS · Android · macOS · Windows",
    download: "Download", step2: "Get your personal link",
    getLink: "Get link", openApp: "Open in app", copy: "Copy", copied: "Copied",
    step3: "Tap “Connect” in the app",
    step3sub: "The app imports the config and turns protection on",
    profile: "Profile", subscription: "Subscription", devices: "Devices",
    myDevices: "My devices",
    deviceRemoved: "Device unlinked",
    history: "Payment history", promo: "Promo code", promoPh: "Enter code",
    apply: "Apply", promoOk: "Promo applied", support: "Support",
    send: "Send", supportPh: "Describe your question…", supportHint: "Message us — we'll reply here.",
    supportTyping: "typing…", supportEscalated: "Connecting an operator",
    balance: "Balance", upTo: "up to", noSub: "Get a subscription first",
    topupBtn: "Top up", topupTitle: "Top up balance", topupAmount: "Amount",
    topupSubmit: "Top up", topupDone: "Balance topped up",
    topupCustom: "Custom amount", topupCustomBad: "Enter a valid amount",
    topupBelowMin: (m) => `Minimum — ${m}`, topupAboveMax: (m) => `Maximum — ${m}`,
    topupInvoiceUnsupported: "Update Telegram or choose another payment method — the invoice didn't open",
    payBalance: "Balance", payStars: "Stars", trialBtn: "Try for free",
    bought: "Done! Subscription is active", error: "Error, try again",
    loading: "Loading…",
    period: "Period", traffic: "Traffic", unlimited: "∞ unlimited",
    soon: "Plans coming soon", soonSub: "We're setting up plans — check back later.",
    wizTitle: (os) => `Setup on ${os}`,
    wizIntroSub: "VPN setup takes 3 steps\nand a couple of minutes",
    wizStart: "Start setup on this device",
    wizOther: "Install on another device",
    wizThisDevice: "Set up this device",
    wizPickPlatform: "Which device are we setting up?",
    wizPickApp: "App",
    wizAppTitle: "The app",
    wizAppSub: (a) => `Install the ${a} app\nand come back to this screen`,
    wizInstall: "Install the app",
    wizNext: "Next step",
    wizSubTitle: "Subscription",
    wizSubSub: (a) => `Add the subscription to ${a}\nusing the button below`,
    wizAddSub: "Add subscription",
    wizDoneTitle: "Done!",
    wizDoneSub: (a) => `Tap the round VPN power\nbutton in the ${a} app`,
    wizFinish: "Finish setup",
    wizBack: "Back",
    wizCopyHint: "Or add it manually — copy the link and paste it into the app:",
    wizOtherSub: (a) => `Install ${a} on the target device, then open this link there or paste it into the app`,
    wizNeedSub: "You need a subscription to connect",
    wizToPlans: "See plans",
    wizRetry: "Retry",
    wizSetupBtn: "Setup & connect",
    siteLogin: "Website login", siteLinked: (m) => `E-mail ${m} is linked — use it to sign in on the website`,
    siteHint: "Link an e-mail and password to open your cabinet from any browser, even when Telegram is down.",
    linkEmailBtn: "Link e-mail", emailPh: "you@example.com", passPh: "Password (8+ chars)",
    sendCode: "Send code", codePh: "Code from the e-mail", confirm: "Confirm",
    codeSent: (m) => `Code sent to ${m}`, emailLinked: "E-mail linked",
    passShort: "Password must be 8+ chars",
  };
  let T = RU;

  // ---------- api ----------
  function authHeaders() {
    return inTg ? { Authorization: `tma ${wa.initData}` } : {};
  }
  async function api(method, path, body) {
    if (mock) {
      const key = path.replace("/api/cabinet/", "").split("?")[0];
      await new Promise((r) => setTimeout(r, 150));
      if (method === "POST") return { ok: true };
      return window.__MOCK__[key] ?? {};
    }
    const res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error((await res.text()).slice(0, 200));
    return res.json();
  }

  // ---------- helpers ----------
  const $ = (sel) => document.querySelector(sel);
  function el(tag, attrs, kids) {
    const n = document.createElement(tag);
    if (attrs)
      for (const [k, v] of Object.entries(attrs)) {
        if (k === "class") n.className = v;
        else if (k === "text") n.textContent = v;
        else if (k === "html") n.innerHTML = v;
        else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
        else n.setAttribute(k, v);
      }
    (kids || []).forEach((c) => c != null && n.append(c.nodeType ? c : String(c)));
    return n;
  }
  function toast(msg) {
    let t = $(".toast");
    if (!t) {
      t = el("div", { class: "toast" });
      document.body.append(t);
    }
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.remove("show"), 2000);
  }
  function money(minor) {
    const v = minor / 100;
    return (v % 1 ? v.toFixed(2) : v.toFixed(0)).replace(/\B(?=(\d{3})+(?!\d))/g, " ") + " ₽";
  }
  function daysLeft(iso) {
    if (!iso) return null;
    return Math.max(0, Math.ceil((new Date(iso) - Date.now()) / 864e5));
  }
  function fmtDate(iso) {
    return iso ? new Date(iso).toLocaleDateString(T === RU ? "ru-RU" : "en-US", { day: "numeric", month: "long" }) : "—";
  }
  function haptic(kind) {
    try {
      if (!wa) return;
      if (kind === "ok") wa.HapticFeedback.notificationOccurred("success");
      else wa.HapticFeedback.impactOccurred("light");
    } catch {}
  }
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      const ta = el("textarea", { style: "position:fixed;opacity:0" });
      ta.value = text;
      document.body.append(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    }
  }
  // Fallback download links, mirroring src/application/services/connection.py CLIENT_STORES.
  // Used only until /connection loads (then app.stores from the API drives the button).
  const APP_STORES = {
    happ: {
      ios: "https://apps.apple.com/app/happ-proxy-utility/id6504287215",
      macos: "https://apps.apple.com/app/happ-proxy-utility/id6504287215",
      android: "https://play.google.com/store/apps/details?id=com.happproxy",
      windows: "https://github.com/Happ-proxy/happ-desktop/releases/latest",
      linux: "https://github.com/Happ-proxy/happ-desktop/releases/latest",
      default: "https://happ.su/",
    },
    v2raytun: {
      ios: "https://apps.apple.com/app/v2raytun/id6476628951",
      macos: "https://apps.apple.com/app/v2raytun/id6476628951",
      android: "https://play.google.com/store/apps/details?id=com.v2raytun.android",
      default: "https://v2raytun.com/",
    },
    hiddify: { default: "https://github.com/hiddify/hiddify-app/releases/latest" },
    streisand: {
      ios: "https://apps.apple.com/app/streisand/id6450534064",
      macos: "https://apps.apple.com/app/streisand/id6450534064",
      default: "https://apps.apple.com/app/streisand/id6450534064",
    },
    incy: {
      ios: "https://apps.apple.com/app/incy/id6756943388",
      macos: "https://apps.apple.com/app/incy/id6756943388",
      android: "https://play.google.com/store/apps/details?id=llc.itdev.incy",
      windows: "https://incy.work/skachat/",
      linux: "https://incy.work/skachat/",
      default: "https://incy.work/",
    },
  };
  // Offline labels for the app picker before /connection is loaded (server list wins after).
  const APP_LABELS = { happ: "Happ", incy: "INCY", v2raytun: "v2RayTun", hiddify: "Hiddify",
                       streisand: "Streisand", shadowrocket: "Shadowrocket", v2box: "V2Box",
                       clash: "Clash Meta", singbox: "sing-box" };
  function detectPlatform() {
    const p = (wa && wa.platform) || "";
    const ua = navigator.userAgent || "";
    if (p === "ios" || /iPhone|iPad/i.test(ua)) return { name: "iOS", os: "ios" };
    if (p === "android" || /Android/i.test(ua)) return { name: "Android", os: "android" };
    if (/Mac/i.test(ua)) return { name: "macOS", os: "macos" };
    if (/Linux/i.test(ua)) return { name: "Linux", os: "linux" };
    return { name: "Windows", os: "windows" };
  }
  // Download URL for the owner's PRIMARY app on this platform. Prefers the API-provided
  // stores (owner config); falls back to the local registry; defaults to Happ.
  function storeFor(os, apps) {
    const primary = (Array.isArray(apps) && apps[0]) || null;
    const key = (primary && primary.key) || "happ";
    const fromApi = primary && primary.stores && (primary.stores[os] || primary.stores.default);
    const fallback = APP_STORES[key] || APP_STORES.happ;
    return fromApi || fallback[os] || fallback.default || APP_STORES.happ.default;
  }

  // ---------- state ----------
  const state = { tab: "home", me: null, plans: null, constructor: null, referral: null, payments: null, connection: null, tariffSel: 0, planSel: 0, cPerSel: 0, cPackSel: 0, paySel: "stars", devices: undefined, topupOpen: false, topupAmountSel: 0, topupPaySel: "stars", topupCustom: "" };
  // Preset top-up amounts (RUB) offered in the mini-app — mirrors the bot's own topup_amount
  // presets (src/bot/handlers/purchase.py); the server re-validates against MIN_DEPOSIT_AMOUNT
  // regardless of which preset the client claims to have tapped.
  const TOPUP_PRESETS_RUB = [100, 250, 500, 1000];
  // Fallback ceiling if /me predates `max_deposit_minor` (older cached response) — mirrors
  // MAX_DEPOSIT_AMOUNT_MINOR in src/core/constants.py. The server always re-validates (#4);
  // this only lets the custom-amount field warn before a round trip.
  const TOPUP_MAX_FALLBACK_MINOR = 100_000_000;
  // admin overrides: {scale, sections:[order], hidden:[keys], buttons:{key:{text,color}},
  // blocks:[{screen,title,text,icon,url,button_label,color}], buttons_extra:[{screen,label,url,color,style}]}
  let UI = {};

  function btnText(key, fallback) {
    const b = UI.buttons && UI.buttons[key];
    return (b && b.text) || fallback;
  }
  function btnStyle(key) {
    const b = UI.buttons && UI.buttons[key];
    return b && b.color ? `background:${b.color}` : "";
  }

  // Only these schemes may be opened — admin/`?ui=` links are attacker-influenceable, so
  // drop javascript:/data:/blob: etc. (defence in depth alongside the server-side validator).
  function safeUrl(u) {
    if (typeof u !== "string" || !u) return null;
    const s = u.trim();
    if (/^(https?:|tg:|mailto:|\/\/|\/)/i.test(s)) return s;  // http(s)/tg/mailto/relative only
    return null;
  }

  // Open an admin-defined link — Telegram links via the native opener, the rest in a tab.
  function openUrl(u) {
    const url = safeUrl(u);
    if (!url) return;
    haptic();
    const tg = url.startsWith("tg://") || /(?:^|\/\/)(?:t\.me|telegram\.me)\//.test(url);
    if (tg && wa && wa.openTelegramLink) wa.openTelegramLink(url);
    else if (wa && wa.openLink) wa.openLink(url);
    else window.open(url, "_blank");
  }

  // Launch a client app from its deep link. A custom scheme (happ://…) CANNOT be opened from
  // inside the Telegram WebView — a direct anchor/navigation fails with ERR_UNKNOWN_URL_SCHEME —
  // and WebApp.openLink() takes http(s) only. So we bounce a custom scheme through our https
  // /dl page (openLink opens it in the EXTERNAL browser, which then hands the scheme to the OS).
  // An https deep link (e.g. a universal link) is opened directly.
  function openApp(link) {
    if (!link) return;
    haptic();
    if (/^https?:/i.test(link)) {
      if (wa && wa.openLink) wa.openLink(link);
      else window.open(link, "_blank");
      return;
    }
    if (wa && wa.openLink) wa.openLink(location.origin + "/dl?to=" + encodeURIComponent(link));
    else location.href = link; // a plain browser routes the scheme to the app itself
  }

  // Admin custom blocks + standalone link-buttons for a given screen (home/connect/account).
  function customItems(screen) {
    const out = [];
    (UI.blocks || []).forEach((b) => {
      if ((b.screen || "home") !== screen) return;
      const kids = [];
      if (b.title) kids.push(el("b", { text: (b.icon ? b.icon + " " : "") + b.title }));
      if (b.text)
        kids.push(el("div", { class: "sub", style: "font-size:13px;margin-top:4px;white-space:pre-line", text: b.text }));
      if (b.url && b.button_label)
        kids.push(el("button", { class: "btn primary sm", style: "margin-top:12px;" + (b.color ? `background:${b.color}` : ""), onclick: () => openUrl(b.url), text: b.button_label }));
      if (kids.length) out.push(el("div", { class: "card fade" }, kids));
    });
    (UI.buttons_extra || []).forEach((x) => {
      if ((x.screen || "home") !== screen) return;
      if (!x.label || !x.url) return;
      out.push(el("button", { class: `btn ${x.style === "ghost" ? "ghost" : "primary"}`, style: x.color ? `background:${x.color}` : "", onclick: () => openUrl(x.url), text: x.label }));
    });
    return out;
  }

  // ---------- screens ----------
  // opts: { excludeBalance, selected, onSelect } — the balance top-up card reuses this same
  // chip pattern but must never offer "pay with balance" and tracks its own selection state.
  function payChips(starsCount, opts) {
    opts = opts || {};
    const me = state.me;
    const app = (me && me.app) || {};
    const gwById = {};
    (app.payment_methods || []).forEach((pm) => { gwById[pm.id] = pm; });
    // Operator-controlled order (PAYMENT_METHOD_ORDER); fall back to balance/stars/gateways
    // for an older cached response that predates the field.
    const order = (app.payment_order && app.payment_order.length)
      ? app.payment_order
      : (app.balance_enabled === false ? [] : ["balance"]).concat(["stars"], (app.payment_methods || []).map((pm) => pm.id));
    const balanceLabel = app.pay_balance_label || T.payBalance;
    const starsLabel = app.pay_stars_label || T.payStars;
    const selected = opts.selected !== undefined ? opts.selected : state.paySel;
    const select = opts.onSelect || ((id) => { state.paySel = id; render(); });
    const chip = (id, text) => el("button", {
      class: `chip${selected === id ? " on" : ""}`,
      onclick: () => select(id),
      text,
    });
    const chips = order.map((id) => {
      if (id === "balance") {
        if (opts.excludeBalance || app.balance_enabled === false) return null;
        return chip("balance", `${balanceLabel} · ${me ? money(me.user.balance_minor) : ""}`);
      }
      if (id === "stars") return chip("stars", starsCount ? `⭐ ${starsLabel} · ${starsCount}` : `⭐ ${starsLabel}`);
      const gw = gwById[id];
      return gw ? chip(gw.id, `💳 ${gw.label}`) : null;
    }).filter(Boolean);
    return el("div", { class: "chips" }, chips);
  }

  // Parses a free-typed RUB amount ("3000" / "3 000,50") into minor units, or null if it
  // isn't a positive number.
  function parseCustomAmountMinor(raw) {
    const s = String(raw || "").trim().replace(/\s/g, "").replace(",", ".");
    if (!s) return null;
    const rub = Number(s);
    if (!isFinite(rub) || rub <= 0) return null;
    return Math.round(rub * 100);
  }

  // Balance top-up card (Account tab): presets + a free-typed custom amount + payChips
  // (Stars/gateways only, no "pay with balance" — you can't top up the wallet from itself).
  //
  // A hard-coded preset alone sent a user who needs, say, 3000 ₽ into the provider's own
  // editable-amount form (e.g. YooMoney quickpay), where they'd type their own number and
  // only the invoice amount gets credited — the gap silently vanishes. The custom field lets
  // them pick their real amount here, validated against min/max before it ever round-trips.
  function topupCard() {
    const me = state.me;
    const app = (me && me.app) || {};
    const minDep = app.min_deposit_minor || 0;
    const maxDep = app.max_deposit_minor || TOPUP_MAX_FALLBACK_MINOR;
    const presets = TOPUP_PRESETS_RUB.map((r) => r * 100).filter((m) => m >= minDep && m <= maxDep);
    const amounts = presets.length ? presets : [Math.min(Math.max(minDep, 1), maxDep) || 10000];
    const amountIdx = state.topupAmountSel < amounts.length ? state.topupAmountSel : 0;

    const customInp = el("input", {
      class: "inp",
      type: "text",
      inputmode: "decimal",
      style: "margin-top:8px",
      placeholder: `${T.topupCustom} (${money(minDep)}–${money(maxDep)})`,
    });
    customInp.value = state.topupCustom || "";
    const chipEls = amounts.map((m, i) =>
      el("button", {
        class: `chip${amountIdx === i && !customInp.value.trim() ? " on" : ""}`,
        onclick: () => { state.topupAmountSel = i; state.topupCustom = ""; haptic(); render(); },
        text: money(m),
      }),
    );
    const hint = el("div", { class: "sub", style: "font-size:11.5px;margin-top:4px;color:var(--warn)" });
    const submitBtn = el("button", { class: "btn primary", style: "margin-top:12px", text: T.topupSubmit });

    function currentAmount() {
      const raw = customInp.value.trim();
      if (!raw) return amounts[amountIdx];
      return parseCustomAmountMinor(raw);
    }
    function refreshSubmit() {
      const raw = customInp.value.trim();
      const amt = currentAmount();
      let bad = "";
      if (raw && amt == null) bad = T.topupCustomBad;
      else if (amt != null && amt < minDep) bad = T.topupBelowMin(money(minDep));
      else if (amt != null && amt > maxDep) bad = T.topupAboveMax(money(maxDep));
      hint.textContent = bad;
      submitBtn.disabled = !amt || !!bad;
      submitBtn.textContent = amt ? `${T.topupSubmit} · ${money(amt)}` : T.topupSubmit;
    }
    customInp.addEventListener("input", () => {
      state.topupCustom = customInp.value;
      chipEls.forEach((c) => c.classList.remove("on"));
      refreshSubmit();
    });
    submitBtn.addEventListener("click", () => {
      const amt = currentAmount();
      if (amt && amt >= minDep && amt <= maxDep) submitTopup(amt);
    });
    refreshSubmit();

    return el("div", { class: "card fade" }, [
      el("div", { class: "h-cap", text: T.topupTitle }),
      el("div", { class: "chips", style: "margin-top:8px" }, chipEls),
      customInp,
      hint,
      el("div", { class: "h-cap", style: "margin-top:12px", text: T.payMethod }),
      payChips("", {
        excludeBalance: true,
        selected: state.topupPaySel,
        onSelect: (id) => { state.topupPaySel = id; render(); },
      }),
      submitBtn,
    ]);
  }

  function orderSections(map) {
    const hidden = Array.isArray(UI.hidden) ? UI.hidden : [];
    const order = Array.isArray(UI.sections) && UI.sections.length
      ? UI.sections
      : ["status", "plans", "referral", "proxy", "custom"];
    const out = [];
    for (const key of order) if (map[key] && !hidden.includes(key)) out.push(...map[key]);
    for (const key of Object.keys(map)) if (!order.includes(key) && !hidden.includes(key)) out.push(...map[key]);
    return out;
  }

  function homeScreen() {
    const me = state.me;
    const sub = me && me.subscription;
    const usable = sub && ["active", "trial", "limited"].includes(sub.status);
    const left = usable ? daysLeft(sub.expire_at) : null;
    const total = 90;
    const sections = { status: [], plans: [], referral: [], proxy: [], custom: customItems("home") };
    const frag = sections.status;

    // Owner greeting (from admin config) — shown once at the very top of Home.
    const greeting = me && me.app && me.app.greeting;
    if (greeting) frag.push(el("div", { class: "card fade", text: greeting }));

    // status card
    frag.push(
      el("div", { class: "card fade" }, [
        el("div", { class: "row spread" }, [
          el("span", { class: "row", style: "gap:7px" }, [
            el("span", { class: `dot${usable ? "" : " off"}` }),
            el("b", { text: usable ? (sub.is_trial ? T.trial : T.active) : T.inactive }),
          ]),
          usable && sub.expire_at
            ? el("span", { class: "sub", style: "font-size:12.5px", text: `${T.till} ${fmtDate(sub.expire_at)}` })
            : null,
        ]),
        usable
          ? el("div", { style: "margin-top:14px" }, [
              el("div", { class: "row", style: "align-items:baseline;gap:8px" }, [
                el("span", { class: "big-num", text: left == null ? "∞" : left }),
                el("span", { class: "sub", text: T.daysLeft }),
              ]),
              el("div", { class: "prog", style: "margin-top:12px" }, [
                el("i", { style: `width:${left == null ? 100 : Math.min(100, (left / total) * 100)}%` }),
              ]),
            ])
          : el("div", { class: "sub", style: "margin-top:10px", text: T.noSub }),
        me && me.user.is_trial_available
          ? el("button", { class: "btn ghost", style: "margin-top:14px;" + btnStyle("trial"), onclick: activateTrial, text: "🎁 " + btnText("trial", T.trialBtn) })
          : null,
      ]),
    );
    // One-tap entry into the guided setup wizard (mirrors the Connect tab).
    frag.push(
      el("button", {
        class: "btn ghost fade wiz-home-btn",
        onclick: () => { haptic(); state.tab = "connect"; render(); },
      }, [
        el("span", { text: "🔌 " + T.wizSetupBtn }),
        el("span", { class: "sub", text: "→" }),
      ]),
    );

    // plans + pay
    const salesMode = params.get("sales") || (me && me.app.sales_mode) || "plans";
    if (salesMode === "constructor") {
      const c = state.constructor;
      const periods = (c && c.periods) || [];
      const packs = (c && c.traffic_packs) || [];
      const per = periods[state.cPerSel] || periods[0];
      const pack = packs[state.cPackSel] || packs[0];
      if (per && pack) {
        const frag = sections.plans;
        const total = per.price_minor + pack.price_minor;
        const stars = Math.max(1, Math.ceil(total / Math.max(1, c.stars_rate || 1)));
        frag.push(
          el("div", { class: "card fade" }, [
            el("div", { class: "h-cap", text: T.period }),
            el(
              "div",
              { class: "plans-row" },
              periods.map((p, i) =>
                el(
                  "div",
                  {
                    class: `plan-opt${(periods[state.cPerSel] ? state.cPerSel : 0) === i ? " on" : ""}`,
                    onclick: () => { state.cPerSel = i; haptic(); render(); },
                  },
                  [
                    el("div", { class: "m", text: p.days < 30 ? `${p.days} дн` : `${p.months} мес` }),
                    el("div", { class: "p", text: money(p.price_minor) }),
                  ],
                ),
              ),
            ),
            el("div", { class: "h-cap", style: "margin-top:14px", text: T.traffic }),
            el(
              "div",
              { class: "chips" },
              packs.map((t, i) =>
                el("button", {
                  class: `chip${(packs[state.cPackSel] ? state.cPackSel : 0) === i ? " on" : ""}`,
                  onclick: () => { state.cPackSel = i; haptic(); render(); },
                  text: (t.gb ? `${t.gb} ГБ` : T.unlimited) + (t.price_minor ? ` · +${money(t.price_minor)}` : ""),
                }),
              ),
            ),
            el("div", { class: "h-cap", style: "margin-top:14px", text: T.payMethod }),
            payChips(stars),
            el("button", {
              class: "btn primary",
              style: "margin-top:14px;" + btnStyle("renew"),
              onclick: () => submitPurchase({ period_id: per.id, pack_id: pack.id }),
              text: `${btnText("renew", usable ? T.renew : T.buy)} · ${money(total)}`,
            }),
          ]),
        );
      } else {
        // Constructor mode with no periods/packs configured yet — show a clear empty state
        // instead of a blank Home tab.
        sections.plans.push(
          el("div", { class: "card fade", style: "text-align:center" }, [
            el("div", { class: "h-cap", text: T.soon }),
            el("div", { class: "muted", style: "margin-top:6px", text: T.soonSub }),
          ]),
        );
      }
    }
    const allPlans = salesMode === "constructor" ? [] : (state.plans && state.plans.items) || [];
    const plan = allPlans[state.tariffSel] || allPlans[0];
    if (plan) {
      const frag = sections.plans;
      const durs = plan.durations;
      const selIdx = durs[state.planSel] ? state.planSel : 0;
      const sel = durs[selIdx];
      const base = durs[0] ? durs[0].price_minor / durs[0].days : 0;
      frag.push(
        el("div", { class: "card fade" }, [
          el("div", { class: "h-cap", text: T.choosePlan }),
          allPlans.length > 1
            ? el(
                "div",
                { class: "chips", style: "margin-bottom:10px" },
                allPlans.map((p, i) =>
                  el("button", {
                    class: `chip${(state.tariffSel || 0) === i ? " on" : ""}`,
                    onclick: () => { state.tariffSel = i; state.planSel = 0; haptic(); render(); },
                    text: p.name,
                  }),
                ),
              )
            : null,
          el(
            "div",
            { class: "plans-row" },
            durs.map((d, i) => {
              const disc = base ? Math.round((1 - d.price_minor / d.days / base) * 100) : 0;
              return el(
                "div",
                {
                  class: `plan-opt${i === selIdx ? " on" : ""}`,
                  style: "position:relative",
                  onclick: () => {
                    state.planSel = i;
                    haptic();
                    render();
                  },
                },
                [
                  i === 1 ? el("span", { class: "badge", text: "★" }) : null,
                  el("div", { class: "m", text: `${d.months} мес` }),
                  el("div", { class: "p", text: money(d.price_minor) }),
                  el("div", { class: "d", text: disc > 0 ? `−${disc}%` : "" }),
                ],
              );
            }),
          ),
          el("div", { class: "h-cap", style: "margin-top:14px", text: T.payMethod }),
          payChips(sel ? sel.price_stars : ""),
          el("button", { class: "btn primary", style: "margin-top:14px;" + btnStyle("renew"), onclick: () => purchase(plan, sel), text: `${btnText("renew", usable ? T.renew : T.buy)} · ${sel ? money(sel.price_minor) : ""}` }),
        ]),
      );
    }

    // referral
    if (state.referral) {
      const frag = sections.referral;
      const r = state.referral;
      frag.push(
        el("div", { class: "card fade row spread" }, [
          el("div", {}, [
            el("b", { text: "🎁 " + T.refTitle }),
            el("div", { class: "sub", style: "font-size:12.5px;margin-top:3px", text: T.refText(r.bonus_days) }),
          ]),
          el("button", {
            class: "btn primary sm",
            style: btnStyle("share"),
            onclick: () => {
              haptic();
              const url = `https://t.me/share/url?url=${encodeURIComponent(r.link)}`;
              wa && wa.openTelegramLink ? wa.openTelegramLink(url) : window.open(url);
            },
            text: btnText("share", T.share),
          }),
        ]),
      );
    }
    if (me && me.app.mtproto_proxy) {
      sections.proxy.push(
        el("div", { class: "card fade row spread" }, [
          el("b", { text: "🔌 " + (T === RU ? "MTProto-прокси" : "MTProto proxy") }),
          el("button", {
            class: "btn primary sm",
            style: btnStyle("connect_proxy"),
            onclick: () => {
              haptic();
              const u = me.app.mtproto_proxy;
              wa && wa.openTelegramLink ? wa.openTelegramLink(u) : window.open(u);
            },
            text: btnText("connect_proxy", T === RU ? "Подключить" : "Connect"),
          }),
        ]),
      );
    }
    return orderSections(sections);
  }

  // ---------- guided connection wizard («Подключение») ----------
  // Four hero screens: intro -> install the app -> add the subscription -> done.
  // Styled entirely with theme variables so every design (a..r) keeps its character.

  const PLATFORMS = [
    { os: "ios", name: "iOS" },
    { os: "android", name: "Android" },
    { os: "windows", name: "Windows" },
    { os: "macos", name: "macOS" },
    { os: "linux", name: "Linux" },
  ];

  function wizApps() {
    // Owner-enabled list from /connection when loaded; offline registry until then.
    const fromApi = state.connection && Array.isArray(state.connection.apps) ? state.connection.apps : null;
    if (fromApi && fromApi.length)
      return fromApi.map((a) => ({ key: a.key, label: a.label, deep_link: a.deep_link, stores: a.stores || {} }));
    return ["happ", "incy", "v2raytun", "hiddify", "streisand"].map((k) => ({
      key: k, label: APP_LABELS[k] || k, deep_link: null, stores: APP_STORES[k] || {},
    }));
  }
  function wizApp() {
    const apps = wizApps();
    return apps.find((a) => a.key === state.wiz.app) || apps[0];
  }
  function wizOs() { return (state.wiz && state.wiz.os) || detectPlatform().os; }
  function wizOsName() {
    const found = PLATFORMS.find((p) => p.os === wizOs());
    return found ? found.name : detectPlatform().name;
  }
  function wizStore() {
    const a = wizApp();
    return (a.stores && (a.stores[wizOs()] || a.stores.default)) || storeFor(wizOs(), state.connection && state.connection.apps);
  }

  function wizIcon(kind) {
    const attrs = 'fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"';
    const inner = {
      plug: '<path d="M9 7.5V3.5M15 7.5V3.5"/><path d="M6.5 7.5h11v3.5a5.5 5.5 0 0 1-11 0Z"/><path d="M12 16.5v4"/>',
      cloud: '<path d="M19.4 16.8A4 4 0 0 0 18 9.1 5.8 5.8 0 0 0 6.8 8.3 4.8 4.8 0 0 0 6 17.6"/><path d="M12 11.5v7.5M9.4 16.4 12 19l2.6-2.6"/>',
      plus: '<circle cx="12" cy="12" r="8.2" stroke-dasharray="3.4 3.6"/><path d="M12 8.4v7.2M8.4 12h7.2"/>',
      check: '<circle cx="12" cy="12" r="8.6"/><path d="m8.2 12.4 2.5 2.5 5.1-5.3"/>',
      shield: '<path d="M12 3.5 5.5 6v5c0 4.4 2.9 7.4 6.5 8.5 3.6-1.1 6.5-4.1 6.5-8.5V6Z"/>',
    }[kind] || "";
    const div = document.createElement("div");
    div.className = "wiz-icon";
    div.innerHTML = `<svg viewBox="0 0 24 24" ${attrs}>${inner}</svg>`;
    return div;
  }

  function wizHero(step, icon) {
    // Concentric guide rings + a progress arc (step of 3) + the step icon in the middle.
    const R = 55, C = (2 * Math.PI * R).toFixed(1);
    const dash = ((Math.max(0, Math.min(3, step)) / 3) * C).toFixed(1);
    const hero = el("div", { class: "wiz-hero" });
    hero.innerHTML =
      '<svg class="wiz-arc" viewBox="0 0 120 120">' +
      `<circle cx="60" cy="60" r="${R}" fill="none" stroke="var(--line)" stroke-width="1.5" opacity="0.7"/>` +
      (step > 0
        ? `<circle cx="60" cy="60" r="${R}" fill="none" stroke="var(--acc)" stroke-width="3" stroke-linecap="round" stroke-dasharray="${dash} ${C}" transform="rotate(-90 60 60)"/>`
        : "") +
      "</svg>";
    hero.append(wizIcon(icon));
    return hero;
  }

  function wizConfetti(host) {
    const colors = ["var(--acc)", "#E5484D", "#F5A623", "#3ECF8E", "#5B8DEF", "#B476E5"];
    for (let i = 0; i < 46; i++) {
      const p = el("span", { class: "wiz-confetti" });
      p.style.left = Math.random() * 100 + "%";
      p.style.background = colors[i % colors.length];
      p.style.animationDelay = (Math.random() * 0.9).toFixed(2) + "s";
      p.style.animationDuration = (2.2 + Math.random() * 1.6).toFixed(2) + "s";
      host.append(p);
    }
  }

  function wizSet(patch) { Object.assign(state.wiz, patch); haptic(); render(); }

  function connectScreen() {
    if (!state.wiz) state.wiz = { step: 0, app: null, os: null, other: false, connReq: false, connErr: false };
    const w = state.wiz;
    if (mock && !state.connection && window.__MOCK__) state.connection = window.__MOCK__.connection;
    // The personal link (and the owner's true app list) — fetched lazily. connErr also gates
    // the fetch so a failure doesn't hammer the API every render; a manual «Повторить» (and
    // load() after a purchase) clears connReq/connErr to allow a fresh attempt.
    if (!mock && !state.connection && !w.connReq && !w.connErr) {
      w.connReq = true;
      api("GET", "/api/cabinet/connection")
        .then((r) => { state.connection = r; w.connReq = false; render(); })
        .catch(() => { w.connReq = false; w.connErr = true; render(); });
    }
    const app = wizApp();
    const conn = state.connection;
    const screen = el("div", { class: "wiz fade" });
    const actions = el("div", { class: "wiz-actions" });
    const title = (t) => el("div", { class: "wiz-title", text: t });
    const sub = (t) => {
      const d = el("div", { class: "wiz-sub" });
      String(t).split("\n").forEach((line, i) => {
        if (i) d.append(document.createElement("br"));
        d.append(document.createTextNode(line));
      });
      return d;
    };
    const back = (to) => el("span", { class: "wiz-back", onclick: () => wizSet({ step: to }), text: "← " + T.wizBack });

    if (w.step === 0) {
      screen.append(title(T.wizTitle(wizOsName())), sub(T.wizIntroSub));
      const apps = wizApps();
      if (apps.length > 1) {
        const chips = el("div", { class: "wiz-chips" });
        apps.forEach((a) => chips.append(el("button", {
          class: "wiz-chip" + (a.key === app.key ? " on" : ""),
          onclick: () => wizSet({ app: a.key }),
          text: a.label,
        })));
        screen.append(el("div", { class: "wiz-cap", text: T.wizPickApp }), chips);
      }
      if (w.other) {
        const plats = el("div", { class: "wiz-chips" });
        PLATFORMS.forEach((p) => plats.append(el("button", {
          class: "wiz-chip" + (wizOs() === p.os ? " on" : ""),
          onclick: () => wizSet({ os: p.os }),
          text: p.name,
        })));
        screen.append(el("div", { class: "wiz-cap", text: T.wizPickPlatform }), plats);
      }
      screen.append(wizHero(0, "plug"));
      actions.append(
        el("button", { class: "btn primary wiz-btn", style: btnStyle("open_app"), onclick: () => wizSet({ step: 1 }), text: w.other ? T.wizNext + " →" : T.wizStart }),
        el("button", { class: "btn ghost wiz-btn", onclick: () => wizSet({ other: !w.other, os: null }), text: w.other ? T.wizThisDevice : T.wizOther }),
      );
    } else if (w.step === 1) {
      screen.append(title(T.wizAppTitle), sub(T.wizAppSub(app.label)), wizHero(1, "cloud"));
      const storeUrl = wizStore();
      actions.append(
        el("button", {
          class: "btn primary wiz-btn",
          onclick: () => { haptic(); wa && wa.openLink ? wa.openLink(storeUrl) : window.open(storeUrl, "_blank"); },
          text: "⤓ " + T.wizInstall,
        }),
        el("button", { class: "btn ghost wiz-btn", onclick: () => wizSet({ step: 2 }), text: T.wizNext + " →" }),
        back(0),
      );
    } else if (w.step === 2) {
      screen.append(title(T.wizSubTitle), sub(w.other ? T.wizOtherSub(app.label) : T.wizSubSub(app.label)), wizHero(2, "plus"));
      // hide_link shops return subscription_url:null but keep working deep_links — the step
      // must still render (one-tap import), otherwise it hangs forever on «Загрузка…».
      if (conn && (conn.subscription_url || conn.hide_link)) {
        const link = (conn.deep_links && conn.deep_links[app.key]) || app.deep_link;
        if (!w.other && link) {
          actions.append(el("button", {
            class: "btn primary wiz-btn",
            style: btnStyle("open_app"),
            onclick: () => openApp(link),
            text: "⊕ " + btnText("open_app", T.wizAddSub),
          }));
        }
        if (!conn.hide_link) {
          if (!w.other) actions.append(el("div", { class: "wiz-cap", text: T.wizCopyHint }));
          actions.append(
            el("div", { class: "link-box mono wiz-link", text: conn.subscription_url }),
            el("button", {
              class: "btn ghost wiz-btn",
              onclick: async () => { (await copyText(conn.subscription_url)) && toast(T.copied); haptic("ok"); },
              text: T.copy,
            }),
          );
        } else if (w.other && link) {
          // Raw link hidden by the owner: hand over the one-tap import link instead (HIDE-1
          // keeps deep links working, so copying one to the other device is consistent).
          actions.append(el("button", {
            class: "btn ghost wiz-btn",
            onclick: async () => { (await copyText(link)) && toast(T.copied); haptic("ok"); },
            text: T.copy,
          }));
        }
        actions.append(el("button", { class: "btn ghost wiz-btn", onclick: () => wizSet({ step: 3 }), text: T.wizNext + " →" }));
      } else if (w.connErr) {
        actions.append(
          el("div", { class: "wiz-cap", text: T.wizNeedSub }),
          el("button", { class: "btn primary wiz-btn", onclick: () => { haptic(); state.tab = "home"; render(); }, text: T.wizToPlans }),
          el("button", { class: "btn ghost wiz-btn", onclick: () => { w.connErr = false; w.connReq = false; render(); }, text: T.wizRetry }),
        );
      } else {
        actions.append(el("div", { class: "wiz-cap", text: T.loading }));
      }
      actions.append(back(1));
    } else {
      screen.append(title(T.wizDoneTitle), sub(T.wizDoneSub(app.label)), wizHero(3, "check"));
      wizConfetti(screen);
      actions.append(
        el("button", {
          class: "btn primary wiz-btn",
          onclick: () => { haptic("ok"); state.wiz = null; state.tab = "home"; render(); },
          text: T.wizFinish,
        }),
        back(2),
      );
    }
    screen.append(actions);
    return [screen].concat(customItems("connect"));
  }

  function accountScreen() {
    const me = state.me;
    if (!me) return [];
    const sub = me.subscription;
    const frag = [];
    frag.push(
      el("div", { class: "card fade row", style: "gap:12px" }, [
        el("div", {
          style:
            "width:46px;height:46px;border-radius:50%;background:var(--soft);color:var(--acc);display:grid;place-items:center;font-weight:800;font-size:17px",
          text: (me.user.first_name || "?").slice(0, 1).toUpperCase(),
        }),
        el("div", {}, [
          el("b", { text: me.user.first_name || "—" }),
          el("div", { class: "sub", style: "font-size:12.5px", text: me.user.username ? "@" + me.user.username : "" }),
        ]),
        el("div", { style: "margin-left:auto;text-align:right" }, [
          el("div", { class: "sub", style: "font-size:11px", text: T.balance }),
          el("b", { text: money(me.user.balance_minor) }),
          me.app.balance_enabled !== false
            ? el("button", {
                class: "btn ghost sm",
                style: "margin-top:6px;padding:3px 10px;font-size:12px",
                onclick: () => { state.topupOpen = !state.topupOpen; haptic(); render(); },
                text: T.topupBtn,
              })
            : null,
        ]),
      ]),
    );
    if (state.topupOpen && me.app.balance_enabled !== false) frag.push(topupCard());
    frag.push(
      el("div", { class: "card fade" }, [
        el("div", { class: "li" }, [
          el("span", { class: "sub", text: T.subscription }),
          el("b", { text: sub && sub.expire_at ? `${T.till} ${fmtDate(sub.expire_at)}` : "—" }),
        ]),
        el("div", { class: "li" }, [
          el("span", { class: "sub", text: T.devices }),
          el("b", { text: sub && sub.device_limit ? `${T.upTo} ${sub.device_limit}` : "—" }),
        ]),
      ]),
    );
    // HWID devices: list + one-tap unbind (loaded lazily per tab visit)
    if (sub && sub.status && sub.status !== "none" && !mock) {
      if (state.devices === undefined) {
        state.devices = null;
        api("GET", "/api/cabinet/devices")
          .then((r) => { state.devices = r.items || []; render(); })
          .catch(() => { state.devices = []; });
      }
      if (state.devices && state.devices.length) {
        frag.push(
          el("div", { class: "card fade" }, [
            el("div", { class: "h-cap", text: T.myDevices }),
            ...state.devices.map((d) =>
              el("div", { class: "li" }, [
                el("span", { class: "sub", text: [d.platform, d.model].filter(Boolean).join(" · ") || d.hwid.slice(0, 12) }),
                el("button", {
                  class: "btn ghost sm",
                  text: "✕",
                  onclick: async () => {
                    try {
                      await api("DELETE", `/api/cabinet/devices/${encodeURIComponent(d.hwid)}`);
                      // `undefined` (not null) is the "refetch me" sentinel — null means
                      // "load in flight" and would leave the list hidden until an app restart.
                      state.devices = undefined;
                      toast(T.deviceRemoved);
                      render();
                    } catch (e) {
                      toast(String(e.message || e));
                    }
                  },
                }),
              ]),
            ),
          ]),
        );
      }
    }
    // promo
    const inp = el("input", { class: "inp", placeholder: T.promoPh, maxlength: 32 });
    frag.push(
      el("div", { class: "card fade" }, [
        el("div", { class: "h-cap", text: T.promo }),
        el("div", { class: "row" }, [
          inp,
          el("button", {
            class: "btn primary sm",
            onclick: async () => {
              if (!inp.value.trim()) return;
              try {
                const r = await api("POST", "/api/cabinet/promocode", { code: inp.value.trim() });
                toast(r.ok ? T.promoOk : r.message || T.error);
                r.ok && haptic("ok");
                r.ok && load();
              } catch {
                toast(T.error);
              }
            },
            text: T.apply,
          }),
        ]),
      ]),
    );
    // history
    if (state.payments && state.payments.items.length) {
      frag.push(
        el("div", { class: "card fade" }, [
          el("div", { class: "h-cap", text: T.history }),
          ...state.payments.items.slice(0, 6).map((p) =>
            el("div", { class: "li" }, [
              el("span", { class: "sub", style: "font-size:12.5px", text: `${new Date(p.created_at).toLocaleDateString("ru-RU")} · ${p.method || p.type}` }),
              el("b", { text: money(p.amount_minor) }),
            ]),
          ),
        ]),
      );
    }
    // Website access: link an e-mail + password so the same account opens in a browser
    // (the client-requested «связка» — one account everywhere).
    if (!mock) {
      if (state.linked === undefined) {
        state.linked = null;
        api("GET", "/api/cabinet/linked")
          .then((r) => { state.linked = r; render(); })
          .catch(() => { state.linked = false; });
      }
      if (state.linked) {
        const siteCard = el("div", { class: "card fade" }, [
          el("div", { class: "h-cap", text: T.siteLogin }),
        ]);
        if (state.linked.email && state.linked.email_verified) {
          siteCard.append(el("div", { class: "sub", style: "font-size:12.5px", text: T.siteLinked(state.linked.email) }));
        } else if (state.linkEmail && state.linkEmail.step === "code") {
          const codeInp = el("input", { class: "inp", inputmode: "numeric", placeholder: T.codePh, maxlength: 8 });
          siteCard.append(
            el("div", { class: "sub", style: "font-size:12.5px;margin-bottom:6px", text: T.codeSent(state.linkEmail.email) }),
            el("div", { class: "row" }, [
              codeInp,
              el("button", {
                class: "btn primary sm", text: T.confirm,
                onclick: async () => {
                  if (!codeInp.value.trim()) return;
                  try {
                    await api("POST", "/api/cabinet/link/email/confirm", { code: codeInp.value.trim() });
                    state.linked = undefined; state.linkEmail = null;
                    toast(T.emailLinked); haptic("ok"); render();
                  } catch (e) { toast(String(e.message || e)); }
                },
              }),
            ]),
          );
        } else if (state.linkEmail && state.linkEmail.step === "form") {
          const emailInp = el("input", { class: "inp", type: "email", placeholder: T.emailPh, maxlength: 255 });
          const passInp = el("input", { class: "inp", type: "password", placeholder: T.passPh, maxlength: 128 });
          siteCard.append(
            emailInp,
            el("div", { style: "height:6px" }),
            passInp,
            el("div", { style: "height:8px" }),
            el("button", {
              class: "btn primary sm", text: T.sendCode,
              onclick: async () => {
                if (passInp.value.length < 8) return toast(T.passShort);
                try {
                  await api("POST", "/api/cabinet/link/email", { email: emailInp.value.trim(), password: passInp.value });
                  state.linkEmail = { step: "code", email: emailInp.value.trim() };
                  render();
                } catch (e) { toast(String(e.message || e)); }
              },
            }),
          );
        } else {
          siteCard.append(
            el("div", { class: "sub", style: "font-size:12.5px;margin-bottom:8px", text: T.siteHint }),
            el("button", {
              class: "btn ghost sm", text: T.linkEmailBtn,
              onclick: () => { state.linkEmail = { step: "form" }; render(); },
            }),
          );
        }
        frag.push(siteCard);
      }
    }
    if (me.app.mtproto_proxy) {
      frag.push(
        el("div", { class: "card fade row spread" }, [
          el("div", {}, [
            el("b", { text: "🔌 " + (T === RU ? "MTProto-прокси" : "MTProto proxy") }),
            el("div", { class: "sub", style: "font-size:12px;margin-top:2px",
                        text: T === RU ? "Telegram без блокировок" : "Telegram without blocks" }),
          ]),
          el("button", {
            class: "btn primary sm",
            onclick: () => {
              haptic();
              const u = me.app.mtproto_proxy;
              wa && wa.openTelegramLink ? wa.openTelegramLink(u) : window.open(u);
            },
            text: T === RU ? "Подключить" : "Connect",
          }),
        ]),
      );
    }
    // Support: inline chat (AI-backed via /api/cabinet/support; operator replies arrive here too).
    if (state.support === undefined) state.support = { messages: null, sending: false };
    if (!mock && state.support.messages === null) {
      state.support.messages = [];
      api("GET", "/api/cabinet/support")
        .then((r) => { state.support.messages = r.messages || []; render(); })
        .catch(() => {});
    }
    const supMsgs = state.support.messages || [];
    const supInp = el("input", { class: "inp", placeholder: T.supportPh, maxlength: 1000 });
    async function sendSupport() {
      const v = supInp.value.trim();
      if (!v || state.support.sending) return;
      supInp.value = "";
      state.support.messages = supMsgs.concat([{ from: "you", text: v }]);
      state.support.sending = true;
      haptic();
      render();
      try {
        const r = await api("POST", "/api/cabinet/support", { text: v });
        if (mock) {
          // Standalone preview: no backend — show a canned assistant reply.
          state.support.messages = state.support.messages.concat([
            { from: "support", text: "Спасибо за обращение! Это демо-режим — в боевом кабинете здесь ответит ИИ-поддержка." },
          ]);
        } else {
          // Live: refetch the full thread (user message + AI/operator replies) in order.
          try { state.support.messages = (await api("GET", "/api/cabinet/support")).messages || []; } catch {}
          if (r && r.ai_outcome === "escalate") toast(T.supportEscalated);
        }
      } catch (e) {
        toast((e.message || T.error).slice(0, 120));
      }
      state.support.sending = false;
      render();
    }
    frag.push(
      el("div", { class: "card fade" }, [
        el("div", { class: "h-cap", text: "🆘 " + T.support }),
        ...(supMsgs.length
          ? supMsgs.slice(-8).map((m) =>
              el("div", { style: `margin:4px 0;text-align:${m.from === "you" ? "right" : "left"}` }, [
                el("span", {
                  style:
                    "display:inline-block;max-width:85%;padding:7px 11px;border-radius:12px;" +
                    "font-size:13.5px;white-space:pre-line;text-align:left;" +
                    (m.from === "you"
                      ? "background:var(--acc);color:var(--accInk)"
                      : "background:var(--soft);color:var(--ink)"),
                  text: m.text,
                }),
              ]),
            )
          : [el("div", { class: "sub", style: "font-size:12.5px", text: T.supportHint })]),
        state.support.sending
          ? el("div", { class: "sub", style: "font-size:12px;margin-top:4px", text: T.supportTyping })
          : null,
        el("div", { class: "row", style: "margin-top:8px" }, [
          supInp,
          el("button", { class: "btn primary sm", text: T.send, onclick: sendSupport }),
        ]),
      ].filter(Boolean)),
    );
    frag.push(el("div", { class: "sub", style: "text-align:center;font-size:11px;opacity:.7", text: T.version }));
    return frag.slice(0, -1).concat(customItems("account"), frag.slice(-1));
  }

  // ---------- actions ----------
  async function purchase(plan, dur) {
    if (!dur) return;
    await submitPurchase({ plan_id: plan.id, days: dur.days });
  }

  async function submitPurchase(payload) {
    haptic();
    try {
      const method =
        state.paySel === "balance" && state.me && state.me.app.balance_enabled === false
          ? "stars"
          : state.paySel;
      const r = await api("POST", "/api/cabinet/purchase", { ...payload, method });
      if (r.redirect_url) {
        wa && wa.openLink ? wa.openLink(r.redirect_url) : window.open(r.redirect_url, "_blank");
        toast(T === RU ? "Оплати по открывшейся ссылке" : "Complete the payment in the opened page");
        setTimeout(load, 4000);
      } else if (r.invoice_link && wa && wa.openInvoice) {
        wa.openInvoice(r.invoice_link, (status) => {
          if (status === "paid") {
            toast(T.bought);
            haptic("ok");
            setTimeout(load, 1200);
          }
        });
      } else if (r.ok) {
        toast(T.bought);
        haptic("ok");
        load();
      }
    } catch (e) {
      toast((e.message || T.error).slice(0, 120));
    }
  }

  async function submitTopup(amountMinor) {
    haptic();
    const method = state.topupPaySel || "stars";
    try {
      const r = await api("POST", "/api/cabinet/topup", { amount_minor: amountMinor, method });
      if (r.redirect_url) {
        wa && wa.openLink ? wa.openLink(r.redirect_url) : window.open(r.redirect_url, "_blank");
        toast(T === RU ? "Оплати по открывшейся ссылке" : "Complete the payment in the opened page");
        setTimeout(load, 4000);
      } else if (r.invoice_link) {
        if (wa && wa.openInvoice) {
          wa.openInvoice(r.invoice_link, (status) => {
            if (status === "paid") {
              toast(T.topupDone);
              haptic("ok");
              state.topupOpen = false;
              setTimeout(load, 1200);
            }
          });
        } else {
          // Old Telegram client without WebApp.openInvoice — the Stars invoice was created but
          // nothing was charged. Must NOT claim success here: /topup never returns a bare
          // {ok:true} (method "balance" is rejected server-side), so the old `else if (r.ok)`
          // fallback used to fire on exactly this case and lie that the balance was topped up.
          toast(T.topupInvoiceUnsupported);
        }
      } else if (r.ok) {
        // Defensive only — kept in case a future server change adds a synchronous success
        // shape; today /topup only ever returns invoice_link or redirect_url.
        toast(T.topupDone);
        haptic("ok");
        state.topupOpen = false;
        load();
      }
    } catch (e) {
      toast((e.message || T.error).slice(0, 120));
    }
  }

  async function activateTrial() {
    haptic();
    try {
      await api("POST", "/api/cabinet/trial");
      toast(T.bought);
      haptic("ok");
      load();
    } catch (e) {
      toast((e.message || T.error).slice(0, 120));
    }
  }

  async function loadConnection() {
    haptic();
    try {
      state.connection = await api("GET", "/api/cabinet/connection");
      render();
    } catch {
      toast(T.noSub);
    }
  }

  // ---------- render ----------
  function render() {
    const screen = $("#screen");
    screen.innerHTML = "";
    const frag =
      state.tab === "home" ? homeScreen() : state.tab === "connect" ? connectScreen() : accountScreen();
    frag.filter(Boolean).forEach((n) => screen.append(n));
    document.querySelectorAll(".tabs button").forEach((b) => {
      b.classList.toggle("on", b.dataset.tab === state.tab);
    });
  }

  async function load() {
    try {
      // Only /me and /plans are load-critical. A failure in a secondary call (referral /
      // payments / constructor) must NOT drop the whole app to the error screen — degrade it
      // to null so the relevant tab just renders empty.
      const [me, plans, constructor, referral, payments] = await Promise.all([
        api("GET", "/api/cabinet/me"),
        api("GET", "/api/cabinet/plans"),
        api("GET", "/api/cabinet/constructor").catch(() => null), // pre-constructor backends
        api("GET", "/api/cabinet/referral").catch(() => null),
        api("GET", "/api/cabinet/payments").catch(() => null),
      ]);
      Object.assign(state, { me, plans, constructor, referral, payments });
      // A full refresh (boot, or after a purchase) invalidates the cached connection + wizard
      // fetch flags — so a user who hit the Connect tab BEFORE buying (got a 404, connErr set)
      // sees the wizard work the moment they come back with a live subscription. Devices too.
      state.connection = null;
      state.devices = undefined;
      if (state.wiz) { state.wiz.connReq = false; state.wiz.connErr = false; }
      // Owner branding: title → document/tab title; greeting shown atop Home.
      // ?title=/?greeting= let the admin preview override the (mock) config.
      const title = params.get("title") || me.app.title;
      if (title) document.title = title;
      if (params.get("greeting") != null) me.app.greeting = params.get("greeting");
      // theme from admin config (?variant= wins for preview)
      const NAMES = { minimal: "a", private: "b", buddy: "c", native: "d",
                      terminal: "e", magazine: "f", neon: "g", pop: "h",
                      onyx: "i", swiss: "j", ledger: "k", graphite: "l", atlas: "m",
                      noir: "n", steel: "o", ivory: "p", sable: "q", quartz: "r" };
      let variant = params.get("variant") || me.app.template || "a";
      variant = NAMES[variant] || variant;
      document.body.dataset.variant = /^[a-r]$/.test(variant) ? variant : "a";
      const accent = params.get("accent") || (!params.get("variant") ? me.app.accent_color : null);
      if (accent && /^#[0-9a-fA-F]{3,8}$/.test(accent)) {
        document.body.style.setProperty("--acc", accent);
      }
      try {
        UI = params.get("ui")
          ? JSON.parse(decodeURIComponent(escape(atob(params.get("ui")))))
          : (me.app.ui || {});
      } catch { UI = me.app.ui || {}; }
      if (UI.scale) document.documentElement.style.fontSize = `${(UI.scale / 100) * 100}%`;
      T = (params.get("lang") || me.user.language) === "en" ? EN : RU;
      document.documentElement.lang = T === EN ? "en" : "ru";
      // Translate the static tab-bar labels (index.html has data-i18n but nothing applied it,
      // so an English user saw «Главная / Подключение / Кабинет» under English content).
      document.querySelectorAll("[data-i18n]").forEach((n) => {
        const key = n.getAttribute("data-i18n");
        if (key && T[key] != null && typeof T[key] === "string") n.textContent = T[key];
      });
      // Preview helpers (admin iframe / screenshots): ?tab= opens a tab, ?wstep= a wizard step.
      const tabParam = params.get("tab");
      if (tabParam && ["home", "connect", "account"].includes(tabParam)) state.tab = tabParam;
      const wstep = parseInt(params.get("wstep") || "", 10);
      if (!Number.isNaN(wstep)) {
        state.tab = "connect";
        state.wiz = { step: Math.max(0, Math.min(3, wstep)), app: params.get("wapp") || null, os: null, other: params.get("wother") === "1", connReq: false, connErr: false };
      }
      render();
    } catch (e) {
      $("#screen").innerHTML = `<div class="skel">${T.error}</div>`;
    }
  }

  // ---------- boot ----------
  if (wa) {
    try {
      wa.ready();
      wa.expand();
    } catch {}
  }
  const scheme = params.get("mode") || (wa && wa.colorScheme) || "light";
  document.body.dataset.mode = scheme === "dark" ? "dark" : "light";
  if (wa && wa.onEvent) wa.onEvent("themeChanged", () => (document.body.dataset.mode = wa.colorScheme));

  document.querySelectorAll(".tabs button").forEach((b) => {
    b.addEventListener("click", () => {
      state.tab = b.dataset.tab;
      haptic();
      render();
      if (state.tab === "connect" && !state.connection && mock) state.connection = window.__MOCK__.connection, render();
    });
  });

  $("#screen").innerHTML = '<div class="skel"><div class="spinner"></div></div>';
  load();
})();
