import { defineConfig } from "vitepress";

// Кастомный домен (docs.vpn-hub.pro) собирается с base "/";
// GitHub Pages задаёт DOCS_BASE=/StepBot/ в workflow.
const BASE = process.env.DOCS_BASE || "/";

export default defineConfig({
  lang: "ru-RU",
  title: "VPN-HUB BOT",
  description:
    "Документация конструктора Telegram-ботов для продажи VPN на базе Remnawave",
  base: BASE,
  head: [["link", { rel: "icon", type: "image/png", href: `${BASE}logo.png` }]],
  sitemap: { hostname: "https://docs.vpn-hub.pro" },
  themeConfig: {
    logo: "/logo.png",
    search: { provider: "local" },
    outline: { label: "На этой странице", level: [2, 3] },
    docFooter: { prev: "Назад", next: "Дальше" },
    lastUpdated: { text: "Обновлено" },
    darkModeSwitchLabel: "Тема",
    sidebarMenuLabel: "Меню",
    returnToTopLabel: "Наверх",
    socialLinks: [
      { icon: "github", link: "https://github.com/STEP-CORP/StepBot" },
      { icon: "telegram", link: "https://t.me/vpnhub_community" },
    ],
    nav: [
      { text: "Установка", link: "/guide/install" },
      { text: "Платежи", link: "/payments/" },
      { text: "Кабинет", link: "/cabinet/" },
      { text: "Демо", link: "https://testbot.tvss-911.com/admin/" },
    ],
    sidebar: [
      {
        text: "Начало",
        collapsed: false,
        items: [
          { text: "Что это", link: "/guide/what-is" },
          { text: "Установка одной командой", link: "/guide/install" },
          { text: "Обновление", link: "/guide/update" },
          { text: "Деплой на VPS (systemd)", link: "/guide/deploy-vps" },
          { text: "Чеклист запуска продаж", link: "/guide/go-live" },
          { text: "Переезд с другого бота", link: "/guide/migration" },
        ],
      },
      {
        text: "Бот",
        collapsed: false,
        items: [
          { text: "Возможности", link: "/bot/" },
          { text: "Админка в боте", link: "/bot/admin-panel" },
          { text: "Конструктор меню", link: "/bot/menu-builder" },
          { text: "Покупка, триал, смена тарифа", link: "/bot/purchase" },
          { text: "Рефералка и выводы", link: "/bot/referral" },
          { text: "Промокоды и gift-коды", link: "/bot/promo" },
          { text: "Тикеты", link: "/bot/tickets" },
          { text: "Устройства (HWID)", link: "/bot/devices" },
          { text: "Гейт подписки на каналы", link: "/bot/channel-gate" },
        ],
      },
      {
        text: "Админ-кабинет",
        collapsed: false,
        items: [
          { text: "Обзор", link: "/cabinet/" },
          { text: "Пользователи", link: "/cabinet/users" },
          { text: "Тарифы и цены", link: "/cabinet/tariffs" },
          { text: "Промо и акции", link: "/cabinet/promos" },
          { text: "Рассылки", link: "/cabinet/broadcasts" },
          { text: "Умные напоминания", link: "/cabinet/reminders" },
          { text: "Рекламные кампании", link: "/cabinet/campaigns" },
          { text: "Серверы", link: "/cabinet/servers" },
          { text: "Настройки", link: "/cabinet/settings" },
          { text: "Обслуживание", link: "/cabinet/maintenance" },
        ],
      },
      {
        text: "Mini App",
        collapsed: true,
        items: [
          { text: "Обзор и темы", link: "/miniapp/" },
          { text: "Настройка владельцем", link: "/miniapp/configuration" },
          { text: "Контракт cabinet API", link: "/miniapp/contract" },
        ],
      },
      {
        text: "Платежи",
        collapsed: false,
        items: [
          { text: "Как устроены платежи", link: "/payments/" },
          { text: "Подключение провайдера", link: "/payments/settings" },
          { text: "Вебхуки и безопасность", link: "/payments/webhooks" },
          { text: "Возвраты", link: "/payments/refunds" },
          {
            text: "Провайдеры",
            collapsed: true,
            items: [
              { text: "Telegram Stars", link: "/payments/providers/telegram-stars" },
              { text: "Manual / баланс", link: "/payments/providers/manual" },
              { text: "YooKassa", link: "/payments/providers/yookassa" },
              { text: "ЮMoney", link: "/payments/providers/yoomoney" },
              { text: "Robokassa", link: "/payments/providers/robokassa" },
              { text: "Platega", link: "/payments/providers/platega" },
              { text: "WATA", link: "/payments/providers/wata" },
              { text: "CryptoBot", link: "/payments/providers/cryptobot" },
              { text: "Cryptomus", link: "/payments/providers/cryptomus" },
              { text: "Heleket", link: "/payments/providers/heleket" },
              { text: "FreeKassa", link: "/payments/providers/freekassa" },
              { text: "KassaAI", link: "/payments/providers/kassa-ai" },
              { text: "PayPalych", link: "/payments/providers/paypalych" },
              { text: "MulenPay", link: "/payments/providers/mulenpay" },
              { text: "CloudPayments", link: "/payments/providers/cloudpayments" },
              { text: "Lava", link: "/payments/providers/lava" },
              { text: "RollyPay", link: "/payments/providers/rollypay" },
              { text: "Antilopay", link: "/payments/providers/antilopay" },
              { text: "RioPay", link: "/payments/providers/riopay" },
              { text: "SeverPay", link: "/payments/providers/severpay" },
              { text: "AuraPay", link: "/payments/providers/aurapay" },
            ],
          },
        ],
      },
      {
        text: "Панель Remnawave",
        collapsed: true,
        items: [
          { text: "Подключение", link: "/panel/" },
          { text: "Синхронизация", link: "/panel/sync" },
          { text: "Вебхук панели", link: "/panel/webhook" },
          { text: "Авто-техрежим (watchdog)", link: "/panel/watchdog" },
          { text: "Мок-панель", link: "/panel/mock" },
        ],
      },
      {
        text: "Фичи",
        collapsed: true,
        items: [
          { text: "ИИ-поддержка", link: "/features/ai-support" },
          { text: "Device Guard", link: "/features/device-guard" },
          { text: "Продажи с сайта", link: "/features/site-sales" },
          { text: "Бэкапы", link: "/features/backups" },
          { text: "Телеметрия ошибок", link: "/features/telemetry" },
        ],
      },
      {
        text: "Справочник",
        collapsed: false,
        items: [{ text: "Коды ошибок", link: "/reference/error-codes" }],
      },
      {
        text: "Разработчикам",
        collapsed: true,
        items: [
          { text: "Архитектура", link: "/dev/architecture" },
          { text: "Добавить платёжку", link: "/dev/add-payment-gateway" },
          { text: "Добавить модель БД", link: "/dev/add-db-model" },
          { text: "Контрибьютинг", link: "/dev/contributing" },
        ],
      },
    ],
    footer: {
      message: "MIT License · сделано для тех, кто продаёт VPN, а не настраивает ботов",
    },
  },
});
