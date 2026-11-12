import { useCallback, useEffect, useRef, useState } from "react";
import { login } from "../auth/auth.js";
import { getLoginBanner, loginOptions, newCaptcha } from "../api/client.js";

const TURNSTILE_SITE_KEY =
  import.meta.env.VITE_TURNSTILE_SITE_KEY || "0x4AAAAAAE4jLGhJo1wHHnwA";
const TURNSTILE_SCRIPT = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";

// Cloudflare's script, loaded only when the server asks for Turnstile (CAPTCHA_MODE=turnstile), so a
// server on its own picture never makes the browser reach Cloudflare.
function loadTurnstile() {
  if (window.turnstile) return Promise.resolve();
  return new Promise((resolve, reject) => {
    let script = document.getElementById("cf-turnstile-script");
    if (!script) {
      script = document.createElement("script");
      script.id = "cf-turnstile-script";
      script.src = TURNSTILE_SCRIPT;
      script.async = true;
      document.head.appendChild(script);
    }
    script.addEventListener("load", () => resolve(), { once: true });
    script.addEventListener("error", () => reject(new Error("Turnstile could not load")), { once: true });
  });
}

// What the server's mode was called before CAPTCHA_MODE (a server not yet upgraded).
function modeOf(opts) {
  if (opts?.captcha) return opts.captcha;
  if (opts?.turnstile_enabled === true) return "turnstile";
  if (opts?.turnstile_enabled === false) return "off";
  return "builtin";
}

export default function Login({ onSuccess, notice = "" }) {
  const [banner, setBanner] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // The bot check the server wants (CAPTCHA_MODE, GET /api/admin/login-options): null while asking,
  // then "builtin" (the server's own picture), "turnstile" or "off".
  const [mode, setMode] = useState(null);
  const [turnstileToken, setTurnstileToken] = useState("");
  const turnstileContainer = useRef(null);
  const turnstileWidget = useRef(null);
  // The server's picture: { id, image, expires_in } and the characters typed. A picture is good for
  // one attempt and a couple of minutes; captchaSeq asks for a new one.
  const [captcha, setCaptcha] = useState(null);
  const [captchaAnswer, setCaptchaAnswer] = useState("");
  const [captchaSeq, setCaptchaSeq] = useState(0);
  const [captchaError, setCaptchaError] = useState("");
  const answerInput = useRef(null);
  const newPicture = useCallback(() => setCaptchaSeq((n) => n + 1), []);

  // The notice everyone sees before signing in (ADMIN_LOGIN_BANNER on the server).
  useEffect(() => {
    getLoginBanner().then((r) => setBanner(r?.banner || "")).catch(() => setBanner(""));
  }, []);

  // If the question fails, assume the server's default (its own picture): showing a check the
  // server ignores still signs you in, while hiding one it demands makes signing in impossible.
  useEffect(() => {
    let cancelled = false;
    loginOptions()
      .then((opts) => { if (!cancelled) setMode(modeOf(opts)); })
      .catch(() => { if (!cancelled) setMode("builtin"); });
    return () => { cancelled = true; };
  }, []);

  // A picture from the server, and a fresh one a few seconds before it expires.
  useEffect(() => {
    if (mode !== "builtin") return undefined;
    let cancelled = false;
    let timer;
    setCaptcha(null);
    setCaptchaAnswer("");
    setCaptchaError("");
    newCaptcha()
      .then((pic) => {
        if (cancelled) return;
        setCaptcha(pic);
        timer = setTimeout(newPicture, Math.max(10, (pic.expires_in || 120) - 5) * 1000);
      })
      .catch((err) => {
        if (!cancelled) setCaptchaError(err.message || "The picture could not be loaded.");
      });
    return () => { cancelled = true; clearTimeout(timer); };
  }, [mode, captchaSeq, newPicture]);

  // Turnstile (CAPTCHA_MODE=turnstile): Cloudflare's widget, rendered once its script has loaded.
  useEffect(() => {
    if (mode !== "turnstile") return undefined;
    let cancelled = false;

    loadTurnstile()
      .then(() => {
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
      })
      .catch(() => {
        if (!cancelled) setError("Human verification could not load. Refresh the page and try again.");
      });

    return () => {
      cancelled = true;
      if (turnstileWidget.current != null && window.turnstile) {
        window.turnstile.remove(turnstileWidget.current);
        turnstileWidget.current = null;
      }
    };
  }, [mode]);

  const checkReady =
    mode === "off" ||
    (mode === "turnstile" && Boolean(turnstileToken)) ||
    (mode === "builtin" && Boolean(captcha) && captchaAnswer.trim().length >= 5);

  async function handleSubmit(e) {
    e.preventDefault();
    if (!checkReady) {
      setError(mode === "builtin" ? "Type the characters shown in the picture" : "Please complete the human verification");
      return;
    }
    setError("");
    setBusy(true);
    try {
      const check =
        mode === "builtin" ? { captchaId: captcha.id, captchaAnswer: captchaAnswer.trim() }
          : mode === "turnstile" ? { turnstileToken } : {};
      await login(username.trim(), password, check);
      onSuccess();
    } catch (err) {
      setError(err.message || "Login failed");
      // Every check is good for one attempt: the server already spent this one.
      if (mode === "builtin") {
        newPicture();
        answerInput.current?.focus();
      }
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
          {mode === "builtin" && (
            <div className="field captcha">
              <label htmlFor="captcha-answer">Characters in the picture</label>
              <div className="captcha-row">
                {captcha ? (
                  <img
                    className="captcha-img"
                    src={captcha.image}
                    width={220}
                    height={72}
                    alt="Security check: five characters to type below"
                    draggable={false}
                  />
                ) : (
                  <div className="captcha-img captcha-wait" aria-live="polite">
                    {captchaError ? "No picture" : "Loading…"}
                  </div>
                )}
                <button
                  type="button"
                  className="ghost sm"
                  onClick={newPicture}
                  title="Show another picture"
                  aria-label="Show another picture"
                >
                  ↻
                </button>
              </div>
              <input
                id="captcha-answer"
                ref={answerInput}
                className="captcha-input"
                dir="ltr"
                autoComplete="off"
                autoCapitalize="characters"
                autoCorrect="off"
                spellCheck={false}
                maxLength={8}
                value={captchaAnswer}
                onChange={(e) => setCaptchaAnswer(e.target.value)}
              />
              {captchaError ? (
                <div className="err">{captchaError}</div>
              ) : (
                <div className="hint">Not case-sensitive. Can&apos;t read it? Press ↻ for another.</div>
              )}
            </div>
          )}
          {mode === "turnstile" && <div className="field" ref={turnstileContainer} />}
          <button
            className="primary"
            type="submit"
            disabled={busy || mode === null || !checkReady}
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
