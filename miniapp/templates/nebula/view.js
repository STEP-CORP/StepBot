/* Nebula presentation. Cyber-HUD layout over the shared view-model. */
(function () {
  "use strict";
  const { t, fmt } = Cabinet;
  const root = document.getElementById("app");

  function el(tag, props, kids) {
    const n = document.createElement(tag);
    if (props)
      for (const [k, v] of Object.entries(props)) {
        if (k === "class") n.className = v;
        else if (k === "html") n.innerHTML = v;
        else if (k === "text") n.textContent = v;
        else if (k === "style") n.setAttribute("style", v);
        else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
        else if (v != null) n.setAttribute(k, v);
      }
    for (const c of [].concat(kids || [])) if (c != null) n.append(c.nodeType ? c : String(c));
    return n;
  }
  function toast(msg) {
    let tn = document.querySelector(".toast");
    if (!tn) {
      tn = el("div", { class: "toast" });
      document.body.append(tn);
    }
    tn.textContent = "▸ " + msg;
    tn.classList.add("show");
    clearTimeout(tn._t);
    tn._t = setTimeout(() => tn.classList.remove("show"), 1800);
  }
  const pillClass = { active: "ok", trial: "trial", limited: "warn", expired: "bad", disabled: "bad", pending: "warn", none: "warn" };

  function header(m) {
    const initials = (m.user.firstName || "?").trim().charAt(0).toUpperCase();
    const st = m.subscription.status;
    return el("div", { class: "hdr reveal" }, [
      el("div", { class: "avatar", text: initials }),
      el("div", { class: "hdr-txt" }, [
        el("div", { class: "hdr-hi", text: t("greeting", { name: "" }).replace(/[,\s]*$/, "") }),
        el("div", { class: "hdr-name", text: m.user.firstName || m.user.username || "—" }),
      ]),
      el("span", { class: "pill " + (pillClass[st] || "warn"), text: m.subscription.statusLabel }),
    ]);
  }

  function hero(m) {
    const s = m.subscription;
    if (s.status === "none") {
      return el("div", { class: "panel reveal" }, [el("div", { class: "hero-plan", text: t("status.none") }), el("div", { class: "hero-sub", text: t("choosePlan") })]);
    }
    const ring = el("div", { class: "ring", style: `--pct:${s.daysPct}` }, [
      el("div", { class: "ring-hole" }, [
        el("div", { class: "ring-num", text: s.daysLeft == null ? "∞" : s.daysLeft }),
        el("div", { class: "ring-cap", text: s.daysLeft == null ? "" : t("daysLeft") }),
      ]),
    ]);
    const tags = el("div", { class: "tags" }, [
      s.deviceLimit ? el("span", { class: "tag", text: "▪ " + t("devicesValue", { n: s.deviceLimit }) }) : null,
      el("span", { class: "tag", text: "▪ " + s.expireLabel }),
      s.autopay ? el("span", { class: "tag", text: "⟳ auto" }) : null,
    ]);
    return el("div", { class: "panel hero reveal" }, [
      ring,
      el("div", { class: "hero-meta" }, [el("div", { class: "hero-plan", text: s.planName }), el("div", { class: "hero-sub", text: t("expiresOn", { date: s.expireLabel }) }), tags]),
    ]);
  }

  function traffic(m) {
    const s = m.subscription;
    if (s.status === "none") return null;
    const hot = s.trafficPct >= 85;
    return el("div", { class: "panel reveal" }, [
      el("div", { class: "tr-head" }, [
        el("span", { text: t("traffic").toUpperCase() }),
        el("span", { class: "tr-val", text: s.unlimited ? t("unlimited") : `${s.trafficUsedLabel} / ${s.trafficLimitLabel}` }),
      ]),
      el("div", { class: "bar" + (hot ? " hot" : "") }, [el("i", { style: `width:${s.unlimited ? 8 : s.trafficPct}%` })]),
    ]);
  }

  function connect(m) {
    const s = m.subscription;
    if (s.status === "none" || !s.subscriptionUrl) return null;
    return el("div", { class: "panel reveal" }, [
      el("div", { class: "panel-title", text: t("connect") }),
      el("button", { class: "btn", onclick: () => Cabinet.actions.openApp(s.cryptoLink, s.subscriptionUrl), text: "⚡ " + t("openInApp") }),
      el("div", { class: "btn-row" }, [
        el(
          "button",
          {
            class: "btn ghost",
            onclick: async () => {
              const ok = await Cabinet.actions.copyLink(s.subscriptionUrl);
              toast(ok ? t("copied") : t("error"));
            },
            text: "❯ " + t("copyLink"),
          }
        ),
      ]),
    ]);
  }

  // Inline preset chips + a free-typed custom amount, both feeding Cabinet.actions.topup().
  // A real in-page field, not window.prompt() — Telegram's in-app WebView doesn't implement
  // it (many clients resolve it to null immediately), so the old "＋ Пополнить" button used
  // to do nothing at all.
  function topupComposer(m) {
    const presets = Cabinet.actions.topupPresets(m);
    const sel = { i: 0 };
    const input = el("input", { class: "inp", type: "text", inputmode: "decimal", placeholder: t("topupPrompt") });
    const note = el("div", { class: "note" });
    const submitBtn = el("button", { class: "btn sm", text: t("topUp") });
    const chipEls = presets.map((minor, i) =>
      el(
        "div",
        {
          class: "dur",
          onclick: () => {
            sel.i = i;
            input.value = "";
            syncChips();
          },
        },
        [el("div", { class: "dur-p", text: fmt.money(minor, m.currency) })]
      )
    );
    function syncChips() {
      chipEls.forEach((c, i) => c.classList.toggle("on", i === sel.i && !input.value.trim()));
    }
    syncChips();
    input.addEventListener("input", syncChips);
    submitBtn.addEventListener("click", async () => {
      const raw = input.value.trim() || (presets.length ? String(presets[sel.i] / 100) : "");
      const parsed = Cabinet.actions.parseTopupAmount(m, raw);
      if (parsed.error) {
        note.className = "note bad";
        note.textContent = parsed.error;
        return;
      }
      submitBtn.disabled = true;
      const r = await Cabinet.actions.topup(parsed.minor);
      submitBtn.disabled = false;
      const res = Cabinet.actions.topupResultMessage(r);
      note.className = "note " + (res.ok ? "ok" : "bad");
      note.textContent = res.message;
      if (res.ok) render(await Cabinet.load());
    });
    return el("div", { style: "margin-top:12px" }, [
      presets.length ? el("div", { class: "durs", style: "margin:0 0 10px" }, chipEls) : null,
      el("div", { class: "promo-row" }, [input, submitBtn]),
      note,
    ]);
  }

  function balance(m) {
    const header = el("div", { class: "bal" }, [
      el("div", {}, [el("div", { class: "panel-title", style: "margin:0 0 6px", text: t("balance") }), el("div", { class: "bal-amount", text: m.user.balanceLabel })]),
    ]);
    // #2: the owner turned the wallet off (BALANCE_ENABLED=false) — no top-up field, not just
    // a hidden button, since /topup would 400 on every submit anyway.
    const kids = m.balanceEnabled ? [header, topupComposer(m)] : [header];
    return el("div", { class: "panel reveal" }, kids);
  }

  function planCard(m, p) {
    const sel = { i: p.durations.length ? p.durations.length - 1 : 0 };
    const cta = el("button", { class: "btn", style: "margin-top:12px" });
    const durEls = p.durations.map((d, i) =>
      el(
        "div",
        {
          class: "dur" + (i === sel.i ? " on" : ""),
          onclick: () => {
            sel.i = i;
            durEls.forEach((n, idx) => n.classList.toggle("on", idx === sel.i));
            draw();
          },
        },
        [
          el("div", { class: "dur-d", text: d.months + " " + Cabinet.i18n.plural(d.months, "months") }),
          el("div", { class: "dur-p", text: d.finalLabel }),
          d.hasDiscount ? el("div", { class: "dur-old", text: d.priceLabel }) : null,
        ]
      )
    );
    function draw() {
      const d = p.durations[sel.i];
      cta.textContent = (p.isCurrent ? t("renew") : t("buy")) + (d ? " · " + d.finalLabel : "");
    }
    cta.addEventListener("click", async () => {
      const d = p.durations[sel.i];
      const r = await Cabinet.actions.purchase(p.code, d ? d.days : null);
      toast(r && (r.ok || r.payment_url) ? t("buy") + " ✓" : t("error"));
    });
    draw();
    return el("div", { class: "plan" + (p.isCurrent ? " cur" : "") }, [
      el("div", { class: "plan-top" }, [
        el("div", {}, [el("div", { class: "plan-name", text: p.name }), el("div", { class: "plan-desc", text: p.description })]),
        p.isCurrent ? el("span", { class: "plan-badge", text: t("current") }) : null,
      ]),
      el("div", { class: "durs" }, durEls),
      cta,
    ]);
  }

  function plans(m) {
    if (!m.plans.length) return null;
    return el("div", { class: "panel reveal" }, [el("div", { class: "panel-title", text: t("plans") }), el("div", { class: "plans" }, m.plans.map((p) => planCard(m, p)))]);
  }

  function referral(m) {
    const r = m.referral;
    if (!r.code) return null;
    return el("div", { class: "panel reveal" }, [
      el("div", { class: "panel-title", text: t("referral") }),
      el("div", { class: "hero-sub", style: "margin-bottom:12px", text: t("referralHint", { pct: r.commissionPercent }) }),
      el("div", { class: "ref-stats" }, [
        el("div", { class: "stat" }, [el("div", { class: "stat-n", text: r.invitedCount }), el("div", { class: "stat-l", text: t("invited") })]),
        el("div", { class: "stat" }, [el("div", { class: "stat-n", text: r.earningsLabel }), el("div", { class: "stat-l", text: t("earned") })]),
      ]),
      el("div", { class: "btn-row", style: "margin-top:0" }, [
        el("button", { class: "btn", onclick: () => Cabinet.actions.shareReferral(r.link, t("referralHint", { pct: r.commissionPercent })), text: "🎁 " + t("share") }),
        el("button", { class: "btn ghost sm", style: "flex:0 0 auto", onclick: async () => { await Cabinet.actions.copyLink(r.link); toast(t("copied")); }, text: "❯" }),
      ]),
    ]);
  }

  function promo() {
    const input = el("input", { class: "inp", placeholder: t("promoPlaceholder"), maxlength: "32" });
    const note = el("div", { class: "note" });
    const btn = el("button", {
      class: "btn sm",
      onclick: async () => {
        const code = input.value.trim();
        if (!code) return;
        btn.disabled = true;
        const r = await Cabinet.actions.applyPromo(code);
        btn.disabled = false;
        note.className = "note " + (r && r.ok ? "ok" : "bad");
        note.textContent = (r && r.ok ? "✓ " : "✕ ") + (r && r.ok ? t("promoOk") : t("promoBad"));
        if (r && r.ok) input.value = "";
      },
      text: t("apply"),
    });
    return el("div", { class: "panel reveal" }, [el("div", { class: "panel-title", text: t("promo") }), el("div", { class: "promo-row" }, [input, btn]), note]);
  }

  function render(m) {
    root.setAttribute("aria-busy", "false");
    root.innerHTML = "";
    [header(m), hero(m), traffic(m), connect(m), balance(m), plans(m), referral(m), promo(m)].filter(Boolean).forEach((n) => root.append(n));
  }
  function loading() {
    root.innerHTML = "";
    root.append(el("div", { class: "center" }, [el("div", { class: "spinner" }), el("div", { text: t("loading").toUpperCase() })]));
  }
  function errorState(retry) {
    root.innerHTML = "";
    root.append(el("div", { class: "center" }, [el("div", { style: "font-size:32px", text: "⚠" }), el("div", { text: t("error") }), el("button", { class: "btn sm", onclick: retry, text: t("retry") })]));
  }

  loading();
  Cabinet.boot({ render, onError: (_e, retry) => errorState(retry) });
})();
