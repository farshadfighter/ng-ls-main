import { useState } from "react";
import { changeMyPassword, getMe } from "../api/client.js";
import { setUser } from "../auth/auth.js";

export const PASSWORD_RULES =
  "At least 12 characters, with at least three of: lower case, upper case, digits, symbols. " +
  "Not containing your user name, and not your current password.";

// Change one's own password. `forced`: the first login (or after an admin reset) -
// nothing else is reachable until it is done. Other sessions of the account end.
export default function ChangePassword({ forced = false, onDone, onCancel }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    if (next !== again) {
      setError("The two new passwords differ.");
      return;
    }
    setError("");
    setBusy(true);
    try {
      await changeMyPassword(current, next);
      setUser(await getMe());
      onDone?.();
    } catch (err) {
      setError(err.message || "The password could not be changed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ maxWidth: 520, margin: forced ? "48px auto" : 0 }}>
      <h2>{forced ? "Choose your own password" : "Change password"}</h2>
      <p className="card-sub">
        {forced
          ? "Your account has a temporary password. Choose your own before continuing."
          : "Your other sessions are signed out."}
      </p>
      {error && <div className="banner error">{error}</div>}
      <form onSubmit={submit}>
        <div className="field">
          <label htmlFor="pw_current">{forced ? "Temporary password" : "Current password"}</label>
          <input id="pw_current" type="password" autoComplete="current-password" value={current}
                 onChange={(e) => setCurrent(e.target.value)} autoFocus />
        </div>
        <div className="field">
          <label htmlFor="pw_new">New password</label>
          <input id="pw_new" type="password" autoComplete="new-password" value={next}
                 onChange={(e) => setNext(e.target.value)} />
          <div className="hint">{PASSWORD_RULES}</div>
        </div>
        <div className="field">
          <label htmlFor="pw_again">New password again</label>
          <input id="pw_again" type="password" autoComplete="new-password" value={again}
                 onChange={(e) => setAgain(e.target.value)} />
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <button className="primary" type="submit" disabled={busy || !current || !next || !again}>
            {busy ? "Saving…" : "Change password"}
          </button>
          {onCancel && <button type="button" onClick={onCancel}>Cancel</button>}
        </div>
      </form>
    </div>
  );
}
