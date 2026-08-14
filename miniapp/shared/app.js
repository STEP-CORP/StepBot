/**
 * Orchestrator: resolves locale + Telegram theme, loads the three read endpoints,
 * and normalizes them into a single camelCase view-model that every template renders.
 *
 * A template only needs: `Cabinet.boot({ render, onError })` and `Cabinet.actions.*`.
 * It never touches the raw API shapes — keeps presentation and data cleanly separated.
 */
(function () {
  "use strict";
  const Cabinet = (window.Cabinet = window.Cabinet || {});
  const { fmt, api, tg, i18n } = Cabinet;

  function resolveLocale(mePayload) {
    const q = new URLSearchParams(location.search).get("lang");
    const cand =
      q ||
      tg.userLanguage() ||
      (mePayload && mePayload.user && mePayload.user.language) ||
      "ru";
    return i18n.setLocale(cand.slice(0, 2).toLowerCase());
  }

  function discounted(minor, pct) {
    if (!pct) return minor;
    return Math.round((minor * (100 - pct)) / 100);
  }

  /** Merge the three payloads into the normalized model templates consume. */
  function buildModel(me, plans, referral) {
    const loc = i18n.getLocale();
    const dateLoc = loc === "ru" ? "ru-RU" : "en-US";
    const currency = (me.user && me.user.currency) || (plans && plans.currency) || "RUB";
    const pct = (me.user && me.user.personal_discount_pct) || 0;

    const sub = me.subscription;
    let subscription = null;
    if (sub && sub.status && sub.status !== "none") {
      const tr = sub.traffic || {};
      const daysLeft = fmt.daysLeft(sub.expire_at);
      const totalDays =
        sub.start_at && sub.expire_at
          ? Math.max(1, Math.round((new Date(sub.expire_at) - new Date(sub.start_at)) / 86400000))
          : null;
      const daysPct =
        daysLeft == null
          ? 0
          : totalDays
            ? Math.max(0, Math.min(100, Math.round((daysLeft / totalDays) * 100)))
            : Math.min(100, Math.round((daysLeft / 30) * 100));
      subscription = {
        status: sub.status,
        statusLabel: Cabinet.t("status." + sub.status),
        isTrial: !!sub.is_trial,
        planName: sub.plan_name || "—",
        startAt: sub.start_at,
        expireAt: sub.expire_at,
        expireLabel: fmt.date(sub.expire_at, dateLoc),
        daysLeft: daysLeft,
        totalDays: totalDays,
        daysPct: daysPct,
        trafficUsed: tr.used_bytes || 0,
        trafficLimit: tr.limit_bytes || 0,
        unlimited: !!tr.unlimited || !tr.limit_bytes,
        trafficUsedLabel: fmt.bytes(tr.used_bytes || 0),
        trafficLimitLabel: tr.unlimited || !tr.limit_bytes ? Cabinet.t("unlimited") : fmt.bytes(tr.limit_bytes),
        trafficPct: fmt.trafficPct(tr.used_bytes, tr.limit_bytes, tr.unlimited),
        deviceLimit: sub.device_limit,
        subscriptionUrl: sub.subscription_url || "",
        cryptoLink: sub.crypto_link || "",
        autopay: !!sub.autopay_enabled,
      };
    } else {
      subscription = { status: "none", statusLabel: Cabinet.t("status.none") };
    }

    const planItems = ((plans && plans.items) || []).map((p) => ({
      code: p.public_code,
      name: p.name,
      description: p.description || "",
      type: p.type,
      trafficLimit: p.traffic_limit_bytes,
      unlimited: !p.traffic_limit_bytes,
      trafficLabel: p.traffic_limit_bytes ? fmt.bytes(p.traffic_limit_bytes) : Cabinet.t("unlimited"),
      deviceLimit: p.device_limit,
      isCurrent: !!p.is_current,
      durations: (p.durations || []).map((d) => {
        // The server already quotes the discounted final price_minor (promo+personal+sale, cap
        // 100%) per user, so never re-apply a discount here — that double-discounted (#4).
        // base_price_minor is the list price for a strikethrough.
        const base = d.base_price_minor || d.price_minor;
        const final = d.price_minor;
        return {
          days: d.days,
          months: Math.round(d.days / 30),
          priceMinor: base,
          priceLabel: fmt.money(base, currency),
          hasDiscount: final !== base,
          finalMinor: final,
          finalLabel: fmt.money(final, currency),
        };
      }),
    }));

    const ref = referral || {};
    const appCfg = me.app || {};
    return {
      locale: loc,
      currency,
      discountPct: pct,
      // Owner UI toggles, so skins stay consistent with the bot: hide the raw sub URL, hide
      // the traffic panel. Server also nulls subscription_url when hideLink, belt-and-braces.
      hideLink: !!appCfg.hide_subscription_link,
      showTraffic: appCfg.show_traffic_usage !== false,
      balanceEnabled: appCfg.balance_enabled !== false,
      // Top-up bounds (min_deposit_minor from MIN_DEPOSIT_AMOUNT, max_deposit_minor a fixed
      // ceiling) — the actions.topup() prompt validates against these before it ever calls
      // POST /topup; the server re-validates both regardless.
      minDepositMinor: appCfg.min_deposit_minor || 0,
      maxDepositMinor: appCfg.max_deposit_minor || 100000000,
      user: {
        firstName: (me.user && me.user.first_name) || "",
        username: (me.user && me.user.username) || "",
        balanceMinor: (me.user && me.user.balance_minor) || 0,
        balanceLabel: fmt.money((me.user && me.user.balance_minor) || 0, currency),
        referralCode: (me.user && me.user.referral_code) || "",
        isTrialAvailable: !!(me.user && me.user.is_trial_available),
      },
      subscription,
      plans: planItems,
      referral: {
        code: ref.code || "",
        link: ref.link || "",
        commissionPercent: ref.commission_percent || 0,
        invitedCount: ref.invited_count || 0,
        earningsMinor: ref.earnings_minor || 0,
        earningsLabel: fmt.money(ref.earnings_minor || 0, currency),
      },
    };
  }

  async function load() {
    const [me, plans, referral] = await Promise.all([api.getMe(), api.getPlans(), api.getReferral()]);
    resolveLocale(me);
    const model = buildModel(me, plans, referral);
    Cabinet.model = model;
    return model;
  }

  /**
   * Template entry point.
   * @param {(model) => void} render  called with the normalized view-model
   * @param {(err, retry) => void} [onError]
   */
  async function boot({ render, onError }) {
    tg.ready();
    tg.applyThemeVars();
    document.documentElement.lang = i18n.getLocale();
    try {
      const model = await load();
      document.documentElement.lang = model.locale;
      render(model);
    } catch (err) {
      console.error("[cabinet] load failed:", err);
      if (onError) onError(err, () => boot({ render, onError }));
    }
  }

  // ---- user actions (thin wrappers with haptics) ---------------------------
  // Preset RUB top-up amounts offered inline by the lightweight themes' balance card —
  // mirrors TOPUP_PRESETS_RUB in miniapp/app/app.js so both surfaces nudge the same round
  // numbers. Templates filter these against the owner's min/max via topupPresets() below.
  const TOPUP_PRESETS_RUB = [100, 250, 500, 1000];

  const actions = {
    async copyLink(url) {
      const ok = await tg.copy(url);
      tg.haptic(ok ? "success" : "error");
      return ok;
    },
    openApp(cryptoLink, subUrl) {
      const link = cryptoLink || subUrl;
      tg.haptic("light");
      if (link) tg.openDeepLink(link);
    },
    shareReferral(link, text) {
      tg.haptic("light");
      tg.share(link, text);
    },
    async applyPromo(code) {
      tg.haptic("light");
      const r = await api.applyPromo(code);
      tg.haptic(r && r.ok ? "success" : "error");
      return r;
    },
    async purchase(code, days) {
      tg.haptic("light");
      const r = await api.purchase(code, days);
      if (r && r.payment_url) tg.openLink(r.payment_url);
      return r;
    },
    async resetDevices() {
      tg.haptic("warning");
      return api.resetDevices();
    },
    /** Stars-only top-up (these lightweight themes have no gateway picker yet — see the
     * cabinet.py review fixes' report for why). Returns:
     *   {ok:true}                     — invoice paid (Stars) or hosted page opened (gateway)
     *   {ok:false, pending:true}      — invoice opened but not confirmed yet
     *   {ok:false, unsupported:true}  — Telegram client too old for WebApp.openInvoice
     *   {ok:false}                    — request failed (see .error) */
    async topup(amountMinor) {
      tg.haptic("light");
      try {
        const r = await api.topup(amountMinor, "stars");
        if (r && r.redirect_url) {
          tg.openLink(r.redirect_url);
          return { ok: true, pending: true };
        }
        if (r && r.invoice_link) {
          const status = await tg.openInvoice(r.invoice_link);
          if (status === "unsupported") {
            tg.haptic("error");
            return { ok: false, unsupported: true };
          }
          tg.haptic(status === "paid" ? "success" : "error");
          return { ok: status === "paid", pending: status !== "paid" && status !== "failed" };
        }
        // The real /topup endpoint never returns a bare {ok:true} (no invoice_link/redirect_url)
        // — only the mock/preview response above does, on purpose, to demo a "successful" tap.
        if (r && r.ok) {
          tg.haptic("success");
          return { ok: true };
        }
        return { ok: false };
      } catch (err) {
        tg.haptic("error");
        return { ok: false, error: err && err.message };
      }
    },
    /** Preset amounts (minor units), clamped to `model.min/maxDepositMinor` — empty if none
     * of TOPUP_PRESETS_RUB fit inside the owner's bounds (the caller still offers the free
     * `parseTopupAmount` field, so an empty result is not a dead end). */
    topupPresets(model) {
      const minDep = model.minDepositMinor || 0;
      const maxDep = model.maxDepositMinor || 100000000;
      return TOPUP_PRESETS_RUB.map((r) => r * 100).filter((m) => m >= minDep && m <= maxDep);
    },
    /** Validates a raw RUB string (typed into a custom-amount field) against
     * `model.minDepositMinor`/`maxDepositMinor` client-side — the server re-validates both
     * regardless. Returns `{minor}` on success or `{error}` with an already-localized
     * message ready to show. */
    parseTopupAmount(model, raw) {
      const rub = Number(String(raw).trim().replace(",", "."));
      if (!String(raw).trim() || !isFinite(rub) || rub <= 0) {
        return { error: Cabinet.t("topupCustomBad") };
      }
      const minor = Math.round(rub * 100);
      const currency = model.currency || "RUB";
      const minDep = model.minDepositMinor || 0;
      const maxDep = model.maxDepositMinor || 100000000;
      if (minor < minDep) {
        return { error: Cabinet.t("topupBelowMin", { amount: fmt.money(minDep, currency) }) };
      }
      if (minor > maxDep) {
        return { error: Cabinet.t("topupAboveMax", { amount: fmt.money(maxDep, currency) }) };
      }
      return { minor };
    },
    /** Turns a topup() result into a toast-ready `{ok, message}`. `pending` MUST be checked
     * before `ok`: a hosted-gateway redirect comes back as `{ok:true, pending:true}` (opened,
     * not confirmed yet), and checking `.ok` first used to report "Balance topped up" for an
     * unconfirmed payment — unreachable today because this surface hardcodes the Stars
     * method, but the next gateway wired into these lightweight themes would hit it exactly
     * like the mock/preview response already does in `topup()` above. Mirrors the check order
     * already fixed in miniapp/app/app.js's submitTopup (redirect/invoice branches resolved
     * before the bare-`ok` fallback). */
    topupResultMessage(r) {
      if (r.unsupported) return { ok: false, message: Cabinet.t("topupUnsupported") };
      if (r.pending) return { ok: false, message: Cabinet.t("topupPending") };
      if (r.ok) return { ok: true, message: Cabinet.t("topupOk") };
      return { ok: false, message: r.error || Cabinet.t("topupFailed") };
    },
  };

  Cabinet.load = load;
  Cabinet.boot = boot;
  Cabinet.actions = actions;
})();
