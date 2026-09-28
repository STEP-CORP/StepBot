import { useEffect, useState } from "react";
import { useNavigate } from "react-router";

import { api, setToken } from "../api/client";
import { BrandLogo } from "../components/Layout";
import { useApp } from "../state/app";

export default function Login() {
  const { t } = useApp();
  const nav = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [demoEnabled, setDemoEnabled] = useState(false);

  useEffect(() => {
    api
      .get<{ enabled: boolean }>("/api/admin/auth/demo")
      .then((r) => setDemoEnabled(r.enabled))
      .catch(() => setDemoEnabled(false));
  }, []);

  async function demo() {
    setBusy(true);
    setError("");
    try {
      const res = await api.post<{ token: string }>("/api/admin/auth/demo");
      setToken(res.token);
      nav("/");
    } catch {
      setError(t.error);
    } finally {
      setBusy(false);
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const res = await api.post<{ token: string }>("/api/admin/auth/login", {
        username,
        password,
      });
      setToken(res.token);
      nav("/");
    } catch {
      setError(t.badCreds);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <BrandLogo size={22} />
        <span className="caps">STEPBOT CABINET</span>
        <input
          className="input"
          placeholder={t.username}
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoFocus
          autoComplete="username"
        />
        <input
          className="input"
          type="password"
          placeholder={t.password}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
        />
        {error && (
          <span className="caps" style={{ color: "var(--muted)" }}>
            ✕ {error}
          </span>
        )}
        <button className="btn primary" disabled={busy || !username || !password}>
          {t.login}
        </button>
        {demoEnabled && (
          <button type="button" className="btn secondary" disabled={busy} onClick={demo}>
            👀 {t.demoLogin}
          </button>
        )}
      </form>
    </div>
  );
}
