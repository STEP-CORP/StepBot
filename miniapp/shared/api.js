/**
 * Contract client. Talks to the live cabinet API when inside Telegram; falls back to
 * window.__CABINET_MOCK__ for standalone browser preview (or when forced with ?mock=1).
 *
 * Endpoints (see ../CONTRACT.md):
 *   GET  /api/cabinet/me
 *   GET  /api/cabinet/plans
 *   GET  /api/cabinet/referral
 *   POST /api/cabinet/promocode          { code }
 *   POST /api/cabinet/purchase           { public_code, days }
 *   POST /api/cabinet/topup              { amount_minor, method }
 *   POST /api/cabinet/subscription/reset-devices
 *
 * Auth: every request carries `Authorization: tma <initData>` (Telegram Mini Apps).
 */
(function () {
  "use strict";
  const Cabinet = (window.Cabinet = window.Cabinet || {});
  const tg = Cabinet.tg;

  const params = new URLSearchParams(location.search);
  const forceMock = params.get("mock") === "1";
  // Mock when explicitly forced or when not running inside Telegram (standalone preview).
  const useMock = forceMock || !tg.inside;

  const BASE = (window.__CABINET_API_BASE__ || "").replace(/\/$/, "");

  function headers() {
    const h = { "Content-Type": "application/json" };
    const auth = tg.initDataAuthHeader();
    if (auth) h["Authorization"] = auth;
    return h;
  }

  async function req(method, path, body) {
    const res = await fetch(`${BASE}${path}`, {
      method,
      headers: headers(),
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new Error(`${res.status} ${res.statusText} ${detail}`.trim());
    }
    return res.status === 204 ? null : res.json();
  }

  // ---- mock plumbing -------------------------------------------------------
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));
  function mock() {
    const m = window.__CABINET_MOCK__;
    if (!m) throw new Error("mock data missing (include mock/mock-data.js)");
    return m;
  }

  // ---- reads ---------------------------------------------------------------
  async function getMe() {
    if (useMock) return delay(220).then(() => mock().me);
    return req("GET", "/api/cabinet/me");
  }
  async function getPlans() {
    if (useMock) return delay(220).then(() => mock().plans);
    return req("GET", "/api/cabinet/plans");
  }
  async function getReferral() {
    if (useMock) return delay(220).then(() => mock().referral);
    return req("GET", "/api/cabinet/referral");
  }

  // ---- writes (mocked responses in preview) --------------------------------
  async function applyPromo(code) {
    if (useMock) {
      await delay(400);
      const ok = /^[A-Za-z0-9]{4,}$/.test((code || "").trim());
      return { ok, reward: ok ? { type: "balance", amount_minor: 5000 } : null };
    }
    return req("POST", "/api/cabinet/promocode", { code });
  }
  async function purchase(publicCode, days) {
    if (useMock) {
      await delay(400);
      return { ok: true, payment_url: null, message: "mock: invoice created" };
    }
    return req("POST", "/api/cabinet/purchase", { public_code: publicCode, days });
  }
  async function resetDevices() {
    if (useMock) {
      await delay(400);
      return { ok: true };
    }
    return req("POST", "/api/cabinet/subscription/reset-devices");
  }
  async function topup(amountMinor, method) {
    if (useMock) {
      await delay(400);
      return { ok: true, invoice_link: null, redirect_url: null };
    }
    return req("POST", "/api/cabinet/topup", { amount_minor: amountMinor, method });
  }

  Cabinet.api = {
    useMock,
    getMe,
    getPlans,
    getReferral,
    applyPromo,
    purchase,
    resetDevices,
    topup,
  };
})();
