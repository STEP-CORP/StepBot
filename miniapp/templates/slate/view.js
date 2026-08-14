/* Slate presentation. Native Telegram grouped-list layout over the shared view-model. */
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
    tn.textContent = msg;
    tn.classList.add("show");
    clearTimeout(tn._t);
    tn._t = setTimeout(() => tn.classList.remove("show"), 1700);
  }
  const group = (title, section) => el("div", { class: "group" }, [title ? el("div", { class: "group-title", text: title }) : null, section]);
  const row = (kids, cls) => el("div", { class: "row" + (cls ? " " + cls : "") }, kids);

  const dotClass = { active: "ok", trial: "trial", limited: "warn", expired: "bad", disabled: "bad", pending: "warn", none: "warn" };

  function header(m) {
    const initials = (m.user.firstName || "?").trim().charAt(0).toUpperCase();
    const st = m.subscription.status;
    return el("div", { class: "hdr" }, [
      el("div", { class: "avatar", text: initials }),
      el("div", { class: "hdr-txt" }, [
        el("div", { class: "hdr-hi", text: t("greeting", { name: m.user.firstName || m.user.username || "" }) }),
        el("span", { class: "dot " + (dotClass[st] || "warn"), text: m.subscription.statusLabel }),
      ]),
    ]);
  }

  function status(m) {
    const s = m.subscription;
    if (s.status === "none") {
      return group(null, el("div", { class: "section" }, [el("div", { class: "status-head" }, [el("div", { class: "status-cap", text: t("choosePlan") })])]));
    }
    const head = el("div", { class: "status-head" }, [
      el("div", { class: "status-days", text: s.daysLeft == null ? "∞" : s.daysLeft }),
      el("div", { class: "status-cap", text: s.daysLeft == null ? t("status." + s.status) : t("daysLeft") }),
      el("div", { class: "status-plan" }, [el("span", { text: s.planName }), el("span", { text: "·" }), el("span", { text: s.expireLabel })]),
      el("div", { class: "dbar" }, [el("i", { style: `width:${s.daysPct}%` })]),
    ]);
    return group(null, el("div", { class: "section" }, [head]));
  }

  function details(m) {
    const s = m.subscription;
    if (s.status === "none") return null;
    const trafficRow = el("div", { class: "row", style: "flex-direction:column;align-items:stretch;gap:7px" }, [
      el("div", { style: "display:flex;justify-content:space-between;align-items:baseline" }, [
        el("span", { class: "row-title", text: t("traffic") }),
        el("span", { class: "row-value strong", text: s.unlimited ? t("unlimited") : t("trafficUsed", { used: s.trafficUsedLabel, limit: s.trafficLimitLabel }) }),
      ]),
      s.unlimited ? null : el("div", { class: "mini-bar" + (s.trafficPct >= 85 ? " hot" : "") }, [el("i", { style: `width:${s.trafficPct}%` })]),
    ]);
    const rows = [
      row([el("div", { class: "row-label" }, [el("div", { class: "row-title", text: t("plan") })]), el("div", { class: "row-value strong", text: s.planName })]),
      s.deviceLimit ? row([el("div", { class: "row-label" }, [el("div", { class: "row-title", text: t("devices") })]), el("div", { class: "row-value strong", text: t("devicesValue", { n: s.deviceLimit }) })]) : null,
      trafficRow,
      row([el("div", { class: "row-label" }, [el("div", { class: "row-title", text: t("expiresOn", { date: "" }).replace(/\s+$/, "") })]), el("div", { class: "row-value strong", text: s.expireLabel })]),
    ].filter(Boolean);
    return group(null, el("div", { class: "section" }, rows));
  }

  function connect(m) {
    const s = m.subscription;
    if (s.status === "none" || !s.subscriptionUrl) return null;
    return group(
      t("connect"),
      el("div", { class: "section" }, [
        el("div", { class: "stack" }, [
          el("button", { class: "btn", onclick: () => Cabinet.actions.openApp(s.cryptoLink, s.subscriptionUrl), text: t("openInApp") }),
          el(
            "button",
            {
              class: "btn ghost",
              onclick: async () => {
                const ok = await Cabinet.actions.copyLink(s.subscriptionUrl);
                toast(ok ? t("copied") : t("error"));
              },
              text: t("copyLink"),
            }
          ),
        ]),
      ])
    );
  }

  // Inline preset segments + a free-typed custom amount, both feeding Cabinet.actions.topup().
  // A real in-page field, not window.prompt() — Telegram's in-app WebView doesn't implement
  // it (many clients resolve it to null immediately), so the old "Пополнить" button used to
  // do nothing at all.
  function topupComposer(m) {
    const presets = Cabinet.actions.topupPresets(m);
    const sel = { i: 0 };
    const input = el("input", { class: "inp", type: "text", inputmode: "decimal", placeholder: t("topupPrompt") });
    const note = el("div", { class: "note" });
    const submitBtn = el("button", { class: "btn link", style: "width:auto", text: t("topUp") });
    const seg = presets.length
      ? el(
          "div",
          { class: "seg", style: "margin:0 16px 12px" },
          presets.map((minor, i) =>
            el("button", {
              class: i === sel.i ? "on" : "",
              onclick: () => {
                sel.i = i;
                input.value = "";
                syncSeg();
              },
              text: fmt.money(minor, m.currency),
            })
          )
        )
      : null;
    function syncSeg() {
      if (!seg) return;
      Array.from(seg.children).forEach((c, i) => c.classList.toggle("on", i === sel.i && !input.value.trim()));
    }
    input.addEventListener("input", syncSeg);
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
    return [seg, row([input, submitBtn]), note].filter(Boolean);
  }

  function balance(m) {
    const items = [
      row([el("div", { class: "row-label" }, [el("div", { class: "row-title", text: m.user.balanceLabel, style: "font-weight:700;font-size:22px" })])]),
    ];
    // #2: the owner turned the wallet off (BALANCE_ENABLED=false) — no top-up field, not just
    // a hidden button, since /topup would 400 on every submit anyway.
    if (m.balanceEnabled) items.push(...topupComposer(m));
    return group(t("balance"), el("div", { class: "section" }, items));
  }

  function planBlock(m, p) {
    const state = { i: p.durations.length - 1 };
    const seg = el(
      "div",
      { class: "seg" },
      p.durations.map((d, i) =>
        el("button", { class: i === state.i ? "on" : "", onclick: () => pick(i), text: t("perDays", { n: d.months, unit: Cabinet.i18n.plural(d.months, "months") }).replace(/^на\s/, "").replace(/^for\s/, "") })
      )
    );
    const priceEl = el("div", { class: "plan-price" });
    const buyBtn = el("button", { class: "btn", onclick: buy });

    function pick(i) {
      state.i = i;
      Array.from(seg.children).forEach((c, idx) => c.classList.toggle("on", idx === state.i));
      draw();
    }
    function draw() {
      const d = p.durations[state.i];
      priceEl.innerHTML = "";
      priceEl.append(d.finalLabel);
      if (d.hasDiscount) priceEl.append(el("s", { text: d.priceLabel }));
      buyBtn.textContent = p.isCurrent ? t("renew") : t("buy");
    }
    async function buy() {
      const d = p.durations[state.i];
      const r = await Cabinet.actions.purchase(p.code, d.days);
      toast(r && (r.ok || r.payment_url) ? t("buy") + " ✓" : t("error"));
    }
    draw();

    return el("div", { class: "plan" }, [
      el("div", { class: "plan-head" }, [el("div", { class: "plan-name", text: p.name }), p.isCurrent ? el("span", { class: "plan-cur", text: t("current") }) : null]),
      el("div", { class: "plan-desc", text: p.description }),
      seg,
      el("div", { class: "plan-buy" }, [priceEl, buyBtn]),
    ]);
  }

  function plans(m) {
    if (!m.plans.length) return null;
    return group(t("plans"), el("div", { class: "section" }, m.plans.map((p) => planBlock(m, p))));
  }

  function referral(m) {
    const r = m.referral;
    if (!r.code) return null;
    return group(
      t("referral"),
      el("div", { class: "section" }, [
        row([el("div", { class: "row-label" }, [el("div", { class: "row-title", text: t("invited") })]), el("div", { class: "row-value strong", text: r.invitedCount })]),
        row([el("div", { class: "row-label" }, [el("div", { class: "row-title", text: t("earned") }), el("div", { class: "row-sub", text: t("referralHint", { pct: r.commissionPercent }) })]), el("div", { class: "row-value strong", text: r.earningsLabel })]),
        el("div", { class: "stack" }, [
          el("button", { class: "btn", onclick: () => Cabinet.actions.shareReferral(r.link, t("referralHint", { pct: r.commissionPercent })), text: t("share") }),
          el("button", { class: "btn ghost", onclick: async () => { await Cabinet.actions.copyLink(r.link); toast(t("copied")); }, text: t("copyLink") }),
        ]),
      ])
    );
  }

  function promo() {
    const input = el("input", { class: "inp", placeholder: t("promoPlaceholder"), maxlength: "32" });
    const note = el("div", { class: "note" });
    const applyBtn = el("button", {
      class: "btn link",
      style: "width:auto",
      onclick: async () => {
        const code = input.value.trim();
        if (!code) return;
        applyBtn.disabled = true;
        const r = await Cabinet.actions.applyPromo(code);
        applyBtn.disabled = false;
        note.className = "note " + (r && r.ok ? "ok" : "bad");
        note.textContent = r && r.ok ? t("promoOk") : t("promoBad");
        if (r && r.ok) input.value = "";
      },
      text: t("apply"),
    });
    return group(t("promo"), el("div", { class: "section" }, [row([input, applyBtn]), note]));
  }

  function render(m) {
    root.setAttribute("aria-busy", "false");
    root.innerHTML = "";
    [header(m), status(m), details(m), connect(m), balance(m), plans(m), referral(m), promo(m)].filter(Boolean).forEach((n) => root.append(n));
  }
  function loading() {
    root.innerHTML = "";
    root.append(el("div", { class: "center" }, [el("div", { class: "spinner" }), el("div", { text: t("loading") })]));
  }
  function errorState(retry) {
    root.innerHTML = "";
    root.append(el("div", { class: "center" }, [el("div", { style: "font-size:32px", text: "😕" }), el("div", { text: t("error") }), el("button", { class: "btn", style: "width:auto;padding:10px 22px", onclick: retry, text: t("retry") })]));
  }

  loading();
  Cabinet.boot({ render, onError: (_e, retry) => errorState(retry) });
})();
