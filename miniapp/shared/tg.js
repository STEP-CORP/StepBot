/**
 * Telegram WebApp glue, with a graceful fallback for plain-browser preview.
 *
 * When running inside Telegram, this exposes theme params, haptics, the native
 * BackButton/MainButton, share/link helpers and the raw initData (sent to the API as
 * `Authorization: tma <initData>`). Outside Telegram every call is a safe no-op so the
 * templates still render standalone.
 */
(function () {
  "use strict";
  const Cabinet = (window.Cabinet = window.Cabinet || {});
  const wa = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

  const inside = !!(wa && wa.initData);

  function ready() {
    if (!wa) return;
    try {
      wa.ready();
      wa.expand();
    } catch (_) {}
  }

  /** Push Telegram theme params onto :root as --tg-theme-* CSS variables. */
  function applyThemeVars() {
    if (!wa || !wa.themeParams) return;
    const root = document.documentElement;
    for (const [k, v] of Object.entries(wa.themeParams)) {
      root.style.setProperty(`--tg-theme-${k.replace(/_/g, "-")}`, v);
    }
    root.dataset.tgColorScheme = wa.colorScheme || "light";
    if (typeof wa.onEvent === "function") {
      wa.onEvent("themeChanged", () => applyThemeVars());
    }
  }

  function haptic(kind = "light") {
    if (!wa || !wa.HapticFeedback) return;
    try {
      if (kind === "success" || kind === "error" || kind === "warning") {
        wa.HapticFeedback.notificationOccurred(kind);
      } else {
        wa.HapticFeedback.impactOccurred(kind);
      }
    } catch (_) {}
  }

  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      // Fallback for insecure contexts.
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (_) {}
      document.body.removeChild(ta);
      return ok;
    }
  }

  function openLink(url, opts) {
    if (wa && typeof wa.openLink === "function") {
      wa.openLink(url, opts);
    } else {
      window.open(url, "_blank", "noopener");
    }
  }

  /** Open a Telegram Stars invoice link. Resolves "unsupported" on an old client without
   * WebApp.openInvoice — the caller must NOT treat that as a paid/failed status. */
  function openInvoice(link) {
    return new Promise((resolve) => {
      if (wa && typeof wa.openInvoice === "function") {
        wa.openInvoice(link, (status) => resolve(status));
      } else {
        resolve("unsupported");
      }
    });
  }

  /** Open a tg://, happ:// or other deep link (subscription import). */
  function openDeepLink(url) {
    if (wa && url.startsWith("https://t.me") && typeof wa.openTelegramLink === "function") {
      wa.openTelegramLink(url);
    } else {
      window.location.href = url;
    }
  }

  function share(url, text) {
    const tgShare = `https://t.me/share/url?url=${encodeURIComponent(url)}&text=${encodeURIComponent(text || "")}`;
    if (wa && typeof wa.openTelegramLink === "function") wa.openTelegramLink(tgShare);
    else window.open(tgShare, "_blank", "noopener");
  }

  function initDataAuthHeader() {
    return inside ? `tma ${wa.initData}` : null;
  }

  function userLanguage() {
    try {
      return (wa && wa.initDataUnsafe && wa.initDataUnsafe.user && wa.initDataUnsafe.user.language_code) || null;
    } catch (_) {
      return null;
    }
  }

  Cabinet.tg = {
    raw: wa,
    inside,
    ready,
    applyThemeVars,
    haptic,
    copy,
    openLink,
    openInvoice,
    openDeepLink,
    share,
    initDataAuthHeader,
    userLanguage,
  };
})();
