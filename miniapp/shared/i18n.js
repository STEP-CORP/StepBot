/**
 * Tiny i18n. RU/EN only (mirrors core/enums.py::Locale). Templates call Cabinet.t("key").
 * Locale resolution: ?lang= → Telegram user language → mock/user payload → default RU.
 *
 * Keys use {placeholders}. Add strings here; keep both locales in sync.
 */
(function () {
  "use strict";
  const Cabinet = (window.Cabinet = window.Cabinet || {});

  const DICT = {
    ru: {
      greeting: "Привет, {name}",
      // statuses
      "status.active": "Активна",
      "status.trial": "Пробный период",
      "status.limited": "Лимит трафика",
      "status.expired": "Истекла",
      "status.disabled": "Отключена",
      "status.pending": "Ожидает оплаты",
      "status.none": "Нет подписки",
      // subscription card
      daysLeft: "осталось дней",
      day_one: "день",
      day_few: "дня",
      day_many: "дней",
      expiresOn: "Действует до {date}",
      traffic: "Трафик",
      trafficUsed: "{used} из {limit}",
      unlimited: "Безлимит",
      devices: "Устройства",
      devicesValue: "до {n}",
      plan: "Тариф",
      // connect
      connect: "Подключиться",
      copyLink: "Скопировать ссылку",
      openInApp: "Открыть в приложении",
      copied: "Скопировано",
      // balance
      balance: "Баланс",
      topUp: "Пополнить",
      topupPrompt: "Сумма пополнения, ₽",
      topupCustomBad: "Введите сумму числом",
      topupBelowMin: "Минимум — {amount}",
      topupAboveMax: "Максимум — {amount}",
      topupPending: "Открой счёт и заверши оплату",
      topupOk: "Баланс пополнен",
      topupFailed: "Не удалось пополнить баланс",
      topupUnsupported: "Обнови Telegram или выбери другой способ оплаты",
      // plans
      plans: "Тарифы",
      choosePlan: "Выберите тариф",
      buy: "Купить",
      renew: "Продлить",
      current: "Текущий",
      perDays: "на {n} {unit}",
      discount: "−{pct}%",
      months_one: "мес",
      months_few: "мес",
      months_many: "мес",
      // referral
      referral: "Рефералы",
      referralHint: "Приглашайте друзей — получайте {pct}% с их пополнений",
      invited: "Приглашено",
      earned: "Заработано",
      yourLink: "Ваша ссылка",
      share: "Поделиться",
      // promo
      promo: "Промокод",
      promoPlaceholder: "Введите код",
      apply: "Применить",
      promoOk: "Промокод применён",
      promoBad: "Неверный или использованный код",
      // misc
      loading: "Загрузка…",
      error: "Не удалось загрузить данные",
      retry: "Повторить",
      manage: "Управление",
      resetDevices: "Сбросить устройства",
      autopayOn: "Автоплатёж включён",
    },
    en: {
      greeting: "Hi, {name}",
      "status.active": "Active",
      "status.trial": "Trial",
      "status.limited": "Traffic limit",
      "status.expired": "Expired",
      "status.disabled": "Disabled",
      "status.pending": "Awaiting payment",
      "status.none": "No subscription",
      daysLeft: "days left",
      day_one: "day",
      day_few: "days",
      day_many: "days",
      expiresOn: "Valid until {date}",
      traffic: "Traffic",
      trafficUsed: "{used} of {limit}",
      unlimited: "Unlimited",
      devices: "Devices",
      devicesValue: "up to {n}",
      plan: "Plan",
      connect: "Connect",
      copyLink: "Copy link",
      openInApp: "Open in app",
      copied: "Copied",
      balance: "Balance",
      topUp: "Top up",
      topupPrompt: "Top-up amount, ₽",
      topupCustomBad: "Enter a valid amount",
      topupBelowMin: "Minimum — {amount}",
      topupAboveMax: "Maximum — {amount}",
      topupPending: "Open the invoice and complete the payment",
      topupOk: "Balance topped up",
      topupFailed: "Failed to top up the balance",
      topupUnsupported: "Update Telegram or choose another payment method",
      plans: "Plans",
      choosePlan: "Choose a plan",
      buy: "Buy",
      renew: "Renew",
      current: "Current",
      perDays: "for {n} {unit}",
      discount: "−{pct}%",
      months_one: "mo",
      months_few: "mo",
      months_many: "mo",
      referral: "Referrals",
      referralHint: "Invite friends — earn {pct}% of their top-ups",
      invited: "Invited",
      earned: "Earned",
      yourLink: "Your link",
      share: "Share",
      promo: "Promo code",
      promoPlaceholder: "Enter code",
      apply: "Apply",
      promoOk: "Promo code applied",
      promoBad: "Invalid or used code",
      loading: "Loading…",
      error: "Failed to load data",
      retry: "Retry",
      manage: "Manage",
      resetDevices: "Reset devices",
      autopayOn: "Autopay is on",
    },
  };

  let current = "ru";

  function setLocale(loc) {
    current = DICT[loc] ? loc : "ru";
    return current;
  }
  function getLocale() {
    return current;
  }

  function interpolate(str, params) {
    if (!params) return str;
    return str.replace(/\{(\w+)\}/g, (_, k) => (params[k] != null ? params[k] : `{${k}}`));
  }

  function t(key, params) {
    const table = DICT[current] || DICT.ru;
    const raw = table[key] != null ? table[key] : DICT.ru[key] != null ? DICT.ru[key] : key;
    return interpolate(raw, params);
  }

  /** Russian-style plural picker (one/few/many); EN collapses to one/many. */
  function plural(n, base) {
    n = Math.abs(Number(n) || 0);
    if (current === "ru") {
      const mod10 = n % 10;
      const mod100 = n % 100;
      if (mod10 === 1 && mod100 !== 11) return t(base + "_one");
      if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return t(base + "_few");
      return t(base + "_many");
    }
    return t(base + (n === 1 ? "_one" : "_many"));
  }

  Cabinet.i18n = { setLocale, getLocale, plural, DICT };
  Cabinet.t = t;
})();
