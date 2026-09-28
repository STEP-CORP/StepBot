/* App shell: sidebar (14 items, groups, badges, statuses) + topbar (crumbs,
   panel badge, theme/lang segments, avatar). */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router";

import { api, setToken } from "../api/client";
import { useApp } from "../state/app";
import { Seg } from "./ui";

type Me = { user_id: number; username: string; role: string };
type Counters = { all: number };
type TicketsResp = { open_count: number };

/* Live build version, bottom-right. Polls /api/version; when the server ships a newer
   build than the one this page loaded with, it offers a one-click reload (index.html is
   served no-cache, so the reload pulls the fresh SPA). Fixes "фронт не подхватывает
   свежую версию" + shows the actual running version. */
function VersionBadge() {
  const v = useQuery({
    queryKey: ["app-version"],
    queryFn: () => api.get<{ version: string; build: string }>("/api/version"),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
  const loaded = useRef<string | null>(null);
  const current = v.data?.version ?? null;
  if (current && loaded.current === null) loaded.current = current;
  const stale = current !== null && loaded.current !== null && current !== loaded.current;
  return (
    <div
      style={{
        position: "fixed",
        right: 12,
        bottom: 10,
        zIndex: 50,
        fontSize: 11,
        display: "flex",
        alignItems: "center",
        gap: 8,
        pointerEvents: stale ? "auto" : "none",
      }}
    >
      {stale && (
        <button
          onClick={() => window.location.reload()}
          style={{
            border: "1px solid var(--accent, #F7971D)",
            background: "var(--accent, #F7971D)",
            color: "#000",
            borderRadius: 20,
            padding: "4px 11px",
            cursor: "pointer",
            fontWeight: 600,
            fontSize: 11,
          }}
        >
          🔄 Новая версия — обновить
        </button>
      )}
      <span
        className="mono"
        style={{ color: "var(--dim)", background: "var(--panel)", padding: "3px 8px", borderRadius: 6, opacity: 0.85 }}
      >
        v{current ?? "…"}
      </span>
    </div>
  );
}

export function BrandLogo({ size = 15 }: { size?: number }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        fontFamily: "'Arial Black','Arial Bold',Arial,sans-serif",
        fontWeight: 900,
        fontSize: size,
        letterSpacing: "-0.5px",
        lineHeight: 1,
      }}
    >
      <span style={{ color: "var(--text)" }}>STEP</span>
      <span
        style={{
          background: "#E0251F",
          color: "#fff",
          borderRadius: size * 0.28,
          padding: `${size * 0.14}px ${size * 0.38}px`,
        }}
      >
        BOT
      </span>
    </span>
  );
}

export default function Layout() {
  const { t, theme, setTheme, lang, setLang } = useApp();
  const qc = useQueryClient();
  const loc = useLocation();
  const nav = useNavigate();

  const me = useQuery({ queryKey: ["me"], queryFn: () => api.get<Me>("/api/admin/auth/me") });
  const counters = useQuery({
    queryKey: ["users", "counters"],
    queryFn: () => api.get<Counters>("/api/admin/users/counters"),
    refetchInterval: 60_000,
  });
  const tickets = useQuery({
    queryKey: ["tickets"],
    queryFn: () => api.get<TicketsResp>("/api/admin/tickets"),
    refetchInterval: 60_000,
  });

  const items: {
    group?: string;
    icon: string;
    path: string;
    label: string;
    badge?: number;
  }[] = [
    { icon: "📊", path: "/", label: t.dashboard },
    { group: t.gProduct, icon: "📈", path: "/stats", label: t.statsTitle },
    { icon: "👥", path: "/users", label: t.users, badge: counters.data?.all },
    { icon: "💳", path: "/tariffs", label: t.tariffs },
    { icon: "🏷️", path: "/promos", label: t.promos },
    { group: t.gConstructor, icon: "🧱", path: "/bot-buttons", label: t.botButtons },
    { icon: "🖼️", path: "/bot-images", label: t.botImages },
    { icon: "📱", path: "/miniapp", label: t.miniapp },
    { group: t.gMarketing, icon: "📣", path: "/broadcasts", label: t.broadcasts },
    { icon: "⏰", path: "/smart", label: t.smart },
    { icon: "🔔", path: "/notifications", label: t.notifications },
    { icon: "⏳", path: "/reminders", label: t.reminders },
    { icon: "📈", path: "/campaigns", label: t.campaigns },
    { icon: "🔥", path: "/sales", label: t.salesTitle },
    { icon: "🎯", path: "/promo-groups", label: t.promoGroups },
    { icon: "🤝", path: "/partners", label: t.partners },
    { group: t.gOps, icon: "💰", path: "/payments", label: t.payments },
    { icon: "🎫", path: "/tickets", label: t.tickets, badge: tickets.data?.open_count },
    { icon: "🤖", path: "/ai-support", label: t.aiSupport },
    { icon: "🚫", path: "/blacklist", label: t.blacklist },
    { group: t.gSystem, icon: "🌍", path: "/servers", label: t.servers },
    { icon: "⚙️", path: "/settings", label: t.settings },
    { icon: "🛠️", path: "/maintenance", label: t.maintenance },
  ];

  const current = items.find(
    (i) => i.path === (loc.pathname === "/" ? "/" : "/" + loc.pathname.split("/")[1]),
  );

  const [navQ, setNavQ] = useState("");
  const filteredItems = useMemo(() => {
    if (!navQ.trim()) return items;
    const n = navQ.toLowerCase();
    return items.filter((i) => i.label.toLowerCase().includes(n));
  }, [items, navQ]);

  // Settings quick-search: jump straight to the matching parameter block.
  const paramHits = useQuery({
    queryKey: ["nav-param-search", navQ],
    queryFn: () =>
      api.get<{ params: { key: string; name: string; category: string }[] }>(
        `/api/admin/settings?q=${encodeURIComponent(navQ)}`,
      ),
    enabled: navQ.trim().length >= 2,
  });

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="side-logo">
          <div className="row" style={{ gap: 8 }}>
            <BrandLogo size={16} />
            <span className="caps" style={{ letterSpacing: "0.12em" }}>CABINET</span>
          </div>
          <div style={{ position: "relative", marginTop: 12 }}>
            <span className="dim" style={{ position: "absolute", left: 9, top: "50%", transform: "translateY(-50%)", fontSize: 13 }}>⌕</span>
            <input
              className="input"
              style={{ width: "100%", paddingLeft: 28, fontSize: 12.5 }}
              placeholder={t.sideSearch}
              value={navQ}
              onChange={(e) => setNavQ(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") setNavQ("");
                if (e.key === "Enter" && filteredItems[0]) {
                  nav(filteredItems[0].path);
                  setNavQ("");
                }
              }}
            />
          </div>
        </div>
        <nav style={{ paddingBottom: 12 }}>
          {navQ.trim().length >= 2 && (paramHits.data?.params ?? []).length > 0 && (
            <>
              <div className="side-group caps">{t.sideSearchParams}</div>
              {(paramHits.data?.params ?? []).slice(0, 5).map((prm) => (
                <button
                  key={prm.key}
                  className="side-item"
                  style={{ width: "100%", textAlign: "left", background: "none", border: 0 }}
                  onClick={() => {
                    sessionStorage.setItem("settings_q", prm.name);
                    setNavQ("");
                    nav("/settings");
                  }}
                >
                  <span className="ico">⚙️</span>
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{prm.name}</span>
                </button>
              ))}
            </>
          )}
          {filteredItems.map((i) => (
            <span key={i.path}>
              {i.group && <div className="side-group caps">{i.group}</div>}
              <NavLink
                to={i.path}
                end={i.path === "/"}
                className={({ isActive }) => "side-item" + (isActive ? " active" : "")}
              >
                <span className="ico">{i.icon}</span>
                {i.label}
                {i.badge !== undefined && i.badge > 0 && (
                  <span className="badge">{i.badge.toLocaleString("ru-RU")}</span>
                )}
              </NavLink>
            </span>
          ))}
        </nav>
        <div className="side-footer">
          <span className="caps">
            <span className="status-dot" />
            API · ONLINE
          </span>
          <span className="caps">
            <span className="status-dot" />
            REMNAWAVE · SYNC
          </span>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <span className="crumbs">ADMIN / {current?.label ?? ""}</span>
          <span className="spacer" />
          <span className="cap-pill">● REMNAWAVE · OK</span>
          <Seg
            value={theme}
            options={[
              { id: "dark" as const, label: "DARK" },
              { id: "light" as const, label: "LIGHT" },
            ]}
            onChange={setTheme}
          />
          <Seg
            value={lang}
            options={[
              { id: "ru" as const, label: "RU" },
              { id: "en" as const, label: "EN" },
            ]}
            onChange={setLang}
          />
          <div className="row" style={{ gap: 8 }}>
            <div className="avatar-sq">
              {(me.data?.username ?? "??").slice(0, 2).toUpperCase()}
            </div>
            <div style={{ lineHeight: 1.2 }}>
              <div style={{ fontSize: 12.5 }}>@{me.data?.username}</div>
              <div className="caps">{me.data?.role}</div>
            </div>
            <button
              className="btn secondary sm"
              title={t.logout}
              onClick={() => {
                setToken(null);
                qc.clear(); // drop the previous admin's cached data before the next login
                nav("/login");
              }}
            >
              ⎋
            </button>
          </div>
        </header>
        <main className="content">
          <div className="content-inner">
            <Outlet />
          </div>
        </main>
      </div>
      <VersionBadge />
    </div>
  );
}
