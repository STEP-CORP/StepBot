/* Screen 05 — Конструктор кнопок бота: tree + editor + live chat preview. */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";

import { api, getToken } from "../api/client";
import { Field, Seg, Toggle } from "../components/ui";
import { useApp } from "../state/app";

/* Buttons of the built-in «Личный кабинет» screen. The owner toggles which show, reorders,
   RENAMES them, and adds their own buttons pointing at any bot action. Saved to CABINET_BUTTONS. */
type CabBtn = {
  key: string;
  label: string;
  icon: string | null;
  color: string | null;
  enabled: boolean;
  gated: boolean;
  custom: boolean;
  action: string | null;
  btype?: "action" | "link" | "miniapp" | "screen" | null; // custom-button kind; built-ins are always actions
  url?: string | null; // https/tg target when btype is link/miniapp
  stext?: string | null; // sub-screen body when btype is screen
  default_label: string | null;
  row?: number | null; // physical row in the custom layout; null/undefined in uniform mode
};
type CabAction = { code: string; label_ru: string; label_en: string; needs_subscription: boolean };

/* A live-preview button for the shared custom-layout editor. */
type PBtn = { id: string; label: string; color: string | null; selected: boolean };

/* Free drag-and-drop row editor used by BOTH previews when «Свои» (custom) layout is on.
   Drop a button ONTO another to join its row (capped at 3, the Bot API grid limit); drop it
   into a GAP between rows (or at the ends) to start a fresh row. `onCommit` receives the new
   rows as arrays of ids, in reading order — the caller stamps its own row/order from that. */
function CustomRows({
  rows,
  onSelect,
  onCommit,
  hint,
}: {
  rows: PBtn[][];
  onSelect: (id: string) => void;
  onCommit: (rows: string[][]) => void;
  hint: string;
}) {
  const [drag, setDrag] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const ids = rows.map((r) => r.map((b) => b.id));

  function end() {
    setDrag(null);
    setOver(null);
  }
  function dropNewRow(anchor: string[] | null) {
    if (!drag) return end();
    let rr = ids.map((r) => r.filter((x) => x !== drag)).filter((r) => r.length);
    let gi: number;
    if (!anchor) gi = rr.length;
    else {
      const rem = anchor.filter((x) => x !== drag);
      gi = rem.length ? rr.findIndex((r) => r.some((x) => rem.includes(x))) : rr.length;
      if (gi < 0) gi = rr.length;
    }
    rr.splice(gi, 0, [drag]);
    onCommit(rr);
    end();
  }
  function dropOnButton(targetId: string) {
    if (!drag || drag === targetId) return end();
    let rr = ids.map((r) => r.filter((x) => x !== drag)).filter((r) => r.length);
    const tr = rr.findIndex((r) => r.includes(targetId));
    if (tr < 0) onCommit(rr);
    else if (rr[tr].length >= 3) {
      /* row already full — ignore the drop */
    } else {
      rr[tr].splice(rr[tr].indexOf(targetId), 0, drag);
      onCommit(rr);
    }
    end();
  }

  const Gap = ({ anchor, id }: { anchor: string[] | null; id: string }) => (
    <div
      onDragOver={(e) => {
        if (!drag) return;
        e.preventDefault();
        if (over !== id) setOver(id);
      }}
      onDrop={(e) => {
        e.preventDefault();
        dropNewRow(anchor);
      }}
      style={{
        height: over === id ? 22 : 10,
        borderRadius: 4,
        margin: "1px 0",
        transition: "height .1s",
        border: over === id ? "2px dashed #3b82f6" : "2px dashed transparent",
        background: over === id ? "rgba(59,130,246,.12)" : "transparent",
      }}
    />
  );

  return (
    <div className="grid" style={{ gap: 0 }}>
      <Gap anchor={ids[0] ?? null} id="g-top" />
      {rows.map((row, ri) => (
        <Fragment key={ri}>
          <div className="row" style={{ gap: 6 }}>
            {row.map((b) => (
              <button
                key={b.id}
                draggable
                onDragStart={() => setDrag(b.id)}
                onDragEnd={end}
                onDragOver={(e) => {
                  if (!drag) return;
                  e.preventDefault();
                  if (over !== `b-${b.id}`) setOver(`b-${b.id}`);
                }}
                onDrop={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  dropOnButton(b.id);
                }}
                onClick={() => onSelect(b.id)}
                title={b.label}
                style={{
                  flex: "1 1 0",
                  minWidth: 0,
                  borderRadius: 6,
                  border:
                    over === `b-${b.id}` && drag && drag !== b.id
                      ? "2px solid #3b82f6"
                      : b.selected
                      ? "1px solid var(--text)"
                      : "1px solid var(--border2)",
                  background: b.color || "var(--panel)",
                  color: b.color ? "#fff" : "var(--text)",
                  padding: "9px 12px",
                  fontSize: 13,
                  cursor: "grab",
                  opacity: drag === b.id ? 0.4 : 1,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {b.label}
              </button>
            ))}
          </div>
          <Gap anchor={ids[ri + 1] ?? null} id={`g-${ri}`} />
        </Fragment>
      ))}
      {rows.length === 0 && <span className="dim">—</span>}
      <span className="dim" style={{ fontSize: 12, marginTop: 8 }}>
        {hint}
      </span>
    </div>
  );
}

let cabSeq = 0;
function CabinetButtonsCard({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  const { t, lang, toast, confirm } = useApp();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["cabinet-buttons"],
    queryFn: () =>
      api.get<{
        buttons: CabBtn[];
        per_row: number;
        layout?: string;
        buttons_default?: CabBtn[];
        per_row_default?: number;
        layout_default?: string;
        text?: string;
        text_default?: string;
        text_sample?: Record<string, string>;
        text_placeholders?: { token: string; desc: string }[];
        sub_active?: string;
        sub_active_default?: string;
        sub_inactive?: string;
        sub_inactive_default?: string;
        sub_placeholders?: { token: string; desc: string }[];
      }>("/api/admin/bot-menu/cabinet"),
  });
  const textRef = useRef<HTMLTextAreaElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (open) cardRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [open]);
  const [emojiId, setEmojiId] = useState("");
  const [emojiChar, setEmojiChar] = useState("🙂");
  const subActiveRef = useRef<HTMLTextAreaElement>(null);
  const subInactiveRef = useRef<HTMLTextAreaElement>(null);
  const [subActive, setSubActive] = useState<string | null>(null);
  const [subInactive, setSubInactive] = useState<string | null>(null);
  const [openSub, setOpenSub] = useState(false);
  const aq = useQuery({
    queryKey: ["bot-menu-actions"],
    queryFn: () => api.get<{ actions: CabAction[] }>("/api/admin/bot-menu/actions"),
  });
  const actions = aq.data?.actions ?? [];
  const actionLabel = (code: string) => {
    const a = actions.find((x) => x.code === code);
    if (!a) return code;
    return lang === "ru" ? a.label_ru : a.label_en;
  };

  const [items, setItems] = useState<CabBtn[] | null>(null);
  const [perRow, setPerRow] = useState<number | null>(null);
  const [layout, setLayout] = useState<string | null>(null);
  const [selKey, setSelKey] = useState<string | null>(null);
  const [dragKey, setDragKey] = useState<string | null>(null);
  const [overKey, setOverKey] = useState<string | null>(null);
  const [text, setText] = useState<string | null>(null);

  // When the owner hasn't set their own text, show the stock template ("вот что бот пишет
  // сейчас") so they edit from it instead of a blank box. Saving an unchanged/empty box still
  // means "default" on the backend, so this is display-only convenience.
  const caption = text ?? q.data?.text ?? q.data?.text_default ?? "";
  const placeholders = q.data?.text_placeholders ?? [];
  const sample = q.data?.text_sample ?? {};
  // {подписка} sub-block editors: own defaults + own placeholder catalogue (active state only).
  const subActiveVal = subActive ?? q.data?.sub_active ?? q.data?.sub_active_default ?? "";
  const subInactiveVal =
    subInactive ?? q.data?.sub_inactive ?? q.data?.sub_inactive_default ?? "";
  const subPlaceholders = q.data?.sub_placeholders ?? [];
  // Live preview: fill the tokens with the sample values, render Telegram HTML. Unknown tokens
  // stay literal (matches the bot). Admin-only, owner-authored text — HTML is rendered as-is.
  const previewHtml = caption.replace(
    /\{([A-Za-zА-Яа-яЁё0-9_]+)\}/g,
    (m: string, tok: string) => (tok in sample ? sample[tok] : m),
  );
  // Insert a {token} at the caret (or append), so chips drop the placeholder where you're typing.
  function insertToken(token: string) {
    const el = textRef.current;
    const cur = caption;
    if (!el) return setText(cur + token);
    const a = el.selectionStart ?? cur.length;
    const b = el.selectionEnd ?? cur.length;
    const next = cur.slice(0, a) + token + cur.slice(b);
    setText(next);
    requestAnimationFrame(() => {
      el.focus();
      const pos = a + token.length;
      el.setSelectionRange(pos, pos);
    });
  }

  // Same caret-insert as insertToken, but targeting an arbitrary textarea + its state setter,
  // so the two {подписка} editors share one implementation.
  function insertInto(
    ref: { current: HTMLTextAreaElement | null },
    cur: string,
    setter: (v: string) => void,
    token: string,
  ) {
    const el = ref.current;
    if (!el) return setter(cur + token);
    const a = el.selectionStart ?? cur.length;
    const b = el.selectionEnd ?? cur.length;
    setter(cur.slice(0, a) + token + cur.slice(b));
    requestAnimationFrame(() => {
      el.focus();
      const pos = a + token.length;
      el.setSelectionRange(pos, pos);
    });
  }

  const list = items ?? q.data?.buttons ?? [];
  const cols = perRow ?? q.data?.per_row ?? 2;
  const mode = layout ?? q.data?.layout ?? "custom"; // "uniform" | "custom"
  const sel = list.find((x) => x.key === selKey) ?? null;

  function update(next: CabBtn[]) {
    setItems(next);
  }
  function patch(key: string, p: Partial<CabBtn>) {
    update(list.map((x) => (x.key === key ? { ...x, ...p } : x)));
  }
  function patchSel(p: Partial<CabBtn>) {
    if (!selKey) return;
    patch(selKey, p);
  }
  // Rewrite the enabled buttons to the given rows (arrays of keys, reading order), stamping each
  // button's `row`. Disabled buttons keep their config and trail at the end with row=null. Keeping
  // same-row buttons adjacent in the list is what lets the bot group them into one physical row.
  function commitRows(rows: string[][]) {
    const rowOf = new Map<string, number>();
    rows.forEach((r, ri) => r.forEach((k) => rowOf.set(k, ri)));
    const flat = rows.flat();
    const enabled = flat.map((k) => ({ ...list.find((x) => x.key === k)!, row: rowOf.get(k)! }));
    const disabled = list.filter((b) => !b.enabled).map((b) => ({ ...b, row: null }));
    update([...enabled, ...disabled]);
  }
  // Slot-preserving arrows: swap the selected button with a neighbour WITHOUT changing the row
  // shape, so the layout never snaps back to a uniform preset. ←/→ step through reading order;
  // ↑/↓ swap with the same column one row up/down.
  function swapKeys(a: string, b: string) {
    commitRows(previewRows.map((r) => r.map((x) => (x.key === a ? b : x.key === b ? a : x.key))));
  }
  function moveFlat(delta: number) {
    if (!sel) return;
    const flat = previewRows.flat();
    const i = flat.findIndex((b) => b.key === sel.key);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= flat.length) return;
    swapKeys(flat[i].key, flat[j].key);
  }
  function moveVert(dir: number) {
    if (!sel) return;
    let ri = -1;
    let ci = -1;
    previewRows.forEach((r, r2) => r.forEach((b, c2) => (b.key === sel.key ? ((ri = r2), (ci = c2)) : 0)));
    if (ri < 0) return;
    const tr = ri + dir;
    if (tr < 0 || tr >= previewRows.length) return;
    const tc = Math.min(ci, previewRows[tr].length - 1);
    swapKeys(sel.key, previewRows[tr][tc].key);
  }
  // Enter custom mode seeding rows from whatever is on screen now, so the layout doesn't jump.
  function enterCustom() {
    const seed = previewRows.map((r) => r.map((b) => b.key));
    setLayout("custom");
    commitRows(seed);
  }
  // Drop `from` onto `to`'s slot: splice out, splice in.
  function reorder(from: string | null, to: string | null) {
    setDragKey(null);
    setOverKey(null);
    if (!from || !to || from === to) return;
    const next = [...list];
    const fi = next.findIndex((x) => x.key === from);
    const ti = next.findIndex((x) => x.key === to);
    if (fi < 0 || ti < 0) return;
    const [moved] = next.splice(fi, 1);
    next.splice(ti, 0, moved);
    update(next);
  }
  function remove(key: string) {
    update(list.filter((x) => x.key !== key));
    if (selKey === key) setSelKey(null);
  }
  function addCustom() {
    const used = new Set(
      list
        .filter((b) => b.enabled)
        .map((b) => (b.custom ? ((b.btype ?? "action") === "action" ? b.action : null) : b.key) ?? "")
        .filter(Boolean),
    );
    const code = actions.find((a) => !used.has(a.code))?.code ?? actions[0]?.code ?? "";
    const next: CabBtn = {
      key: `c${++cabSeq}_${Date.now().toString(36)}`,
      label: "",
      icon: null,
      color: null,
      enabled: true,
      gated: false,
      custom: true,
      action: code,
      btype: "action",
      url: null,
      default_label: null,
    };
    update([...list, next]);
    setSelKey(next.key);
  }
  async function save() {
    for (const b of list) {
      if (!b.label.trim()) return toast(t.cabinetBtnNeedLabelAction);
      if (b.custom) {
        const bt = b.btype ?? "action";
        if (bt === "action" && !b.action) return toast(t.cabinetBtnNeedLabelAction);
        if (bt === "link" && !(b.url || "").trim()) return toast(t.cabinetBtnNeedLabelAction);
      }
    }
    const payload = {
      per_row: cols,
      layout: mode,
      text: caption,
      sub_active: subActiveVal,
      sub_inactive: subInactiveVal,
      items: list.map((b) => ({
        key: b.key,
        label: b.label,
        enabled: b.enabled,
        icon: b.icon || undefined,
        color: b.color || undefined,
        btype: b.custom ? b.btype ?? "action" : undefined,
        action: b.custom && (b.btype ?? "action") === "action" ? b.action : undefined,
        url: b.custom && (b.btype ?? "action") === "link" ? b.url || "" : undefined,
        stext: b.custom && (b.btype ?? "action") === "screen" ? b.stext || "" : undefined,
        row: mode === "custom" && typeof b.row === "number" ? b.row : undefined,
      })),
    };
    try {
      await api.put("/api/admin/bot-menu/cabinet", payload);
      setItems(null);
      setPerRow(null);
      setLayout(null);
      setText(null);
      setSubActive(null);
      setSubInactive(null);
      void qc.invalidateQueries({ queryKey: ["cabinet-buttons"] });
      toast(t.saved);
    } catch (e) {
      toast((e as Error).message);
    }
  }
  // Restore the stock cabinet button set (order, labels, per-row) into the editor. Buttons only,
  // not the caption text; the owner still has to hit Save to persist it.
  async function resetButtons() {
    const d = q.data;
    if (!d) return;
    if (
      !(await confirm(
        lang === "ru"
          ? "Вернуть стандартные кнопки кабинета? Текущие заменятся."
          : "Restore the default cabinet buttons? Current ones will be replaced.",
      ))
    )
      return;
    setItems((d.buttons_default ?? []).map((b) => ({ ...b })));
    setPerRow(d.per_row_default ?? 2);
    setLayout(d.layout_default ?? "uniform");
    setSelKey(null);
  }

  // Live preview: only enabled buttons reach the bot. Uniform mode chunks them `cols` wide;
  // custom mode groups consecutive buttons by their `row` (new physical row when it changes or
  // the row is full at 3) — exactly what the bot's cabinet_rows() does.
  const shown = list.filter((b) => b.enabled);
  const previewRows: CabBtn[][] = [];
  if (mode === "custom") {
    let cur: number | null = null;
    let started = false;
    for (const b of shown) {
      const r = typeof b.row === "number" ? b.row : 0;
      if (!started || r !== cur || previewRows[previewRows.length - 1].length >= 3) {
        previewRows.push([]);
        cur = r;
        started = true;
      }
      previewRows[previewRows.length - 1].push(b);
    }
  } else {
    for (let k = 0; k < shown.length; k += cols) previewRows.push(shown.slice(k, k + cols));
  }

  // Default the cabinet button-row layout to «Свои»: on first load seed custom rows from the
  // current arrangement so the toggle starts on Custom without moving any button.
  const cabSeeded = useRef(false);
  useEffect(() => {
    if (cabSeeded.current || !q.data || layout !== null) return;
    cabSeeded.current = true;
    if ((q.data.layout ?? "uniform") !== "custom") enterCustom();
  }, [q.data, layout]);

  return (
    <div ref={cardRef} style={{ marginTop: 24, border: "1px solid var(--line, #2b2d33)", borderRadius: 14, padding: "6px 18px 18px" }}>
      <div className="page-head">
        <h1
          className="h1"
          style={{ cursor: "pointer", userSelect: "none" }}
          onClick={onToggle}
        >
          {open ? "▾" : "▸"}{" "}
          {lang === "ru" ? "Личный кабинет" : "Cabinet"}
        </h1>
      </div>

      {open && (
      <>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="caps" style={{ marginBottom: 10 }}>
          {lang === "ru" ? "Текст экрана" : "Screen text"}
        </div>
        <div className="cols" style={{ alignItems: "stretch" }}>
          {/* editor */}
          <div style={{ flex: "1 1 320px", minWidth: 0 }}>
            <div className="dim" style={{ fontSize: 12, marginBottom: 6 }}>
              {lang === "ru" ? "Текст:" : "Text:"}
            </div>
            <textarea
              ref={textRef}
              className="input"
              rows={9}
              value={caption}
              placeholder={
                lang === "ru"
                  ? "Пусто — стандартный текст кабинета. Можно вставить метки ниже."
                  : "Empty = default cabinet text. Insert the tokens below."
              }
              onChange={(e) => setText(e.target.value)}
              style={{ fontFamily: "inherit", resize: "vertical", width: "100%" }}
            />
            {placeholders.length > 0 && (
              <>
                <div className="dim" style={{ fontSize: 12, margin: "10px 0 6px" }}>
                  {lang === "ru"
                    ? "Метки (клик — вставить в текст): живые данные подставятся у каждого пользователя."
                    : "Tokens (click to insert): live data is filled in per user."}
                </div>
                <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                  {placeholders.map((ph) => (
                    <button
                      key={ph.token}
                      className="btn secondary sm"
                      title={ph.desc}
                      onClick={() => insertToken(`{${ph.token}}`)}
                      style={{ fontFamily: "monospace" }}
                    >
                      {`{${ph.token}}`}
                    </button>
                  ))}
                </div>
              </>
            )}
            <div className="dim" style={{ fontSize: 12, margin: "12px 0 6px" }}>
              {lang === "ru" ? "Кастом-эмодзи в тексте:" : "Custom emoji in text:"}
            </div>
            <div className="row" style={{ gap: 6, flexWrap: "wrap", alignItems: "center" }}>
              <input
                className="input mono"
                value={emojiId}
                placeholder="custom_emoji_id"
                onChange={(e) => setEmojiId(e.target.value.replace(/\D/g, ""))}
                style={{ width: 180 }}
              />
              <input
                className="input"
                value={emojiChar}
                onChange={(e) => setEmojiChar(e.target.value)}
                style={{ width: 60 }}
              />
              <button
                className="btn secondary sm"
                disabled={!emojiId}
                onClick={() =>
                  insertToken(
                    `<tg-emoji emoji-id="${emojiId}">${emojiChar || "🙂"}</tg-emoji>`,
                  )
                }
              >
                {lang === "ru" ? "＋ эмодзи" : "＋ emoji"}
              </button>
            </div>
            <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>
              {lang === "ru"
                ? "ID премиум-эмодзи + запасной символ (виден, если эмодзи недоступно)."
                : "Premium emoji id + fallback char (shown if the emoji is unavailable)."}
            </div>
          </div>
          {/* live preview */}
          <div style={{ flex: "1 1 300px", minWidth: 0 }}>
            <div className="dim" style={{ fontSize: 12, marginBottom: 6 }}>
              {lang === "ru"
                ? "Превью (пример данных — у каждого свои):"
                : "Preview (sample data — real per user):"}
            </div>
            <div
              style={{
                border: "1px solid var(--line, #333)",
                borderRadius: 10,
                padding: "12px 14px",
                background: "var(--panel, #17181c)",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                lineHeight: 1.5,
                minHeight: 120,
              }}
              dangerouslySetInnerHTML={{ __html: previewHtml }}
            />
            <div className="row" style={{ justifyContent: "flex-end", marginTop: 8 }}>
              <button
                className="btn secondary"
                title={
                  lang === "ru"
                    ? "Вернуть стандартный текст кабинета"
                    : "Restore the stock cabinet text"
                }
                onClick={async () => {
                  if (
                    !(await confirm(
                      lang === "ru"
                        ? "Сбросить текст кабинета к стандартному?"
                        : "Reset cabinet text to default?",
                    ))
                  )
                    return;
                  setText(q.data?.text_default ?? "");
                }}
                style={{ fontSize: 15, padding: "10px 18px" }}
              >
                {lang === "ru" ? "↺ Сбросить к стандартному" : "↺ Reset to default"}
              </button>
            </div>
          </div>
        </div>
      </div>
      <div className="card" style={{ marginBottom: 14 }}>
        <div
          className="caps"
          onClick={() => setOpenSub((v) => !v)}
          style={{ marginBottom: openSub ? 4 : 0, cursor: "pointer", userSelect: "none" }}
        >
          {openSub ? "▾" : "▸"}{" "}
          {lang === "ru" ? "Текст блока {подписка}" : "{подписка} block text"}
        </div>
        {openSub && (
        <>
        <div className="dim" style={{ fontSize: 12, marginBottom: 10 }}>
          {lang === "ru"
            ? "Подставляется вместо метки {подписка} — отдельно когда подписка активна и когда её нет."
            : "Replaces the {подписка} token — separately for an active subscription and for none."}
        </div>
        <div className="cols" style={{ alignItems: "stretch" }}>
          {[
            {
              key: "active",
              title: lang === "ru" ? "С подпиской (активна):" : "With subscription (active):",
              ref: subActiveRef,
              val: subActiveVal,
              set: setSubActive,
              def: q.data?.sub_active_default ?? "",
              rows: 7,
              chips: subPlaceholders,
            },
            {
              key: "inactive",
              title: lang === "ru" ? "Без подписки:" : "No subscription:",
              ref: subInactiveRef,
              val: subInactiveVal,
              set: setSubInactive,
              def: q.data?.sub_inactive_default ?? "",
              rows: 3,
              chips: [] as { token: string; desc: string }[],
            },
          ].map((ed) => (
            <div key={ed.key} style={{ flex: "1 1 320px", minWidth: 0 }}>
              <div className="dim" style={{ fontSize: 12, marginBottom: 6 }}>
                {ed.title}
              </div>
              <textarea
                ref={ed.ref}
                className="input"
                rows={ed.rows}
                value={ed.val}
                placeholder={
                  lang === "ru" ? "Пусто — стандартный текст" : "Empty = default text"
                }
                onChange={(e) => ed.set(e.target.value)}
                style={{ fontFamily: "inherit", resize: "vertical", width: "100%" }}
              />
              {ed.chips.length > 0 && (
                <div className="row" style={{ flexWrap: "wrap", gap: 6, marginTop: 8 }}>
                  {ed.chips.map((ph) => (
                    <button
                      key={ph.token}
                      className="btn secondary sm"
                      title={ph.desc}
                      onClick={() => insertInto(ed.ref, ed.val, ed.set, `{${ph.token}}`)}
                      style={{ fontFamily: "monospace" }}
                    >
                      {`{${ph.token}}`}
                    </button>
                  ))}
                </div>
              )}
              <div
                className="row"
                style={{ gap: 6, flexWrap: "wrap", alignItems: "center", marginTop: 8 }}
              >
                <button
                  className="btn secondary sm"
                  disabled={!emojiId}
                  title={
                    lang === "ru"
                      ? "Вставить кастом-эмодзи (ID из поля выше)"
                      : "Insert custom emoji (id from the field above)"
                  }
                  onClick={() =>
                    insertInto(
                      ed.ref,
                      ed.val,
                      ed.set,
                      `<tg-emoji emoji-id="${emojiId}">${emojiChar || "🙂"}</tg-emoji>`,
                    )
                  }
                >
                  {lang === "ru" ? "＋ эмодзи" : "＋ emoji"}
                </button>
                <button
                  className="btn secondary sm"
                  title={lang === "ru" ? "Вернуть стандартный текст" : "Restore default text"}
                  onClick={() => ed.set(ed.def)}
                >
                  {lang === "ru" ? "↺ Сбросить" : "↺ Reset"}
                </button>
              </div>
            </div>
          ))}
        </div>
        <div className="dim" style={{ fontSize: 11, marginTop: 8 }}>
          {lang === "ru"
            ? "Кастом-эмодзи берёт ID из поля выше."
            : "Custom emoji uses the id field above."}
        </div>
        </>
        )}
      </div>
      <div className="cols">
        {/* tree */}
        <div className="card" style={{ flex: "1 1 260px" }}>
          <div className="caps" style={{ marginBottom: 10 }}>
            {t.menuTree}
          </div>
          <div className="grid" style={{ gap: 6 }}>
            {list.map((b) => (
              <div
                key={b.key}
                className="row click"
                draggable
                onDragStart={() => setDragKey(b.key)}
                onDragEnd={() => {
                  setDragKey(null);
                  setOverKey(null);
                }}
                onDragOver={(e) => {
                  if (!dragKey) return;
                  e.preventDefault();
                  if (overKey !== b.key) setOverKey(b.key);
                }}
                onDrop={() => reorder(dragKey, b.key)}
                onClick={() => setSelKey(b.key)}
                title={t.cabinetBtnDrag}
                style={{
                  padding: "8px 10px",
                  cursor: "pointer",
                  gap: 8,
                  background: b.key === selKey ? "var(--pill)" : "var(--panel2)",
                  border: b.key === selKey ? "1px solid var(--muted)" : "1px solid var(--border)",
                  borderRadius: 6,
                  opacity: dragKey === b.key ? 0.4 : b.enabled ? 1 : 0.55,
                  outline:
                    overKey === b.key && dragKey && dragKey !== b.key ? "2px solid #3b82f6" : "none",
                }}
              >
                <span
                  style={{
                    width: 10,
                    height: 10,
                    borderRadius: "50%",
                    background: b.color || "var(--border2)",
                    flex: "0 0 auto",
                  }}
                />
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {b.label || b.default_label || t.cabinetBtnNewLabel}
                </span>
                {b.icon && <span className="dim">◈</span>}
                <span className="cap-pill dim" style={{ marginLeft: "auto" }}>
                  {b.custom ? actionLabel(b.action ?? "") : actionLabel(b.key)}
                </span>
              </div>
            ))}
          </div>
          <button
            className="btn secondary"
            style={{ marginTop: 12, width: "100%" }}
            onClick={addCustom}
            disabled={!actions.length}
          >
            + {t.cabinetBtnAdd}
          </button>
        </div>

        {/* editor */}
        <div className="card" style={{ flex: "1 1 300px" }}>
          {sel ? (
            <div className="grid" style={{ gap: 14 }}>
              <div className="row" style={{ gap: 4 }}>
                <button
                  className="btn secondary sm"
                  title={lang === "ru" ? "Влево" : "Left"}
                  onClick={() => moveFlat(-1)}
                >
                  ←
                </button>
                <button
                  className="btn secondary sm"
                  title={lang === "ru" ? "Вправо" : "Right"}
                  onClick={() => moveFlat(1)}
                >
                  →
                </button>
                <button
                  className="btn secondary sm"
                  title={lang === "ru" ? "Вверх (на ряд)" : "Up a row"}
                  onClick={() => moveVert(-1)}
                >
                  ↑
                </button>
                <button
                  className="btn secondary sm"
                  title={lang === "ru" ? "Вниз (на ряд)" : "Down a row"}
                  onClick={() => moveVert(1)}
                >
                  ↓
                </button>
                {sel.custom && (
                  <button
                    className="btn danger sm"
                    style={{ marginLeft: "auto" }}
                    onClick={() => remove(sel.key)}
                  >
                    ✕ {t.delete}
                  </button>
                )}
              </div>
              <Field label={lang === "ru" ? "Показывать" : "Show"}>
                <Toggle on={sel.enabled} onChange={(v) => patchSel({ enabled: v })} />
              </Field>
              <Field label={t.buttonText}>
                <input
                  className="input"
                  value={sel.label}
                  placeholder={sel.default_label ?? t.cabinetBtnNewLabel}
                  onChange={(e) => patchSel({ label: e.target.value })}
                />
              </Field>
              {sel.custom && (
                <Field label={t.buttonType}>
                  <Seg
                    value={sel.btype ?? "action"}
                    options={(["screen", "action", "link"] as const).map((k) => ({
                      id: k,
                      label: {
                        screen: lang === "ru" ? "Подменю" : "Submenu",
                        action: lang === "ru" ? "Действие" : "Action",
                        link: lang === "ru" ? "Ссылка" : "Link",
                      }[k],
                    }))}
                    onChange={(btype) => patchSel({ btype })}
                  />
                </Field>
              )}
              {(() => {
                const bt = sel.btype ?? "action";
                if (!sel.custom) {
                  return (
                    <Field label={t.cabinetBtnAction}>
                      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                        <span className="cap-pill dim">{actionLabel(sel.key)}</span>
                        {sel.gated && <span className="cap-pill dim">{t.cabinetButtonGated}</span>}
                      </div>
                    </Field>
                  );
                }
                if (bt === "action") {
                  return (
                    <Field label={t.cabinetBtnAction}>
                      <select
                        className="input"
                        value={sel.action ?? ""}
                        onChange={(e) => patchSel({ action: e.target.value })}
                      >
                        {actions
                          .filter(
                            (a) =>
                              a.code === sel.action ||
                              !list.some(
                                (b) =>
                                  b.key !== sel.key &&
                                  b.enabled &&
                                  (b.custom
                                    ? (b.btype ?? "action") === "action"
                                      ? b.action
                                      : null
                                    : b.key) === a.code,
                              ),
                          )
                          .map((a) => (
                            <option key={a.code} value={a.code}>
                              {lang === "ru" ? a.label_ru : a.label_en}
                            </option>
                          ))}
                      </select>
                    </Field>
                  );
                }
                if (bt === "link") {
                  return (
                    <Field label="URL">
                      <input
                        className="input mono"
                        value={sel.url ?? ""}
                        placeholder="https://…"
                        onChange={(e) => patchSel({ url: e.target.value })}
                      />
                    </Field>
                  );
                }
                if (bt === "miniapp") {
                  return null; // opens the global mini-app automatically; no field needed
                }
                return (
                  <Field label={lang === "ru" ? "Текст подменю" : "Submenu text"}>
                    <textarea
                      className="input"
                      rows={4}
                      value={sel.stext ?? ""}
                      onChange={(e) => patchSel({ stext: e.target.value })}
                    />
                  </Field>
                );
              })()}
              <Field label={t.buttonColor}>
                <div className="row" style={{ flexWrap: "wrap" }}>
                  {SWATCHES.map((c) => (
                    <button
                      key={c || "none"}
                      title={c || "—"}
                      onClick={() => patchSel({ color: c || null })}
                      style={{
                        width: 24,
                        height: 24,
                        borderRadius: 3,
                        cursor: "pointer",
                        background: c || "transparent",
                        border:
                          (sel.color ?? "") === c
                            ? "2px solid var(--text)"
                            : "1px solid var(--border2)",
                      }}
                    />
                  ))}
                  <input
                    className="input mono"
                    style={{ width: 100 }}
                    placeholder="#31A24C"
                    value={sel.color ?? ""}
                    onChange={(e) => patchSel({ color: e.target.value || null })}
                  />
                </div>
              </Field>
              <Field label={t.customEmoji}>
                <input
                  className="input mono"
                  value={sel.icon ?? ""}
                  placeholder="5368324170671202286"
                  onChange={(e) => patchSel({ icon: e.target.value.replace(/[^0-9]/g, "") || null })}
                />
              </Field>
            </div>
          ) : (
            <span className="dim">← {t.menuTree}</span>
          )}
        </div>

        {/* live preview */}
        <div className="card" style={{ flex: "1 1 300px" }}>
          <div className="caps" style={{ marginBottom: 10 }}>
            {t.livePreview}
          </div>
          {shown.length > 1 && (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                flexWrap: "wrap",
                padding: "10px 12px",
                marginBottom: 12,
                background: "var(--panel2)",
                border: "1px solid var(--border)",
                borderRadius: 8,
              }}
            >
              <span style={{ fontSize: 13, fontWeight: 600 }}>{t.perRow}:</span>
              <div className="row" style={{ gap: 6 }}>
                {[1, 2, 3].map((w) => (
                  <button
                    key={w}
                    className={`btn ${mode === "uniform" && cols === w ? "primary" : "secondary"}`}
                    style={{ minWidth: 42, padding: "6px 12px", fontWeight: 700 }}
                    onClick={() => {
                      setLayout("uniform");
                      setPerRow(w);
                    }}
                  >
                    {w}
                  </button>
                ))}
                <button
                  className={`btn ${mode === "custom" ? "primary" : "secondary"}`}
                  style={{ padding: "6px 14px", fontWeight: 700 }}
                  onClick={enterCustom}
                >
                  {lang === "ru" ? "Свои" : "Custom"}
                </button>
              </div>
              <span className="dim" style={{ fontSize: 12, flexBasis: "100%" }}>
                {mode === "custom"
                  ? lang === "ru"
                    ? "Перетаскивай кнопки: на другую — в её ряд, в пустое место — новый ряд."
                    : "Drag a button onto another to join its row, or into a gap for a new row."
                  : t.perRowHint}
              </span>
            </div>
          )}
          <div
            style={{
              background: "var(--panel2)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              padding: 14,
            }}
          >
            {mode === "custom" ? (
              <CustomRows
                rows={previewRows.map((r) =>
                  r.map((b) => ({
                    id: b.key,
                    label: b.label || b.default_label || t.cabinetBtnNewLabel,
                    color: b.color,
                    selected: b.key === selKey,
                  })),
                )}
                onSelect={setSelKey}
                onCommit={commitRows}
                hint={
                  lang === "ru"
                    ? "До 3 кнопок в ряд — как в Telegram."
                    : "Up to 3 buttons per row — matches Telegram."
                }
              />
            ) : (
              <div className="grid" style={{ gap: 6 }}>
                {previewRows.map((row, ri) => (
                  <div key={ri} className="row" style={{ gap: 6 }}>
                    {row.map((b) => (
                      <button
                        key={b.key}
                        onClick={() => setSelKey(b.key)}
                        title={b.label}
                        style={{
                          flex: "1 1 0",
                          minWidth: 0,
                          borderRadius: 6,
                          border:
                            b.key === selKey ? "1px solid var(--text)" : "1px solid var(--border2)",
                          background: b.color || "var(--panel)",
                          color: b.color ? "#fff" : "var(--text)",
                          padding: "9px 12px",
                          fontSize: 13,
                          cursor: "pointer",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {b.label || b.default_label || t.cabinetBtnNewLabel}
                      </button>
                    ))}
                  </div>
                ))}
                {shown.length === 0 && <span className="dim">—</span>}
              </div>
            )}
          </div>
        </div>
      </div>
      <div className="row" style={{ justifyContent: "flex-end", marginTop: 12, gap: 8 }}>
        <button
          className="btn secondary"
          title={
            lang === "ru"
              ? "Вернуть стандартный набор кнопок кабинета"
              : "Restore the stock cabinet button set"
          }
          onClick={resetButtons}
        >
          {lang === "ru" ? "↺ Сбросить кнопки" : "↺ Reset buttons"}
        </button>
        <button className="btn primary" onClick={save}>
          {lang === "ru" ? "Сохранить" : "Save"}
        </button>
      </div>
      </>
      )}
    </div>
  );
}

type Node = {
  id: string;
  parent: string | null;
  label: string;
  kind: "screen" | "action" | "link" | "miniapp" | "back";
  payload: string | null;
  custom_emoji_id: string | null;
  color: string | null;
  image_path: string | null;
  is_active: boolean;
  order_index?: number;
  row_index?: number;
};

const SWATCHES = ["", "#31A24C", "#2E63E7", "#E53935", "#F59E0B", "#7C5CFF", "#111111"];

let nextId = 1;
/* Buttons of a built-in bot SCREEN (Подключение, История, Баланс, …). Same shape as the cabinet
   editor, but there are many screens, picked from a dropdown, and saved to SCREEN_BUTTONS. */
type ScrBtn = {
  key: string;
  label: string;
  default_label: string | null;
  color: string | null;
  icon: string | null;
  enabled: boolean;
  custom: boolean;
  action: string | null;
  url: string | null;
  row?: number | null;
};
type Screen = {
  key: string;
  title_ru: string;
  title_en: string;
  buttons: ScrBtn[];
  per_row: number;
  layout: string;
  text?: string;
  text_default?: string;
  text_placeholders?: { token: string; desc: string }[];
  text_sample?: Record<string, string>;
  buttons_default?: ScrBtn[];
  per_row_default?: number;
  layout_default?: string;
};
type ScrWork = { buttons: ScrBtn[]; per_row: number; layout: string };

let scrSeq = 0;
function ScreenCard({
  screen,
  actions,
  actionLabel,
  open,
  onToggle,
}: {
  screen: Screen;
  actions: CabAction[];
  actionLabel: (code: string) => string;
  open: boolean;
  onToggle: () => void;
}) {
  const { t, lang, toast, confirm } = useApp();
  const qc = useQueryClient();

  const [workState, setWorkState] = useState<ScrWork | null>(null);
  const [textState, setTextState] = useState<string | null>(null);
  const [selKey, setSelKey] = useState<string | null>(null);
  const [dragKey, setDragKey] = useState<string | null>(null);
  const [overKey, setOverKey] = useState<string | null>(null);
  const [emojiId, setEmojiId] = useState("");
  const [emojiChar, setEmojiChar] = useState("🙂");
  const textRef = useRef<HTMLTextAreaElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  // On open, bring the whole screen editor to the top of the viewport.
  useEffect(() => {
    if (open) cardRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [open]);

  const key = screen.key;
  const dirty = workState != null || textState != null;
  // Empty = the bot keeps building this screen's text itself; a filled box replaces it wholesale.
  // API sends text="" (not null) when there's no override; `??` won't fall through on "",
  // so use `||` to reach the stock default. Empty saved text ⇒ bot uses the default anyway.
  const caption = textState ?? (screen.text || screen.text_default) ?? "";
  const placeholders = screen.text_placeholders ?? [];
  const sample = screen.text_sample ?? {};
  // Substitute {tokens} from sample data so the preview matches what a user sees (the bot fills
  // the same tokens from live data). Unknown tokens stay literal — same as render_template.
  const previewHtml = caption.replace(
    /\{([A-Za-zА-Яа-яЁё0-9_]+)\}/g,
    (m: string, tok: string) => (tok in sample ? sample[tok] : m),
  );
  // Insert a token at the caret (or append) so chips/emoji drop where you're typing.
  function insertToken(token: string) {
    const el = textRef.current;
    const cur = caption;
    if (!el) return setTextState(cur + token);
    const a = el.selectionStart ?? cur.length;
    const b = el.selectionEnd ?? cur.length;
    const next = cur.slice(0, a) + token + cur.slice(b);
    setTextState(next);
    requestAnimationFrame(() => {
      el.focus();
      const pos = a + token.length;
      el.setSelectionRange(pos, pos);
    });
  }
  const work: ScrWork =
    workState ?? { buttons: screen.buttons, per_row: screen.per_row, layout: screen.layout };
  const list = work.buttons;
  const cols = work.per_row;
  const mode = work.layout;
  const sel = list.find((x) => x.key === selKey) ?? null;

  function setWork(next: ScrWork) {
    setWorkState(next);
  }
  // Restore this screen's stock button set into the editor (order, labels, per-row). Buttons
  // only, not the caption; the owner still hits Save to persist — mirrors the cabinet card.
  async function resetButtons() {
    if (
      !(await confirm(
        lang === "ru"
          ? "Вернуть стандартные кнопки этого экрана? Текущие заменятся."
          : "Restore this screen's default buttons? Current ones will be replaced.",
      ))
    )
      return;
    setWork({
      buttons: (screen.buttons_default ?? []).map((b) => ({ ...b })),
      per_row: screen.per_row_default ?? 1,
      layout: screen.layout_default ?? "uniform",
    });
    setSelKey(null);
  }
  function update(next: ScrBtn[]) {
    setWork({ ...work, buttons: next });
  }
  function patch(k: string, p: Partial<ScrBtn>) {
    update(list.map((x) => (x.key === k ? { ...x, ...p } : x)));
  }
  function patchSel(p: Partial<ScrBtn>) {
    if (selKey) patch(selKey, p);
  }

  // Preview rows: only enabled buttons reach the bot; custom groups by `row`, uniform chunks `cols`.
  const shown = list.filter((b) => b.enabled);
  const previewRows: ScrBtn[][] = [];
  if (mode === "custom") {
    let cur: number | null = null;
    let started = false;
    for (const b of shown) {
      const r = typeof b.row === "number" ? b.row : 0;
      if (!started || r !== cur || previewRows[previewRows.length - 1].length >= 3) {
        previewRows.push([]);
        cur = r;
        started = true;
      }
      previewRows[previewRows.length - 1].push(b);
    }
  } else {
    for (let i = 0; i < shown.length; i += cols) previewRows.push(shown.slice(i, i + cols));
  }

  function commitRows(rows: string[][]) {
    const rowOf = new Map<string, number>();
    rows.forEach((r, ri) => r.forEach((k) => rowOf.set(k, ri)));
    const flat = rows.flat();
    const enabled = flat.map((k) => ({ ...list.find((x) => x.key === k)!, row: rowOf.get(k)! }));
    const disabled = list.filter((b) => !b.enabled).map((b) => ({ ...b, row: null }));
    update([...enabled, ...disabled]);
  }
  function swapKeys(a: string, b: string) {
    commitRows(previewRows.map((r) => r.map((x) => (x.key === a ? b : x.key === b ? a : x.key))));
  }
  function moveFlat(delta: number) {
    if (!sel) return;
    const flat = previewRows.flat();
    const i = flat.findIndex((b) => b.key === sel.key);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= flat.length) return;
    swapKeys(flat[i].key, flat[j].key);
  }
  function moveVert(dir: number) {
    if (!sel) return;
    let ri = -1;
    let ci = -1;
    previewRows.forEach((r, r2) => r.forEach((b, c2) => (b.key === sel.key ? ((ri = r2), (ci = c2)) : 0)));
    if (ri < 0) return;
    const tr = ri + dir;
    if (tr < 0 || tr >= previewRows.length) return;
    const tc = Math.min(ci, previewRows[tr].length - 1);
    swapKeys(sel.key, previewRows[tr][tc].key);
  }
  function enterCustom() {
    const seed = previewRows.map((r) => r.map((b) => b.key));
    // stamp rows off the seed
    const rowOf = new Map<string, number>();
    seed.forEach((r, ri) => r.forEach((k) => rowOf.set(k, ri)));
    const flat = seed.flat();
    const enabled = flat.map((k) => ({ ...list.find((x) => x.key === k)!, row: rowOf.get(k)! }));
    const disabled = list.filter((b) => !b.enabled).map((b) => ({ ...b, row: null }));
    setWork({ ...work, layout: "custom", buttons: [...enabled, ...disabled] });
  }
  // Default every screen's layout to «Свои»: on first load, if the screen isn't already custom,
  // seed custom rows from the current arrangement so the toggle starts on Custom without moving
  // any button. Mirrors the cabinet card. Runs once per mount (workState untouched).
  const scrSeeded = useRef(false);
  useEffect(() => {
    if (scrSeeded.current || workState !== null) return;
    scrSeeded.current = true;
    if ((screen.layout ?? "uniform") !== "custom") enterCustom();
  }, [screen, workState]);
  function reorder(from: string | null, to: string | null) {
    setDragKey(null);
    setOverKey(null);
    if (!from || !to || from === to) return;
    const next = [...list];
    const fi = next.findIndex((x) => x.key === from);
    const ti = next.findIndex((x) => x.key === to);
    if (fi < 0 || ti < 0) return;
    const [moved] = next.splice(fi, 1);
    next.splice(ti, 0, moved);
    update(next);
  }
  function remove(k: string) {
    update(list.filter((x) => x.key !== k));
    if (selKey === k) setSelKey(null);
  }
  function addCustom() {
    const used = new Set(
      list
        .filter((b) => b.enabled)
        .map((b) => (b.custom ? b.action : b.key) ?? "")
        .filter(Boolean),
    );
    const code = actions.find((a) => !used.has(a.code))?.code ?? actions[0]?.code ?? "";
    const next: ScrBtn = {
      key: `c${++scrSeq}_${Date.now().toString(36)}`,
      label: "",
      default_label: null,
      color: null,
      icon: null,
      enabled: true,
      custom: true,
      action: code,
      url: null,
    };
    update([...list, next]);
    setSelKey(next.key);
  }
  async function save() {
    for (const b of list) {
      if (b.custom && !b.label.trim()) return toast(lang === "ru" ? "У своей кнопки нужен текст" : "Custom button needs a label");
      if (b.custom && !b.action) return toast(lang === "ru" ? "У своей кнопки нужно действие" : "Custom button needs an action");
    }
    // One action per screen: two buttons pointing at the same action would collide in the bot.
    const seenAct = new Set<string>();
    for (const b of list) {
      if (!b.enabled) continue;
      const code = b.custom ? b.action : b.key;
      if (!code) continue;
      if (seenAct.has(code))
        return toast(
          lang === "ru"
            ? `Действие «${actionLabel(code)}» уже используется другой кнопкой`
            : `Action “${actionLabel(code)}” is already used by another button`,
        );
      seenAct.add(code);
    }
    const payload: Record<string, unknown> = {
      per_row: cols,
      layout: mode,
      items: list.map((b) => ({
        key: b.key,
        label: b.label,
        enabled: b.enabled,
        custom: b.custom,
        icon: b.icon || undefined,
        color: b.color || undefined,
        action: b.custom ? b.action : undefined,
        row: mode === "custom" && typeof b.row === "number" ? b.row : undefined,
      })),
    };
    if (textState != null) payload.text = textState;
    try {
      await api.put(`/api/admin/bot-menu/screens/${key}`, payload);
      setWorkState(null);
      setTextState(null);
      setSelKey(null);
      void qc.invalidateQueries({ queryKey: ["screen-buttons"] });
      toast(t.saved);
    } catch (e) {
      toast((e as Error).message);
    }
  }

  return (
    <div ref={cardRef} style={{ marginTop: 24, border: "1px solid var(--line, #2b2d33)", borderRadius: 14, padding: "6px 18px 18px" }}>
      <div className="page-head">
        <h1
          className="h1"
          style={{ cursor: "pointer", userSelect: "none" }}
          onClick={onToggle}
        >
          {open ? "▾" : "▸"} {lang === "ru" ? screen.title_ru : screen.title_en}
          {dirty && <span style={{ color: "#3b82f6" }}> •</span>}
        </h1>
      </div>

      {open && (
        <>
          <div
            className="row"
            style={{ marginBottom: 14, justifyContent: "space-between", alignItems: "center", gap: 8 }}
          >
            <span className="dim" style={{ fontSize: 12 }}>
              {lang === "ru"
                ? "Правь встроенные кнопки экрана: текст, цвет, порядок, видимость — или добавь свои."
                : "Edit this screen's built-in buttons — text, color, order, visibility — or add your own."}
            </span>
          </div>

          <div className="card" style={{ marginBottom: 14 }}>
            <div className="caps" style={{ marginBottom: 10 }}>
              {lang === "ru" ? "Текст экрана" : "Screen text"}
            </div>
            <div className="cols" style={{ alignItems: "stretch" }}>
              {/* editor */}
              <div style={{ flex: "1 1 320px", minWidth: 0 }}>
                <div className="dim" style={{ fontSize: 12, marginBottom: 6 }}>
                  {lang === "ru" ? "Текст:" : "Text:"}
                </div>
                <textarea
                  ref={textRef}
                  className="input"
                  rows={7}
                  value={caption}
                  placeholder={
                    lang === "ru"
                      ? "Пусто — бот показывает свой обычный текст этого экрана. Заполни, чтобы заменить его целиком."
                      : "Empty = the bot keeps its own text for this screen. Fill it in to replace it wholesale."
                  }
                  onChange={(e) => setTextState(e.target.value)}
                  style={{ fontFamily: "inherit", resize: "vertical", width: "100%" }}
                />
                {placeholders.length > 0 && (
                  <>
                    <div className="dim" style={{ fontSize: 12, margin: "10px 0 6px" }}>
                      {lang === "ru"
                        ? "Метки (клик — вставить в текст):"
                        : "Tokens (click to insert):"}
                    </div>
                    <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                      {placeholders.map((ph) => (
                        <button
                          key={ph.token}
                          className="btn secondary sm"
                          title={ph.desc}
                          onClick={() => insertToken(`{${ph.token}}`)}
                          style={{ fontFamily: "monospace" }}
                        >
                          {`{${ph.token}}`}
                        </button>
                      ))}
                    </div>
                  </>
                )}
                <div className="dim" style={{ fontSize: 12, margin: "12px 0 6px" }}>
                  {lang === "ru" ? "Кастом-эмодзи в тексте:" : "Custom emoji in text:"}
                </div>
                <div className="row" style={{ gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                  <input
                    className="input mono"
                    value={emojiId}
                    placeholder="custom_emoji_id"
                    onChange={(e) => setEmojiId(e.target.value.replace(/\D/g, ""))}
                    style={{ width: 180 }}
                  />
                  <input
                    className="input"
                    value={emojiChar}
                    onChange={(e) => setEmojiChar(e.target.value)}
                    style={{ width: 60 }}
                  />
                  <button
                    className="btn secondary sm"
                    disabled={!emojiId}
                    onClick={() =>
                      insertToken(
                        `<tg-emoji emoji-id="${emojiId}">${emojiChar || "🙂"}</tg-emoji>`,
                      )
                    }
                  >
                    {lang === "ru" ? "＋ эмодзи" : "＋ emoji"}
                  </button>
                </div>
                <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>
                  {lang === "ru"
                    ? "ID премиум-эмодзи + запасной символ (виден, если эмодзи недоступно)."
                    : "Premium emoji id + fallback char (shown if the emoji is unavailable)."}
                </div>
              </div>
              {/* live preview */}
              <div style={{ flex: "1 1 300px", minWidth: 0 }}>
                <div className="dim" style={{ fontSize: 12, marginBottom: 6 }}>
                  {lang === "ru" ? "Превью:" : "Preview:"}
                </div>
                <div
                  style={{
                    border: "1px solid var(--line, #333)",
                    borderRadius: 10,
                    padding: "12px 14px",
                    background: "var(--panel, #17181c)",
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-word",
                    lineHeight: 1.5,
                    minHeight: 120,
                  }}
                  dangerouslySetInnerHTML={{ __html: previewHtml }}
                />
                <div className="row" style={{ justifyContent: "flex-end", marginTop: 8 }}>
                  <button
                    className="btn secondary"
                    title={
                      lang === "ru"
                        ? "Вернуть стандартный текст экрана"
                        : "Restore the stock screen text"
                    }
                    onClick={async () => {
                      if (
                        !(await confirm(
                          lang === "ru"
                            ? "Сбросить текст экрана к стандартному?"
                            : "Reset screen text to default?",
                        ))
                      )
                        return;
                      setTextState(screen.text_default ?? "");
                    }}
                    style={{ fontSize: 15, padding: "10px 18px" }}
                  >
                    {lang === "ru" ? "↺ Сбросить к стандартному" : "↺ Reset to default"}
                  </button>
                </div>
              </div>
            </div>
            <div className="dim" style={{ fontSize: 12, marginTop: 10 }}>
              {lang === "ru"
                ? "⚠️ У экранов с живыми данными (подписка, оплата, трафик, устройства) заполненный текст заменит эти данные — оставь пусто, если не уверен."
                : "⚠️ On screens with live data (subscription, payment, traffic, devices) a filled text replaces that data — leave empty if unsure."}
            </div>
          </div>

          <div className="cols">
            {/* tree */}
            <div className="card" style={{ flex: "1 1 260px" }}>
              <div className="caps" style={{ marginBottom: 10 }}>
                {t.menuTree}
              </div>
              <div className="grid" style={{ gap: 6 }}>
                {list.map((b) => (
                  <div
                    key={b.key}
                    className="row click"
                    draggable
                    onDragStart={() => setDragKey(b.key)}
                    onDragEnd={() => {
                      setDragKey(null);
                      setOverKey(null);
                    }}
                    onDragOver={(e) => {
                      if (!dragKey) return;
                      e.preventDefault();
                      if (overKey !== b.key) setOverKey(b.key);
                    }}
                    onDrop={() => reorder(dragKey, b.key)}
                    onClick={() => setSelKey(b.key)}
                    style={{
                      padding: "8px 10px",
                      cursor: "pointer",
                      gap: 8,
                      background: b.key === selKey ? "var(--pill)" : "var(--panel2)",
                      border:
                        b.key === selKey ? "1px solid var(--muted)" : "1px solid var(--border)",
                      borderRadius: 6,
                      opacity: dragKey === b.key ? 0.4 : b.enabled ? 1 : 0.55,
                      outline:
                        overKey === b.key && dragKey && dragKey !== b.key
                          ? "2px solid #3b82f6"
                          : "none",
                    }}
                  >
                    <span
                      style={{
                        width: 10,
                        height: 10,
                        borderRadius: "50%",
                        background: b.color || "var(--border2)",
                        flex: "0 0 auto",
                      }}
                    />
                    <span
                      style={{
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {b.label || b.default_label || (lang === "ru" ? "Кнопка" : "Button")}
                    </span>
                    {b.icon && <span className="dim">◈</span>}
                    <span className="cap-pill dim" style={{ marginLeft: "auto" }}>
                      {b.custom
                        ? actionLabel(b.action ?? "")
                        : lang === "ru"
                        ? "встроенная"
                        : "built-in"}
                    </span>
                  </div>
                ))}
              </div>
              <button
                className="btn secondary"
                style={{ marginTop: 12, width: "100%" }}
                onClick={addCustom}
                disabled={!actions.length}
              >
                + {lang === "ru" ? "Своя кнопка" : "Custom button"}
              </button>
            </div>

            {/* editor */}
            <div className="card" style={{ flex: "1 1 300px" }}>
              {sel ? (
                <div className="grid" style={{ gap: 14 }}>
                  <div className="row" style={{ gap: 4 }}>
                    <button
                      className="btn secondary sm"
                      title={lang === "ru" ? "Влево" : "Left"}
                      onClick={() => moveFlat(-1)}
                    >
                      ←
                    </button>
                    <button
                      className="btn secondary sm"
                      title={lang === "ru" ? "Вправо" : "Right"}
                      onClick={() => moveFlat(1)}
                    >
                      →
                    </button>
                    <button
                      className="btn secondary sm"
                      title={lang === "ru" ? "Вверх (на ряд)" : "Up a row"}
                      onClick={() => moveVert(-1)}
                    >
                      ↑
                    </button>
                    <button
                      className="btn secondary sm"
                      title={lang === "ru" ? "Вниз (на ряд)" : "Down a row"}
                      onClick={() => moveVert(1)}
                    >
                      ↓
                    </button>
                    {sel.custom && (
                      <button
                        className="btn danger sm"
                        style={{ marginLeft: "auto" }}
                        onClick={() => remove(sel.key)}
                      >
                        ✕ {t.delete}
                      </button>
                    )}
                  </div>
                  <Field label={lang === "ru" ? "Показывать" : "Show"}>
                    <Toggle on={sel.enabled} onChange={(v) => patchSel({ enabled: v })} />
                  </Field>
                  <Field label={t.buttonText}>
                    <input
                      className="input"
                      value={sel.label}
                      placeholder={sel.default_label ?? (lang === "ru" ? "Текст" : "Label")}
                      onChange={(e) => patchSel({ label: e.target.value })}
                    />
                  </Field>
                  {sel.custom ? (
                    <Field label={t.cabinetBtnAction}>
                      <select
                        className="input"
                        value={sel.action ?? ""}
                        onChange={(e) => patchSel({ action: e.target.value })}
                      >
                        {actions
                          .filter(
                            (a) =>
                              a.code === sel.action ||
                              !list.some(
                                (b) =>
                                  b.key !== sel.key &&
                                  b.enabled &&
                                  (b.custom ? b.action : b.key) === a.code,
                              ),
                          )
                          .map((a) => (
                            <option key={a.code} value={a.code}>
                              {lang === "ru" ? a.label_ru : a.label_en}
                            </option>
                          ))}
                      </select>
                    </Field>
                  ) : (
                    <Field label={t.cabinetBtnAction}>
                      <span className="cap-pill dim">
                        {lang === "ru" ? "встроенная кнопка" : "built-in button"}
                      </span>
                    </Field>
                  )}
                  <Field label={t.buttonColor}>
                    <div className="row" style={{ flexWrap: "wrap" }}>
                      {SWATCHES.map((c) => (
                        <button
                          key={c || "none"}
                          title={c || "—"}
                          onClick={() => patchSel({ color: c || null })}
                          style={{
                            width: 24,
                            height: 24,
                            borderRadius: 3,
                            cursor: "pointer",
                            background: c || "transparent",
                            border:
                              (sel.color ?? "") === c
                                ? "2px solid var(--text)"
                                : "1px solid var(--border2)",
                          }}
                        />
                      ))}
                      <input
                        className="input mono"
                        style={{ width: 100 }}
                        placeholder="#31A24C"
                        value={sel.color ?? ""}
                        onChange={(e) => patchSel({ color: e.target.value || null })}
                      />
                    </div>
                  </Field>
                  <Field label={t.customEmoji}>
                    <input
                      className="input mono"
                      value={sel.icon ?? ""}
                      placeholder="5368324170671202286"
                      onChange={(e) =>
                        patchSel({ icon: e.target.value.replace(/[^0-9]/g, "") || null })
                      }
                    />
                  </Field>
                </div>
              ) : (
                <span className="dim">← {t.menuTree}</span>
              )}
            </div>

            {/* live preview */}
            <div className="card" style={{ flex: "1 1 300px" }}>
              <div className="caps" style={{ marginBottom: 10 }}>
                {t.livePreview}
              </div>
              {shown.length >= 1 && (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    flexWrap: "wrap",
                    padding: "10px 12px",
                    marginBottom: 12,
                    background: "var(--panel2)",
                    border: "1px solid var(--border)",
                    borderRadius: 8,
                  }}
                >
                  <span style={{ fontSize: 13, fontWeight: 600 }}>{t.perRow}:</span>
                  <div className="row" style={{ gap: 6 }}>
                    {[1, 2, 3].map((w) => (
                      <button
                        key={w}
                        className={`btn ${mode === "uniform" && cols === w ? "primary" : "secondary"}`}
                        style={{ minWidth: 42, padding: "6px 12px", fontWeight: 700 }}
                        onClick={() => setWork({ ...work, layout: "uniform", per_row: w })}
                      >
                        {w}
                      </button>
                    ))}
                    <button
                      className={`btn ${mode === "custom" ? "primary" : "secondary"}`}
                      style={{ padding: "6px 14px", fontWeight: 700 }}
                      onClick={enterCustom}
                    >
                      {lang === "ru" ? "Свои" : "Custom"}
                    </button>
                  </div>
                  <span className="dim" style={{ fontSize: 12, flexBasis: "100%" }}>
                    {mode === "custom"
                      ? lang === "ru"
                        ? "Перетаскивай кнопки: на другую — в её ряд, в пустое место — новый ряд."
                        : "Drag a button onto another to join its row, or into a gap for a new row."
                      : t.perRowHint}
                  </span>
                </div>
              )}
              <div
                style={{
                  background: "var(--panel2)",
                  border: "1px solid var(--border)",
                  borderRadius: 8,
                  padding: 14,
                }}
              >
                {mode === "custom" ? (
                  <CustomRows
                    rows={previewRows.map((r) =>
                      r.map((b) => ({
                        id: b.key,
                        label: b.label || b.default_label || "",
                        color: b.color,
                        selected: b.key === selKey,
                      })),
                    )}
                    onSelect={setSelKey}
                    onCommit={commitRows}
                    hint={
                      lang === "ru"
                        ? "До 3 кнопок в ряд — как в Telegram."
                        : "Up to 3 buttons per row — matches Telegram."
                    }
                  />
                ) : (
                  <div className="grid" style={{ gap: 6 }}>
                    {previewRows.map((row, ri) => (
                      <div key={ri} className="row" style={{ gap: 6 }}>
                        {row.map((b) => (
                          <button
                            key={b.key}
                            onClick={() => setSelKey(b.key)}
                            title={b.label}
                            style={{
                              flex: "1 1 0",
                              minWidth: 0,
                              borderRadius: 6,
                              border:
                                b.key === selKey
                                  ? "1px solid var(--text)"
                                  : "1px solid var(--border2)",
                              background: b.color || "var(--panel)",
                              color: b.color ? "#fff" : "var(--text)",
                              padding: "9px 12px",
                              fontSize: 13,
                              cursor: "pointer",
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                              whiteSpace: "nowrap",
                            }}
                          >
                            {b.label || b.default_label || ""}
                          </button>
                        ))}
                      </div>
                    ))}
                    {shown.length === 0 && <span className="dim">—</span>}
                  </div>
                )}
              </div>
            </div>
          </div>
          <div className="row" style={{ justifyContent: "flex-end", marginTop: 12, gap: 8 }}>
            <button
              className="btn secondary"
              title={
                lang === "ru"
                  ? "Вернуть стандартный набор кнопок экрана"
                  : "Restore the screen's stock button set"
              }
              onClick={resetButtons}
            >
              {lang === "ru" ? "↺ Сбросить кнопки" : "↺ Reset buttons"}
            </button>
            <button className="btn primary" onClick={save} disabled={!dirty}>
              {lang === "ru" ? "Сохранить" : "Save"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function ScreenButtonsCard({
  openKey,
  setOpenKey,
}: {
  openKey: string | null;
  setOpenKey: Dispatch<SetStateAction<string | null>>;
}) {
  const { lang } = useApp();
  const q = useQuery({
    queryKey: ["screen-buttons"],
    queryFn: () => api.get<{ screens: Screen[] }>("/api/admin/bot-menu/screens"),
  });
  const aq = useQuery({
    queryKey: ["bot-menu-actions"],
    queryFn: () => api.get<{ actions: CabAction[] }>("/api/admin/bot-menu/actions"),
  });
  const actions = aq.data?.actions ?? [];
  const actionLabel = (code: string) => {
    const a = actions.find((x) => x.code === code);
    if (!a) return code;
    return lang === "ru" ? a.label_ru : a.label_en;
  };
  const screens = q.data?.screens ?? [];
  return (
    <>
      {screens.map((s) => (
        <ScreenCard
          key={s.key}
          screen={s}
          actions={actions}
          actionLabel={actionLabel}
          open={openKey === s.key}
          onToggle={() => setOpenKey((k) => (k === s.key ? null : s.key))}
        />
      ))}
    </>
  );
}

function genId(): string {
  return `n${Date.now().toString(36)}${nextId++}`;
}

export default function BotButtons() {
  const { t, lang, toast, confirm } = useApp();
  const qc = useQueryClient();
  const [nodes, setNodes] = useState<Node[]>([]);
  const [selId, setSelId] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [uploading, setUploading] = useState(false);
  // One editor open at a time across the whole page (main menu, cabinet, every screen).
  // Opening one collapses the rest. Sentinels avoid clashing with real screen keys.
  const [openEditor, setOpenEditor] = useState<string | null>(null);
  const openMenu = openEditor === "__menu__";
  const menuCardRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (openMenu) menuCardRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [openMenu]);
  const [menuCustom, setMenuCustom] = useState(true);
  const [menuText, setMenuText] = useState<string | null>(null);
  const menuTextRef = useRef<HTMLTextAreaElement>(null);
  const [menuEmojiId, setMenuEmojiId] = useState("");
  const [menuEmojiChar, setMenuEmojiChar] = useState("🙂");
  const fileRef = useRef<HTMLInputElement>(null);
  const ioRef = useRef<HTMLInputElement>(null);

  async function uploadImage(f: File) {
    setUploading(true);
    try {
      const form = new FormData();
      form.append("file", f);
      const res = await fetch("/api/admin/upload", {
        method: "POST",
        headers: { Authorization: `Bearer ${getToken()}` },
        body: form,
      });
      if (!res.ok) throw new Error(await res.text());
      const data = (await res.json()) as { path: string };
      patchSel({ image_path: data.path });
      toast("✓");
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setUploading(false);
    }
  }

  async function exportMenu() {
    try {
      const dump = await api.get<{ nodes: unknown[] }>("/api/admin/bot-menu/export");
      const blob = new Blob([JSON.stringify(dump, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `stepbot-menu-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      toast(
        (lang === "ru" ? "Сохранено: кнопок " : "Saved: buttons ") +
          dump.nodes.length +
          (lang === "ru" ? " + настройки кабинета" : " + cabinet settings"),
      );
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function importMenu(file: File) {
    try {
      const parsed = JSON.parse(await file.text());
      const hasNodes = Array.isArray(parsed?.nodes);
      const hasCfg = parsed?.config && typeof parsed.config === "object";
      if (!hasNodes && !hasCfg) {
        throw new Error(lang === "ru" ? "Не похоже на файл меню" : "Not a bot-menu file");
      }
      const parts: string[] = [];
      if (hasNodes)
        parts.push((lang === "ru" ? "кнопок: " : "buttons: ") + parsed.nodes.length);
      if (hasCfg) parts.push(lang === "ru" ? "тексты меню и кабинета" : "menu + cabinet texts");
      const ok = await confirm(
        (lang === "ru"
          ? "Импортировать меню? Текущее будет заменено — "
          : "Import menu? Current will be replaced — ") + parts.join(", "),
      );
      if (!ok) return;
      const res = await api.post<{ nodes: number; applied: string[] }>(
        "/api/admin/bot-menu/import",
        { nodes: hasNodes ? parsed.nodes : null, config: hasCfg ? parsed.config : null },
      );
      setMenuText(null);
      setLoaded(false);
      await qc.invalidateQueries();
      toast(
        (lang === "ru" ? "Загружено ✔️ кнопок " : "Loaded ✔️ buttons ") +
          res.nodes +
          (lang === "ru"
            ? `, настроек ${res.applied.length}`
            : `, settings ${res.applied.length}`),
      );
    } catch (e) {
      toast((e as Error).message);
    }
  }

  const data = useQuery({
    queryKey: ["bot-menu"],
    queryFn: () =>
      api.get<{ nodes: Node[]; text?: string; text_default?: string }>("/api/admin/bot-menu"),
  });
  const actionsQ = useQuery({
    queryKey: ["bot-menu-actions"],
    queryFn: () => api.get<{ actions: CabAction[] }>("/api/admin/bot-menu/actions"),
  });
  const menuActions = actionsQ.data?.actions ?? [];
  // Menu caption = START_MESSAGE (plain HTML, no placeholders). Editable here with a default reset.
  const menuCaption = menuText ?? data.data?.text ?? "";
  function insertMenuToken(token: string) {
    const el = menuTextRef.current;
    const cur = menuCaption;
    if (!el) return setMenuText(cur + token);
    const a = el.selectionStart ?? cur.length;
    const b = el.selectionEnd ?? cur.length;
    const next = cur.slice(0, a) + token + cur.slice(b);
    setMenuText(next);
    requestAnimationFrame(() => {
      el.focus();
      const pos = a + token.length;
      el.setSelectionRange(pos, pos);
    });
  }

  useEffect(() => {
    if (data.data && !loaded) {
      setNodes(data.data.nodes);
      setLoaded(true);
    }
  }, [data.data, loaded]);

  const sel = nodes.find((n) => n.id === selId) ?? null;
  const kids = (parent: string | null) =>
    nodes
      .filter((n) => n.parent === parent)
      .sort(
        (a, b) =>
          (a.row_index ?? 0) - (b.row_index ?? 0) || (a.order_index ?? 0) - (b.order_index ?? 0),
      );

  // Current buttons-per-row of a screen = its widest row. A flat menu (every button on the same
  // row_index) renders stacked one-per-row in the bot, so it reads as width 1 here too.
  function rowWidth(parent: string | null): number {
    const siblings = kids(parent);
    if (new Set(siblings.map((n) => n.row_index ?? 0)).size <= 1) return 1;
    const counts = new Map<number, number>();
    for (const n of siblings) {
      const r = n.row_index ?? 0;
      counts.set(r, (counts.get(r) ?? 0) + 1);
    }
    return Math.max(1, ...counts.values());
  }

  // Re-flow a screen's buttons into rows of `perRow`, preserving their order. This stamps the
  // row_index the bot renders by (inline honours up to 3/row; the reply bottom-bar up to 2).
  function layoutRows(parent: string | null, perRow: number) {
    const seq = kids(parent);
    const pos = new Map(seq.map((n, i) => [n.id, i]));
    setNodes((ns) =>
      ns.map((n) => {
        const i = pos.get(n.id);
        if (i === undefined) return n;
        return { ...n, row_index: Math.floor(i / perRow), order_index: i };
      }),
    );
  }

  // Custom layout: write these rows (arrays of node ids, reading order) as the screen's layout,
  // stamping row_index (which buttons share a row) and order_index (position). Used by the free
  // drag-and-drop preview, so any 1-2-1-style arrangement is representable.
  function commitMenuRows(rows: string[][]) {
    const patch = new Map<string, Partial<Node>>();
    let order = 0;
    rows.forEach((r, ri) => r.forEach((id) => patch.set(id, { row_index: ri, order_index: order++ })));
    setNodes((ns) => ns.map((n) => (patch.has(n.id) ? { ...n, ...patch.get(n.id)! } : n)));
  }

  function patchSel(patch: Partial<Node>) {
    if (!selId) return;
    setNodes((ns) => ns.map((n) => (n.id === selId ? { ...n, ...patch } : n)));
  }

  function addNode() {
    // Child of the selected screen (or of its parent when selected is not a screen).
    let parent: string | null = null;
    if (sel) parent = sel.kind === "screen" ? sel.id : sel.parent;
    const node: Node = {
      id: genId(),
      parent,
      label: "Новая кнопка",
      kind: "action",
      payload: null,
      custom_emoji_id: null,
      color: null,
      image_path: null,
      is_active: true,
      order_index: kids(parent).length,
    };
    setNodes((ns) => [...ns, node]);
    setSelId(node.id);
  }

  async function removeSel() {
    if (!sel) return;
    if (!(await confirm(t.deleteNodeConfirm))) return;
    // Collect the whole subtree.
    const doomed = new Set<string>([sel.id]);
    let grew = true;
    while (grew) {
      grew = false;
      for (const n of nodes) {
        if (n.parent && doomed.has(n.parent) && !doomed.has(n.id)) {
          doomed.add(n.id);
          grew = true;
        }
      }
    }
    setNodes((ns) => ns.filter((n) => !doomed.has(n.id)));
    setSelId(null);
  }

  // Slot-preserving arrows: swap the selected button with a neighbour, keeping the row SHAPE
  // fixed so the layout never snaps back to a uniform preset. ←/→ step through reading order;
  // ↑/↓ swap with the same column one row up/down. (Reshaping rows is done by drag-and-drop.)
  function swapSlots(a: string, b: string) {
    setNodes((ns) => {
      const na = ns.find((n) => n.id === a);
      const nb = ns.find((n) => n.id === b);
      if (!na || !nb) return ns;
      const ra = na.row_index ?? 0;
      const oa = na.order_index ?? 0;
      const rb = nb.row_index ?? 0;
      const ob = nb.order_index ?? 0;
      return ns.map((n) =>
        n.id === a
          ? { ...n, row_index: rb, order_index: ob }
          : n.id === b
          ? { ...n, row_index: ra, order_index: oa }
          : n,
      );
    });
  }
  function moveFlat(delta: number) {
    if (!sel) return;
    const flat = previewRows.flat();
    const i = flat.findIndex((n) => n.id === sel.id);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= flat.length) return;
    swapSlots(flat[i].id, flat[j].id);
  }
  function moveVert(dir: number) {
    if (!sel) return;
    let ri = -1;
    let ci = -1;
    previewRows.forEach((r, r2) => r.forEach((n, c2) => (n.id === sel.id ? ((ri = r2), (ci = c2)) : 0)));
    if (ri < 0) return;
    const tr = ri + dir;
    if (tr < 0 || tr >= previewRows.length) return;
    const tc = Math.min(ci, previewRows[tr].length - 1);
    swapSlots(sel.id, previewRows[tr][tc].id);
  }

  async function save() {
    try {
      const payload = nodes.map((n, i) => ({
        id: n.id,
        parent: n.parent,
        label: n.label,
        kind: n.kind,
        payload: n.payload,
        custom_emoji_id: n.custom_emoji_id,
        color: n.color,
        image_path: n.image_path,
        is_active: n.is_active,
        // Persist BOTH layout axes so reordering sticks: order_index (position among
        // siblings, set by move()) and row_index (which buttons share a row). Without
        // order_index the server fell back to array/creation order and reorders snapped back.
        row_index: n.row_index ?? 0,
        order_index: n.order_index ?? i,
      }));
      const res = await api.put<{ nodes: Node[] }>("/api/admin/bot-menu", {
        nodes: payload,
        text: menuCaption,
      });
      setNodes(res.nodes);
      setSelId(null);
      setMenuText(null);
      void qc.invalidateQueries({ queryKey: ["bot-menu"] });
      toast(t.saved);
    } catch (e) {
      toast((e as Error).message);
    }
  }
  // Restore the built-in default menu (replaces the tree via /reset-default), then reload it into
  // the editor. Buttons only — the caption text has its own «Сбросить к стандартному».
  async function resetMenuButtons() {
    if (
      !(await confirm(
        lang === "ru"
          ? "Вернуть стандартное меню бота? Текущие кнопки заменятся."
          : "Restore the default bot menu? Current buttons will be replaced.",
      ))
    )
      return;
    try {
      await api.post("/api/admin/bot-menu/reset-default", {});
      const res = await api.get<{ nodes: Node[] }>("/api/admin/bot-menu");
      setNodes(res.nodes);
      setSelId(null);
      void qc.invalidateQueries({ queryKey: ["bot-menu"] });
      toast(t.saved);
    } catch (e) {
      toast((e as Error).message);
    }
  }

  // Preview: the screen owning the selected node (or root).
  const previewScreenId = useMemo(() => {
    if (!sel) return null;
    return sel.kind === "screen" ? sel.id : sel.parent;
  }, [sel]);
  const previewButtons = kids(previewScreenId);
  // Show the exact grid the bot renders. With no deliberate layout (every button on the same
  // row_index) the bot stacks one per row, so the preview does too; once «В ряд 2/3» assigns
  // distinct row_index values, group buttons by that so what you see is what the bot shows.
  const previewRows = useMemo(() => {
    const deliberate = new Set(previewButtons.map((b) => b.row_index ?? 0)).size > 1;
    if (!deliberate) return previewButtons.map((b) => [b]); // stacked, one per row
    const out: Node[][] = [];
    let cur: number | null = null;
    for (const b of previewButtons) {
      const r = b.row_index ?? 0;
      if (out.length === 0 || r !== cur) {
        out.push([]);
        cur = r;
      }
      out[out.length - 1].push(b);
    }
    return out;
  }, [previewButtons]);
  // The 1/2/3 presets are "uniform" (all rows equal width bar the last). Anything else — or the
  // «Свои» toggle — is treated as a custom layout so the arrows/DnD don't snap it to a preset.
  const previewUniform = useMemo(() => {
    const lens = previewRows.map((r) => r.length);
    if (lens.length <= 1) return true;
    const w = Math.max(...lens);
    return lens.slice(0, -1).every((l) => l === w);
  }, [previewRows]);
  const customActive = menuCustom || !previewUniform;

  function TreeRow({ node, depth }: { node: Node; depth: number }) {
    const children = kids(node.id);
    return (
      <div style={{ position: "relative" }}>
        <div
          className="row click"
          style={{
            padding: "8px 10px",
            cursor: "pointer",
            background: node.id === selId ? "var(--pill)" : "var(--panel2)",
            border:
              node.id === selId ? "1px solid var(--muted)" : "1px solid var(--border)",
            borderRadius: 6,
            marginBottom: 6,
          }}
          onClick={() => setSelId(node.id)}
        >
          <span
            style={{
              width: 10,
              height: 10,
              borderRadius: node.kind === "screen" ? 2 : "50%",
              background: node.color || "var(--border2)",
              flex: "0 0 auto",
            }}
          />
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {node.label}
          </span>
          {node.image_path && <span title="картинка экрана">🖼</span>}
          {node.custom_emoji_id && <span className="dim">◈</span>}
          <span className="cap-pill" style={{ marginLeft: "auto" }}>
            {kindLabel(node.kind)}
          </span>
        </div>
        {children.length > 0 && (
          <div
            style={{
              marginLeft: 14,
              paddingLeft: 16,
              borderLeft: "2px solid var(--border2)",
            }}
          >
            {children.map((c) => (
              <div key={c.id} style={{ position: "relative" }}>
                <span
                  style={{
                    position: "absolute",
                    left: -16,
                    top: 17,
                    width: 12,
                    height: 2,
                    background: "var(--border2)",
                  }}
                />
                <TreeRow node={c} depth={depth + 1} />
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  function kindLabel(kind: Node["kind"]): string {
    return {
      screen: t.kindScreen,
      action: t.kindAction,
      link: t.kindLink,
      miniapp: t.kindMiniapp,
      back: t.kindBack,
    }[kind];
  }

  return (
    <>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginBottom: 10 }}>
        <input
          ref={ioRef}
          type="file"
          accept="application/json,.json"
          style={{ display: "none" }}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void importMenu(f);
            e.target.value = "";
          }}
        />
        <button className="btn secondary" onClick={() => void exportMenu()}>
          {lang === "ru" ? "💾 Сохранить" : "💾 Save"}
        </button>
        <button className="btn secondary" onClick={() => ioRef.current?.click()}>
          {lang === "ru" ? "📂 Загрузить" : "📂 Load"}
        </button>
      </div>
      <div ref={menuCardRef} style={{ border: "1px solid var(--line, #2b2d33)", borderRadius: 14, padding: "6px 18px 18px" }}>
      <div className="page-head">
        <h1
          className="h1"
          style={{ cursor: "pointer", userSelect: "none" }}
          onClick={() => setOpenEditor((k) => (k === "__menu__" ? null : "__menu__"))}
        >
          {openMenu ? "▾" : "▸"} {t.botButtons}
        </h1>
      </div>

      {openMenu && (
      <>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="caps" style={{ marginBottom: 10 }}>
          {lang === "ru" ? "Текст меню (приветствие /start)" : "Menu text (/start greeting)"}
        </div>
        <div className="cols" style={{ alignItems: "stretch" }}>
          {/* editor */}
          <div style={{ flex: "1 1 320px", minWidth: 0 }}>
            <div className="dim" style={{ fontSize: 12, marginBottom: 6 }}>
              {lang === "ru" ? "Текст:" : "Text:"}
            </div>
            <textarea
              ref={menuTextRef}
              className="input"
              rows={7}
              value={menuCaption}
              placeholder={lang === "ru" ? "Текст под баннером меню" : "Caption under the menu banner"}
              onChange={(e) => setMenuText(e.target.value)}
              style={{ fontFamily: "inherit", resize: "vertical", width: "100%" }}
            />
            <div className="dim" style={{ fontSize: 12, margin: "12px 0 6px" }}>
              {lang === "ru" ? "Кастом-эмодзи в тексте:" : "Custom emoji in text:"}
            </div>
            <div className="row" style={{ gap: 6, flexWrap: "wrap", alignItems: "center" }}>
              <input
                className="input mono"
                value={menuEmojiId}
                placeholder="custom_emoji_id"
                onChange={(e) => setMenuEmojiId(e.target.value.replace(/\D/g, ''))}
                style={{ width: 180 }}
              />
              <input
                className="input"
                value={menuEmojiChar}
                onChange={(e) => setMenuEmojiChar(e.target.value)}
                style={{ width: 60 }}
              />
              <button
                className="btn secondary sm"
                disabled={!menuEmojiId}
                onClick={() =>
                  insertMenuToken(
                    `<tg-emoji emoji-id="${menuEmojiId}">${menuEmojiChar || "🙂"}</tg-emoji>`,
                  )
                }
              >
                {lang === "ru" ? "＋ эмодзи" : "＋ emoji"}
              </button>
            </div>
            <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>
              {lang === "ru"
                ? "ID премиум-эмодзи + запасной символ (виден, если эмодзи недоступно)."
                : "Premium emoji id + fallback char (shown if the emoji is unavailable)."}
            </div>
          </div>
          {/* live preview */}
          <div style={{ flex: "1 1 300px", minWidth: 0 }}>
            <div className="dim" style={{ fontSize: 12, marginBottom: 6 }}>
              {lang === "ru" ? "Превью:" : "Preview:"}
            </div>
            <div
              style={{
                border: "1px solid var(--line, #333)",
                borderRadius: 10,
                padding: "12px 14px",
                background: "var(--panel, #17181c)",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                lineHeight: 1.5,
                minHeight: 120,
              }}
              dangerouslySetInnerHTML={{ __html: menuCaption }}
            />
            <div className="row" style={{ justifyContent: "flex-end", marginTop: 8 }}>
              <button
                className="btn secondary"
                title={lang === "ru" ? "Вернуть стандартное приветствие" : "Restore the stock greeting"}
                onClick={async () => {
                  if (
                    !(await confirm(
                      lang === "ru"
                        ? "Сбросить приветствие к стандартному?"
                        : "Reset greeting to default?",
                    ))
                  )
                    return;
                  setMenuText(data.data?.text_default ?? "");
                }}
                style={{ fontSize: 15, padding: "10px 18px" }}
              >
                {lang === "ru" ? "↺ Сбросить к стандартному" : "↺ Reset to default"}
              </button>
            </div>
          </div>
        </div>
      </div>
      <div className="cols">
        {/* tree */}
        <div className="card" style={{ flex: "1 1 280px" }}>
          <div className="caps" style={{ marginBottom: 10 }}>
            {t.menuTree}
          </div>
          <div className="grid" style={{ gap: 2 }}>
            {kids(null).map((n) => (
              <TreeRow key={n.id} node={n} depth={0} />
            ))}
          </div>
          <button className="btn secondary" style={{ marginTop: 12, width: "100%" }} onClick={addNode}>
            {t.addButton}
          </button>
        </div>

        {/* editor */}
        <div className="card" style={{ flex: "1 1 300px" }}>
          {sel ? (
            <div className="grid" style={{ gap: 14 }}>
              <div className="row" style={{ gap: 4 }}>
                <button
                  className="btn secondary sm"
                  title={lang === "ru" ? "Влево" : "Left"}
                  onClick={() => moveFlat(-1)}
                >
                  ←
                </button>
                <button
                  className="btn secondary sm"
                  title={lang === "ru" ? "Вправо" : "Right"}
                  onClick={() => moveFlat(1)}
                >
                  →
                </button>
                <button
                  className="btn secondary sm"
                  title={lang === "ru" ? "Вверх (на ряд)" : "Up a row"}
                  onClick={() => moveVert(-1)}
                >
                  ↑
                </button>
                <button
                  className="btn secondary sm"
                  title={lang === "ru" ? "Вниз (на ряд)" : "Down a row"}
                  onClick={() => moveVert(1)}
                >
                  ↓
                </button>
                <button className="btn danger sm" style={{ marginLeft: "auto" }} onClick={removeSel}>
                  ✕ {t.delete}
                </button>
              </div>
              <Field label={t.buttonText}>
                <input
                  className="input"
                  value={sel.label}
                  onChange={(e) => patchSel({ label: e.target.value })}
                />
              </Field>
              <Field label={t.buttonType}>
                <Seg
                  value={sel.kind}
                  options={(["screen", "action", "link", "miniapp"] as const).map((k) => ({
                    id: k,
                    label: kindLabel(k),
                  }))}
                  onChange={(kind) => patchSel({ kind })}
                />
              </Field>
              {sel.kind === "screen" && (
                <Field label={t.screenText}>
                  <textarea
                    className="input"
                    rows={4}
                    value={sel.payload ?? ""}
                    onChange={(e) => patchSel({ payload: e.target.value })}
                  />
                </Field>
              )}
              {sel.kind === "link" && (
                <Field label="URL">
                  <input
                    className="input mono"
                    value={sel.payload ?? ""}
                    placeholder="https://…"
                    onChange={(e) => patchSel({ payload: e.target.value })}
                  />
                </Field>
              )}
              {sel.kind === "action" && (
                <Field label="ACTION CODE">
                  <select
                    className="input"
                    value={sel.payload ?? ""}
                    onChange={(e) => patchSel({ payload: e.target.value })}
                  >
                    <option value="">—</option>
                    {sel.payload && !menuActions.some((a) => a.code === sel.payload) && (
                      <option value={sel.payload}>{sel.payload}</option>
                    )}
                    {menuActions
                      .filter(
                        (a) =>
                          a.code === sel.payload ||
                          !nodes.some(
                            (n) =>
                              n.id !== sel.id && n.kind === "action" && n.payload === a.code,
                          ),
                      )
                      .map((a) => (
                        <option key={a.code} value={a.code}>
                          {lang === "ru" ? a.label_ru : a.label_en}
                        </option>
                      ))}
                  </select>
                </Field>
              )}
              <Field label={t.buttonColor}>
                <div className="row" style={{ flexWrap: "wrap" }}>
                  {SWATCHES.map((c) => (
                    <button
                      key={c || "none"}
                      title={c || "—"}
                      onClick={() => patchSel({ color: c || null })}
                      style={{
                        width: 24,
                        height: 24,
                        borderRadius: 3,
                        cursor: "pointer",
                        background: c || "transparent",
                        border:
                          (sel.color ?? "") === c
                            ? "2px solid var(--text)"
                            : "1px solid var(--border2)",
                      }}
                    />
                  ))}
                  <input
                    className="input mono"
                    style={{ width: 100 }}
                    placeholder="#HEX"
                    value={sel.color ?? ""}
                    onChange={(e) => patchSel({ color: e.target.value || null })}
                  />
                </div>
              </Field>
              <Field label={t.customEmoji}>
                <input
                  className="input mono"
                  value={sel.custom_emoji_id ?? ""}
                  placeholder="5368324170671202286"
                  onChange={(e) => patchSel({ custom_emoji_id: e.target.value || null })}
                />
              </Field>
              {sel.kind === "screen" && (
                <Field label={t.screenImage}>
                  <div className="row" style={{ flexWrap: "wrap" }}>
                    <button
                      className="btn secondary sm"
                      disabled={uploading}
                      onClick={() => fileRef.current?.click()}
                    >
                      {uploading ? "…" : "🖼 " + t.uploadImage}
                    </button>
                    {sel.image_path && (
                      <>
                        <img
                          src={"/" + sel.image_path}
                          alt=""
                          style={{ height: 40, borderRadius: 4, border: "1px solid var(--border2)" }}
                        />
                        <button
                          className="btn danger sm"
                          onClick={() => patchSel({ image_path: null })}
                        >
                          ✕
                        </button>
                      </>
                    )}
                    <input
                      ref={fileRef}
                      type="file"
                      accept=".jpg,.jpeg,.png,.webp"
                      style={{ display: "none" }}
                      onChange={(e) => {
                        const f = e.target.files?.[0];
                        if (f) void uploadImage(f);
                        e.target.value = "";
                      }}
                    />
                  </div>
                </Field>
              )}
            </div>
          ) : (
            <span className="dim">← {t.menuTree}</span>
          )}
        </div>

        {/* live preview */}
        <div className="card" style={{ flex: "1 1 300px" }}>
          <div className="caps" style={{ marginBottom: 10 }}>
            {t.livePreview}
          </div>
          {previewButtons.length > 1 && (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                flexWrap: "wrap",
                padding: "10px 12px",
                marginBottom: 12,
                background: "var(--panel2)",
                border: "1px solid var(--border)",
                borderRadius: 8,
              }}
            >
              <span style={{ fontSize: 13, fontWeight: 600 }}>{t.perRow}:</span>
              <div className="row" style={{ gap: 6 }}>
                {[1, 2, 3].map((w) => (
                  <button
                    key={w}
                    className={`btn ${
                      !customActive && rowWidth(previewScreenId) === w ? "primary" : "secondary"
                    }`}
                    style={{ minWidth: 42, padding: "6px 12px", fontWeight: 700 }}
                    onClick={() => {
                      setMenuCustom(false);
                      layoutRows(previewScreenId, w);
                    }}
                  >
                    {w}
                  </button>
                ))}
                <button
                  className={`btn ${customActive ? "primary" : "secondary"}`}
                  style={{ padding: "6px 14px", fontWeight: 700 }}
                  onClick={() => setMenuCustom(true)}
                >
                  {lang === "ru" ? "Свои" : "Custom"}
                </button>
              </div>
              <span className="dim" style={{ fontSize: 12, flexBasis: "100%" }}>
                {customActive
                  ? lang === "ru"
                    ? "Перетаскивай кнопки: на другую — в её ряд, в пустое место — новый ряд."
                    : "Drag a button onto another to join its row, or into a gap for a new row."
                  : t.perRowHint}
              </span>
            </div>
          )}
          <div
            style={{
              background: "var(--panel2)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              padding: 14,
            }}
          >
            {customActive ? (
              <CustomRows
                rows={previewRows.map((r) =>
                  r.map((b) => ({ id: b.id, label: b.label, color: b.color, selected: b.id === selId })),
                )}
                onSelect={setSelId}
                onCommit={commitMenuRows}
                hint={
                  lang === "ru"
                    ? "До 3 кнопок в ряд — как в Telegram."
                    : "Up to 3 buttons per row — matches Telegram."
                }
              />
            ) : (
              <div className="grid" style={{ gap: 6 }}>
                {previewRows.map((row, ri) => (
                  <div key={ri} className="row" style={{ gap: 6 }}>
                    {row.map((b) => (
                      <button
                        key={b.id}
                        onClick={() => setSelId(b.id)}
                        title={b.label}
                        style={{
                          flex: "1 1 0",
                          minWidth: 0,
                          borderRadius: 6,
                          border:
                            b.id === selId ? "1px solid var(--text)" : "1px solid var(--border2)",
                          background: b.color || "var(--panel)",
                          color: b.color ? "#fff" : "var(--text)",
                          padding: "9px 12px",
                          fontSize: 13,
                          cursor: "pointer",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {b.label}
                      </button>
                    ))}
                  </div>
                ))}
                {previewButtons.length === 0 && <span className="dim">—</span>}
              </div>
            )}
          </div>
        </div>
      </div>
      <div className="row" style={{ justifyContent: "flex-end", marginTop: 12, gap: 8 }}>
        <button
          className="btn secondary"
          title={lang === "ru" ? "Вернуть стандартный набор кнопок меню" : "Restore the stock menu buttons"}
          onClick={resetMenuButtons}
        >
          {lang === "ru" ? "↺ Сбросить кнопки" : "↺ Reset buttons"}
        </button>
        <button className="btn primary" onClick={save}>
          {lang === "ru" ? "Сохранить" : "Save"}
        </button>
      </div>
      </>
      )}
      </div>
      <CabinetButtonsCard
        open={openEditor === "__cabinet__"}
        onToggle={() => setOpenEditor((k) => (k === "__cabinet__" ? null : "__cabinet__"))}
      />
      <ScreenButtonsCard openKey={openEditor} setOpenKey={setOpenEditor} />
    </>
  );
}
