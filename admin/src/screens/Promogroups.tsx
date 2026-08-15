/* Screen 21. Скидочные группы: priority discount tiers referenced by promocodes & campaigns. */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import { Toggle } from "../components/ui";
import { useApp } from "../state/app";

type Group = {
  id: number;
  name: string;
  priority: number;
  is_default: boolean;
  server_discount_pct: number;
  traffic_discount_pct: number;
  device_discount_pct: number;
  period_discounts: Record<string, number>;
  auto_assign_total_spent_minor: number | null;
  apply_discounts_to_addons: boolean;
  members: number;
  promocodes: number;
  campaigns: number;
};

const NEW: Omit<Group, "id" | "members" | "promocodes" | "campaigns"> = {
  name: "",
  priority: 0,
  is_default: false,
  server_discount_pct: 0,
  traffic_discount_pct: 0,
  device_discount_pct: 0,
  period_discounts: {},
  auto_assign_total_spent_minor: null,
  apply_discounts_to_addons: false,
};

const pct = (v: string) => Math.min(100, Math.max(0, Number(v) || 0));

export default function Promogroups() {
  const { t, toast, confirm } = useApp();
  const qc = useQueryClient();
  const list = useQuery({
    queryKey: ["promogroups"],
    queryFn: () => api.get<{ items: Group[] }>("/api/admin/promogroups"),
  });
  const [adding, setAdding] = useState(false);
  const [nw, setNw] = useState<Omit<Group, "id" | "members" | "promocodes" | "campaigns">>({ ...NEW });
  const [draft, setDraft] = useState<Record<number, Partial<Group>>>({});

  const refresh = () => void qc.invalidateQueries({ queryKey: ["promogroups"] });

  function edit(g: Group, patch: Partial<Group>) {
    setDraft((d) => ({ ...d, [g.id]: { ...(d[g.id] ?? {}), ...patch } }));
  }
  function val(g: Group): Group {
    return { ...g, ...(draft[g.id] ?? {}) };
  }

  function body(g: Omit<Group, "id" | "members" | "promocodes" | "campaigns">) {
    return {
      name: g.name.trim(),
      priority: g.priority,
      is_default: g.is_default,
      server_discount_pct: g.server_discount_pct,
      traffic_discount_pct: g.traffic_discount_pct,
      device_discount_pct: g.device_discount_pct,
      period_discounts: g.period_discounts,
      auto_assign_total_spent_minor: g.auto_assign_total_spent_minor,
      apply_discounts_to_addons: g.apply_discounts_to_addons,
    };
  }

  async function add() {
    if (!nw.name.trim()) return;
    try {
      await api.post("/api/admin/promogroups", body(nw));
      setAdding(false);
      setNw({ ...NEW });
      refresh();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function save(g: Group) {
    try {
      await api.patch(`/api/admin/promogroups/${g.id}`, body(val(g)));
      setDraft((d) => {
        const n = { ...d };
        delete n[g.id];
        return n;
      });
      toast(`${t.saved} ✓`);
      refresh();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function remove(g: Group) {
    // The delete itself is refused server-side while active promocodes/campaigns are
    // still bound (see routes/admin/promos.py) — this dialog just shows the operator
    // the full blast radius up front instead of them finding out from a 409 toast.
    const bits: string[] = [];
    if (g.members > 0) bits.push(`${t.members}: ${g.members}`);
    if (g.promocodes > 0) bits.push(`${t.groupPromoCount}: ${g.promocodes}`);
    if (g.campaigns > 0) bits.push(`${t.groupCampaignCount}: ${g.campaigns}`);
    const question = bits.length ? `${t.groupDeleteImpact} (${bits.join(", ")}). ${t.deleteConfirm}` : t.deleteConfirm;
    if (!(await confirm(question))) return;
    try {
      await api.del(`/api/admin/promogroups/${g.id}`);
      refresh();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  const items = list.data?.items ?? [];

  function discountRow(g: Group | Omit<Group, "id" | "members" | "promocodes" | "campaigns">, on: (p: Partial<Group>) => void) {
    return (
      <div className="row" style={{ gap: 12, flexWrap: "wrap", alignItems: "center" }}>
        <label className="row" style={{ gap: 6 }}>
          <span className="caps">{t.grServer}</span>
          <input className="input num" style={{ width: 64 }} type="number" min={0} max={100}
            value={g.server_discount_pct}
            onChange={(e) => on({ server_discount_pct: pct(e.target.value) })} />
        </label>
        <label className="row" style={{ gap: 6 }}>
          <span className="caps">{t.grTraffic}</span>
          <input className="input num" style={{ width: 64 }} type="number" min={0} max={100}
            value={g.traffic_discount_pct}
            onChange={(e) => on({ traffic_discount_pct: pct(e.target.value) })} />
        </label>
        <label className="row" style={{ gap: 6 }}>
          <span className="caps">{t.grDevices}</span>
          <input className="input num" style={{ width: 64 }} type="number" min={0} max={100}
            value={g.device_discount_pct}
            onChange={(e) => on({ device_discount_pct: pct(e.target.value) })} />
        </label>
        <label className="row" style={{ gap: 6 }}>
          <span className="caps">{t.grAddons}</span>
          <Toggle on={g.apply_discounts_to_addons} onChange={(v) => on({ apply_discounts_to_addons: v })} />
        </label>
      </div>
    );
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="h1">{t.promoGroups}</h1>
          <div className="caps sub">{t.promoGroupsSub}</div>
        </div>
        <div className="actions">
          <button className="btn primary" onClick={() => setAdding(true)}>
            + {t.groupAdd}
          </button>
        </div>
      </div>

      {adding && (
        <div className="card" style={{ marginBottom: 12 }}>
          <div className="row" style={{ gap: 10, flexWrap: "wrap", alignItems: "center", marginBottom: 10 }}>
            <input className="input" style={{ flex: 1, minWidth: 160 }} placeholder={t.groupName}
              value={nw.name} onChange={(e) => setNw({ ...nw, name: e.target.value })} />
            <label className="row" style={{ gap: 6 }}>
              <span className="caps">{t.priority}</span>
              <input className="input num" style={{ width: 70 }} type="number" min={0} max={1000}
                value={nw.priority}
                onChange={(e) => setNw({ ...nw, priority: Math.max(0, Number(e.target.value) || 0) })} />
            </label>
            <label className="row" style={{ gap: 6 }}>
              <span className="caps">{t.groupDefault}</span>
              <Toggle on={nw.is_default} onChange={(v) => setNw({ ...nw, is_default: v })} />
            </label>
          </div>
          {discountRow(nw, (p) => setNw({ ...nw, ...p }))}
          <div className="row" style={{ gap: 6, marginTop: 10, justifyContent: "flex-end" }}>
            <button className="btn secondary" onClick={() => setAdding(false)}>{t.cancel}</button>
            <button className="btn primary" onClick={add}>{t.save}</button>
          </div>
        </div>
      )}

      <div className="grid" style={{ gap: 12 }}>
        {items.map((g0) => {
          const g = val(g0);
          const dirty = JSON.stringify(g) !== JSON.stringify(g0);
          return (
            <div key={g0.id} className="card">
              <div className="row" style={{ justifyContent: "space-between", gap: 10, flexWrap: "wrap", marginBottom: 10 }}>
                <input className="input" style={{ flex: 1, minWidth: 150 }}
                  value={g.name} onChange={(e) => edit(g0, { name: e.target.value })} />
                <label className="row" style={{ gap: 6 }}>
                  <span className="caps">{t.priority}</span>
                  <input className="input num" style={{ width: 70 }} type="number" min={0} max={1000}
                    value={g.priority}
                    onChange={(e) => edit(g0, { priority: Math.max(0, Number(e.target.value) || 0) })} />
                </label>
                <label className="row" style={{ gap: 6 }}>
                  <span className="caps">{t.groupDefault}</span>
                  <Toggle on={g.is_default} onChange={(v) => edit(g0, { is_default: v })} />
                </label>
                <span className="cap-pill">{t.members}: {g0.members}</span>
              </div>
              {discountRow(g, (p) => edit(g0, p))}
              <div className="row" style={{ gap: 6, marginTop: 10 }}>
                <button className="btn secondary sm" style={{ marginLeft: "auto" }} onClick={() => remove(g0)}>
                  {t.delete}
                </button>
                {dirty && (
                  <button className="btn primary sm" onClick={() => save(g0)}>{t.save}</button>
                )}
              </div>
            </div>
          );
        })}
        {items.length === 0 && <div className="caps dim" style={{ padding: 20, textAlign: "center" }}>{t.promoGroupsEmpty}</div>}
      </div>
    </>
  );
}
