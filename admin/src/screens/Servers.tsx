/* Screen 12 — Серверы Remnawave: panel summary + node table + for-sale toggles. */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, bytesFmt, dtTime } from "../api/client";
import { Toggle } from "../components/ui";
import { useApp } from "../state/app";

type Node = {
  id: number;
  name: string;
  country_code: string | null;
  address: string | null;
  status: "online" | "maintenance" | "offline";
  users_online: number;
  traffic_day_bytes: number;
  load_pct: number;
  ping_ms: number | null;
  uptime_pct: number | null;
  is_for_sale: boolean;
  last_sync_at: string | null;
};
type Squad = { id: number; name: string; original_name: string | null; uuid: string };
type Resp = { panel_url: string; items: Node[]; squads: Squad[] };
type Connection = {
  base_url: string;
  auth_type: "api_key" | "bearer" | "basic" | "caddy";
  basic_user: string;
  force_local: "auto" | "true" | "false";
  token_set: boolean;
  basic_password_set: boolean;
  caddy_api_key_set: boolean;
  cf_access_client_id_set: boolean;
  cf_access_client_secret_set: boolean;
  secret_key_cookie_set: boolean;
  webhook_secret_set: boolean;
};
type ConnectionDraft = {
  base_url: string;
  auth_type: Connection["auth_type"];
  basic_user: string;
  force_local: Connection["force_local"];
  token: string;
  basic_password: string;
  caddy_api_key: string;
  cf_access_client_id: string;
  cf_access_client_secret: string;
  secret_key_cookie: string;
  webhook_secret: string;
};

function draftFromConnection(c: Connection): ConnectionDraft {
  return {
    base_url: c.base_url,
    auth_type: c.auth_type,
    basic_user: c.basic_user,
    force_local: c.force_local,
    token: "",
    basic_password: "",
    caddy_api_key: "",
    cf_access_client_id: "",
    cf_access_client_secret: "",
    secret_key_cookie: "",
    webhook_secret: "",
  };
}

export default function Servers() {
  const { t, toast } = useApp();
  const qc = useQueryClient();
  const [syncing, setSyncing] = useState(false);
  const [savingConnection, setSavingConnection] = useState(false);
  const [checkingConnection, setCheckingConnection] = useState(false);
  const [connectionDraft, setConnectionDraft] = useState<ConnectionDraft | null>(null);

  const data = useQuery({
    queryKey: ["servers"],
    queryFn: () => api.get<Resp>("/api/admin/servers"),
  });
  const connection = useQuery({
    queryKey: ["remnawave-connection"],
    queryFn: () => api.get<Connection>("/api/admin/servers/connection"),
  });

  useEffect(() => {
    if (connection.data) setConnectionDraft(draftFromConnection(connection.data));
  }, [connection.data]);

  function setConnectionField<K extends keyof ConnectionDraft>(key: K, value: ConnectionDraft[K]) {
    setConnectionDraft((draft) => (draft ? { ...draft, [key]: value } : draft));
  }

  async function saveConnection() {
    if (!connectionDraft) return;
    setSavingConnection(true);
    try {
      const body: Record<string, string> = {
        base_url: connectionDraft.base_url,
        auth_type: connectionDraft.auth_type,
        basic_user: connectionDraft.basic_user,
        force_local: connectionDraft.force_local,
      };
      for (const key of [
        "token",
        "basic_password",
        "caddy_api_key",
        "cf_access_client_id",
        "cf_access_client_secret",
        "secret_key_cookie",
        "webhook_secret",
      ] as const) {
        if (connectionDraft[key].trim()) body[key] = connectionDraft[key].trim();
      }
      await api.patch("/api/admin/servers/connection", body);
      setConnectionDraft((draft) =>
        draft
          ? {
              ...draft,
              token: "",
              basic_password: "",
              caddy_api_key: "",
              cf_access_client_id: "",
              cf_access_client_secret: "",
              secret_key_cookie: "",
              webhook_secret: "",
            }
          : draft,
      );
      void qc.invalidateQueries({ queryKey: ["remnawave-connection"] });
      void qc.invalidateQueries({ queryKey: ["servers"] });
      toast(t.saved);
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setSavingConnection(false);
    }
  }

  async function checkConnection() {
    setCheckingConnection(true);
    try {
      const r = await api.post<{ version: string }>("/api/admin/servers/connection/check");
      toast(`✓ Remnawave ${r.version}`);
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setCheckingConnection(false);
    }
  }

  async function resetConnection() {
    try {
      await api.post("/api/admin/servers/connection/reset");
      void qc.invalidateQueries({ queryKey: ["remnawave-connection"] });
      void qc.invalidateQueries({ queryKey: ["servers"] });
      toast(t.resetToEnv);
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function sync() {
    setSyncing(true);
    try {
      const r = await api.post<{ synced: number }>("/api/admin/servers/sync");
      void qc.invalidateQueries({ queryKey: ["servers"] });
      toast(`✓ ${r.synced} ${t.nodes}`);
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setSyncing(false);
    }
  }

  async function toggleSale(n: Node, on: boolean) {
    await api.patch(`/api/admin/servers/${n.id}`, { is_for_sale: on });
    void qc.invalidateQueries({ queryKey: ["servers"] });
    toast(`${n.name}: ${on ? t.on : t.off}`);
  }

  async function renameSquad(sq: Squad, value: string) {
    const name = value.trim();
    if (name === sq.name) return;
    try {
      const r = await api.patch<{ squad: Squad }>(`/api/admin/servers/squads/${sq.id}`, {
        display_name: name,
      });
      void qc.invalidateQueries({ queryKey: ["servers"] });
      toast(`✓ ${r.squad.name}`);
    } catch (e) {
      toast((e as Error).message);
    }
  }

  const d = data.data;
  const totalUsers = d?.items.reduce((a, n) => a + n.users_online, 0) ?? 0;
  const totalTraffic = d?.items.reduce((a, n) => a + n.traffic_day_bytes, 0) ?? 0;
  const lastSync = d?.items.map((n) => n.last_sync_at).filter(Boolean).sort().at(-1);
  const cols = "1.6fr 1.2fr 0.8fr 1fr 0.7fr 0.7fr 1.1fr auto";

  return (
    <>
      <div className="page-head">
        <h1 className="h1">{t.servers}</h1>
        <div className="actions">
          <button className="btn primary" onClick={sync} disabled={syncing}>
            {syncing ? <span className="spin">⟳</span> : "⟳"} {t.syncBtn}
          </button>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="row" style={{ flexWrap: "wrap", gap: 24 }}>
          <span className="mono muted">{d?.panel_url ?? "…"}</span>
          <span className="caps">
            {d?.items.length ?? 0} {t.nodes}
          </span>
          <span className="caps">
            {totalUsers} {t.online.toLowerCase()}
          </span>
          <span className="caps">{bytesFmt(totalTraffic)}/сут</span>
          <span className="caps" style={{ marginLeft: "auto" }}>
            {t.lastSync}: {lastSync ? dtTime(lastSync) : "—"}
          </span>
        </div>
      </div>

      {connectionDraft && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="row" style={{ justifyContent: "space-between", gap: 12 }}>
            <div>
              <div className="caps" style={{ marginBottom: 4 }}>{t.remnawaveConnection}</div>
              <div className="dim" style={{ fontSize: 11.5 }}>{t.remnawaveConnectionHint}</div>
            </div>
            <div className="row" style={{ gap: 8 }}>
              <button className="btn secondary sm" onClick={() => void checkConnection()} disabled={checkingConnection}>
                {checkingConnection ? "⟳" : "✓"} {t.checkConn}
              </button>
              <button className="btn primary sm" onClick={() => void saveConnection()} disabled={savingConnection}>
                {savingConnection ? "⟳" : "✓"} {t.save}
              </button>
            </div>
          </div>
          <div className="kpis" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(230px, 1fr))", marginTop: 14 }}>
            <label>
              <div className="caps">{t.remnawaveUrl}</div>
              <input className="input mono" value={connectionDraft.base_url} onChange={(e) => setConnectionField("base_url", e.target.value)} placeholder="https://panel.example.com" />
            </label>
            <label>
              <div className="caps">{t.remnawaveAuth}</div>
              <select className="input" value={connectionDraft.auth_type} onChange={(e) => setConnectionField("auth_type", e.target.value as ConnectionDraft["auth_type"])}>
                <option value="api_key">API key</option>
                <option value="bearer">Bearer</option>
                <option value="basic">Basic</option>
                <option value="caddy">Caddy</option>
              </select>
            </label>
            <label>
              <div className="caps">{t.forceLocal}</div>
              <select className="input" value={connectionDraft.force_local} onChange={(e) => setConnectionField("force_local", e.target.value as ConnectionDraft["force_local"])}>
                <option value="auto">{t.forceAuto}</option>
                <option value="true">{t.forceYes}</option>
                <option value="false">{t.forceNo}</option>
              </select>
            </label>
            <label>
              <div className="caps">{t.remnawaveToken}</div>
              <input className="input mono" type="password" value={connectionDraft.token} onChange={(e) => setConnectionField("token", e.target.value)} placeholder={connection.data?.token_set ? t.secretKeep : "API token"} autoComplete="new-password" />
            </label>
            <label>
              <div className="caps">{t.basicUser}</div>
              <input className="input" value={connectionDraft.basic_user} onChange={(e) => setConnectionField("basic_user", e.target.value)} />
            </label>
            <label>
              <div className="caps">{t.basicPassword}</div>
              <input className="input mono" type="password" value={connectionDraft.basic_password} onChange={(e) => setConnectionField("basic_password", e.target.value)} placeholder={connection.data?.basic_password_set ? t.secretKeep : "Basic password"} autoComplete="new-password" />
            </label>
            <label>
              <div className="caps">{t.caddyApiKey}</div>
              <input className="input mono" type="password" value={connectionDraft.caddy_api_key} onChange={(e) => setConnectionField("caddy_api_key", e.target.value)} placeholder={connection.data?.caddy_api_key_set ? t.secretKeep : "Caddy API key"} autoComplete="new-password" />
            </label>
            <label>
              <div className="caps">{t.cfAccessId}</div>
              <input className="input mono" type="password" value={connectionDraft.cf_access_client_id} onChange={(e) => setConnectionField("cf_access_client_id", e.target.value)} placeholder={connection.data?.cf_access_client_id_set ? t.secretKeep : "Optional"} autoComplete="new-password" />
            </label>
            <label>
              <div className="caps">{t.cfAccessSecret}</div>
              <input className="input mono" type="password" value={connectionDraft.cf_access_client_secret} onChange={(e) => setConnectionField("cf_access_client_secret", e.target.value)} placeholder={connection.data?.cf_access_client_secret_set ? t.secretKeep : "Optional"} autoComplete="new-password" />
            </label>
            <label>
              <div className="caps">{t.cookie}</div>
              <input className="input mono" type="password" value={connectionDraft.secret_key_cookie} onChange={(e) => setConnectionField("secret_key_cookie", e.target.value)} placeholder={connection.data?.secret_key_cookie_set ? t.secretKeep : "name:value"} autoComplete="new-password" />
            </label>
            <label>
              <div className="caps">{t.webhookSecret}</div>
              <input className="input mono" type="password" value={connectionDraft.webhook_secret} onChange={(e) => setConnectionField("webhook_secret", e.target.value)} placeholder={connection.data?.webhook_secret_set ? t.secretKeep : "Webhook secret"} autoComplete="new-password" />
            </label>
          </div>
          <div className="row" style={{ justifyContent: "space-between", marginTop: 12, gap: 12 }}>
            <span className="dim" style={{ fontSize: 11.5 }}>{t.remnawaveSecretsHint}</span>
            <button className="btn secondary sm" onClick={() => void resetConnection()}>{t.resetToEnv}</button>
          </div>
        </div>
      )}

      <div className="tbl">
        <div className="tr head" style={{ gridTemplateColumns: cols }}>
          <span>NODE</span>
          <span>{t.load}</span>
          <span>{t.online}</span>
          <span>{t.colTraffic}</span>
          <span>PING</span>
          <span>UPTIME</span>
          <span>{t.colStatus}</span>
          <span>{t.forSale}</span>
        </div>
        {(d?.items ?? []).map((n) => (
          <div key={n.id} className="tr" style={{ gridTemplateColumns: cols }}>
            <span>
              <b style={{ fontWeight: 500 }}>{n.name}</b>
              <div className="dim" style={{ fontSize: 11.5 }}>
                {n.country_code ?? "—"} · {n.address ?? "—"}
              </div>
            </span>
            <span>
              <div className="mono" style={{ fontSize: 11, marginBottom: 3 }}>
                {n.load_pct}%
              </div>
              <div className="prog">
                <i
                  style={{
                    width: `${n.load_pct}%`,
                    background: n.load_pct > 70 ? "var(--text)" : "var(--muted)",
                  }}
                />
              </div>
            </span>
            <span className="mono">{n.users_online}</span>
            <span className="mono muted">{bytesFmt(n.traffic_day_bytes)}</span>
            <span className="mono muted">{n.ping_ms !== null ? `${n.ping_ms}ms` : "—"}</span>
            <span className="mono muted">
              {n.uptime_pct !== null ? `${n.uptime_pct}%` : "—"}
            </span>
            <span
              className={`st ${
                n.status === "online" ? "on" : n.status === "maintenance" ? "mid" : "off"
              }`}
            >
              {n.status === "online" && <span className="status-dot" />}
              {n.status === "online" ? t.onlineSt : n.status === "maintenance" ? t.maintSt : t.offlineSt}
            </span>
            <Toggle on={n.is_for_sale} onChange={(v) => void toggleSale(n, v)} />
          </div>
        ))}
        {d && d.items.length === 0 && (
          <div className="tr dim">— · {t.syncBtn} →</div>
        )}
      </div>

      {d && d.squads.length > 0 && (
        <div className="card" style={{ marginTop: 14 }}>
          <div className="caps" style={{ marginBottom: 4 }}>{t.squadsTitle}</div>
          <div className="dim" style={{ fontSize: 11.5, marginBottom: 10 }}>
            {t.squadNameHint}
          </div>
          {d.squads.map((sq) => (
            <div
              key={sq.id}
              className="row"
              style={{ gap: 12, alignItems: "baseline", padding: "5px 0" }}
            >
              <input
                className="input"
                style={{ maxWidth: 320 }}
                defaultValue={sq.name}
                placeholder={sq.original_name ?? ""}
                onBlur={(e) => void renameSquad(sq, e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") e.currentTarget.blur();
                }}
              />
              {sq.original_name && sq.original_name !== sq.name && (
                <span className="dim mono" style={{ fontSize: 11.5 }}>
                  {t.squadPanelName}: {sq.original_name}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </>
  );
}
