/* Thin fetch wrapper: bearer token, JSON, 401 -> logout redirect. */

const TOKEN_KEY = "admin_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(humanError(status, detail));
    this.status = status;
    this.detail = detail;
  }
}

/* Map raw API details to admin-friendly text (RU/EN). Unknown details fall back
   to a generic per-status message with the raw detail appended for debugging. */
const ERR: Record<string, [string, string]> = {
  "demo is read-only": ["Демо-режим: только просмотр, изменения запрещены", "Demo mode is read-only"],
  "invalid credentials": ["Неверный логин или пароль", "Invalid username or password"],
  unauthorized: ["Сессия истекла — войди заново", "Session expired — sign in again"],
  "admin access revoked": ["Доступ отозван — войди заново", "Access revoked — sign in again"],
  "audience is empty": ["В выбранной аудитории нет пользователей", "The selected audience has no users"],
  "menu tree contains a cycle": ["В дереве меню зацикленная вложенность — проверь родителей кнопок", "The menu tree has a cycle — check button parents"],
  "code already exists": ["Такой промокод уже существует — сгенерируй другой", "This promo code already exists"],
  "start_param already in use": ["Эта UTM-метка уже занята другой кампанией", "This start param is already used"],
  "plan has subscriptions": ["У тарифа есть активные подписки — выключи его вместо удаления", "This plan has active subscriptions — deactivate it instead"],
  "promo group is bound to": ["Группу используют активные промокоды или кампании — сначала отвяжи или выключи их", "This group is used by active promocodes or campaigns — unbind or deactivate them first"],
  "promo group not found": ["Такой промогруппы не существует", "This promo group doesn't exist"],
  "insufficient balance": ["Недостаточно средств на балансе", "Insufficient balance"],
  "user has no subscription": ["У пользователя нет активной подписки", "The user has no active subscription"],
  "cannot block a staff account": ["Нельзя заблокировать администратора", "Staff accounts can't be blocked"],
  "unsupported file type": ["Формат файла не поддерживается (jpg, png, webp, gif, mp4)", "Unsupported file type (jpg, png, webp, gif, mp4)"],
  "file too large": ["Файл слишком большой — максимум 20 МБ", "File too large — 20 MB max"],
  "panel error": ["Панель Remnawave вернула ошибку — проверь адрес и токен в настройках", "The Remnawave panel returned an error — check its URL and token"],
  "panel sync failed": ["Не удалось синхронизироваться с панелью Remnawave", "Remnawave panel sync failed"],
  "media broadcast requires": ["Сначала загрузи файл для медиа-рассылки", "Upload a media file first"],
  "button needs a url or an action": ["У кнопки должна быть ссылка или действие", "The button needs a URL or an action"],
  "no changes": ["Нет изменений для сохранения", "Nothing to save"],
  "unknown config key": ["Неизвестный параметр настроек — обнови страницу", "Unknown settings key — refresh the page"],
  "demo mode disabled": ["Демо-режим выключен", "Demo mode is disabled"],
};

function humanError(status: number, detail: string): string {
  const ru = (localStorage.getItem("lang") ?? "ru") === "ru";
  const lower = (detail || "").toLowerCase();
  for (const key of Object.keys(ERR)) {
    if (lower.includes(key)) return ERR[key][ru ? 0 : 1];
  }
  const generic: Record<number, [string, string]> = {
    400: ["Проверь заполнение полей — что-то введено неверно", "Check the form — some field is invalid"],
    401: ["Сессия истекла — войди заново", "Session expired — sign in again"],
    403: ["Действие запрещено для твоей роли", "This action is not allowed for your role"],
    404: ["Не найдено — возможно, уже удалено. Обнови страницу", "Not found — refresh the page"],
    409: ["Уже существует — выбери другое имя или код", "Already exists — pick another name/code"],
    413: ["Файл слишком большой — максимум 20 МБ", "File too large — 20 MB max"],
    422: ["Проверь заполнение полей — что-то введено неверно", "Check the form — some field is invalid"],
    500: ["Внутренняя ошибка сервера — попробуй ещё раз", "Internal server error — try again"],
    502: ["Сервис недоступен (перезапуск или панель) — попробуй через минуту", "Service unavailable — try again in a minute"],
  };
  const g = generic[status];
  const base = g ? g[ru ? 0 : 1] : ru ? "Неизвестная ошибка" : "Unknown error";
  return detail && detail !== "Internal Server Error" ? `${base} · ${detail.slice(0, 120)}` : base;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    const ru = (localStorage.getItem("lang") ?? "ru") === "ru";
    throw new ApiError(
      0,
      ru ? "Нет связи с сервером — проверь интернет" : "Can't reach the server — check your connection",
    );
  }
  if (res.status === 401) {
    setToken(null);
    if (!location.hash.includes("login")) location.hash = "#/login";
    throw new ApiError(401, "unauthorized");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = (await res.json()) as { detail?: unknown };
      if (typeof data.detail === "string") detail = data.detail;
      else if (data.detail) detail = JSON.stringify(data.detail);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};

/* ---- formatters ---- */

export function money(minor: number, currency = "RUB"): string {
  const v = minor / 100;
  const num = v.toLocaleString("ru-RU", {
    minimumFractionDigits: v % 1 ? 2 : 0,
    maximumFractionDigits: 2,
  });
  return currency === "RUB" ? `${num} ₽` : `${num} ${currency}`;
}

export function bytesFmt(n: number): string {
  if (!n) return "0";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  const v = n / 1024 ** i;
  return `${v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

export function dt(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
}

export function dtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return (
    d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" }) +
    " " +
    d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })
  );
}
