import { useEffect, useRef, useState } from "react";
import { login } from "../auth/auth.js";
import { getLoginBanner } from "../api/client.js";

const TURNSTILE_SITE_KEY =
  import.meta.env.VITE_TURNSTILE_SITE_KEY || "0x4AAAAAAE4jLGhJo1wHHnwA";

export default function Login({ onSuccess, notice = "" }) {
  const [banner, setBanner] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [turnstileToken, setTurnstileToken] = useState("");
  const turnstileContainer = useRef(null);
  const turnstileWidget = useRef(null);

  // The notice everyone sees before signing in (ADMIN_LOGIN_BANNER on the server).
  useEffect(() => {
    getLoginBanner().then((r) => setBanner(r?.banner || "")).catch(() => setBanner(""));
  }, []);

  // The Turnstile script is loaded async from index.html with render=explicit,
  // so it may not be ready on first mount: render now or once it loads.
  useEffect(() => {
    let cancelled = false;

    function renderWidget() {
      if (cancelled || !window.turnstile || !turnstileContainer.current) return;
      turnstileWidget.current = window.turnstile.render(turnstileContainer.current, {
        sitekey: TURNSTILE_SITE_KEY,
        action: "admin-login",
        callback: (token) => setTurnstileToken(token),
        "expired-callback": () => setTurnstileToken(""),
        "error-callback": () => {
          setTurnstileToken("");
          setError("Human verification could not load. Refresh the page and try again.");
        },
      });
    }

    const script = document.getElementById("cf-turnstile-script");
    if (window.turnstile) renderWidget();
    else script?.addEventListener("load", renderWidget);

    return () => {
      cancelled = true;
      script?.removeEventListener("load", renderWidget);
      if (turnstileWidget.current != null && window.turnstile) {
        window.turnstile.remove(turnstileWidget.current);
        turnstileWidget.current = null;
      }
    };
  }, []);

  async function handleSubmit(e) {
    e.preventDefault();
    if (!turnstileToken) {
      setError("Please complete the human verification");
      return;
    }
    setError("");
    setBusy(true);
    try {
      await login(username.trim(), password, turnstileToken);
      onSuccess();
    } catch (err) {
      setError(err.message || "Login failed");
      // Tokens are single-use: the server already spent this one.
      setTurnstileToken("");
      if (turnstileWidget.current != null) window.turnstile?.reset(turnstileWidget.current);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <div className="login-card card">
        <div className="logo">
          <div className="mark">License Server</div>
          <div className="tag">Admin Console</div>
        </div>

        {banner && <div className="banner warn login-notice" role="note">{banner}</div>}
        {notice && !error && <div className="banner info">{notice}</div>}
        {error && <div className="banner error">{error}</div>}

        <form onSubmit={handleSubmit}>
          <div className="field">
            <label htmlFor="username">Username</label>
            <input
              id="username"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
            />
          </div>
          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <div className="field" ref={turnstileContainer} />
          <button
            className="primary"
            type="submit"
            disabled={busy || !turnstileToken}
            style={{ width: "100%" }}
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <p className="note" style={{ marginTop: 18, marginBottom: 0 }}>
          Your own account on this server. The first account comes from{" "}
          <code className="inline-code">ADMIN_USERNAME</code> /{" "}
          <code className="inline-code">ADMIN_PASSWORD</code> and must change its password at first
          login; further accounts are made under Users.
        </p>
      </div>
    </div>
  );
}
