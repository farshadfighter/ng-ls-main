import { useCallback, useEffect, useState } from "react";
import { createUser, listUsers, resetUserPassword, unlockUser, updateUser } from "../api/client.js";
import { getUser } from "../auth/auth.js";
import { fmtDate } from "../lib/format.js";
import { PASSWORD_RULES } from "./ChangePassword.jsx";

const ROLES = [
  { id: "admin", label: "Admin", text: "Everything, including accounts and permanent deletion" },
  { id: "operator", label: "Operator", text: "Create and change licenses, offline files" },
  { id: "auditor", label: "Auditor", text: "Read licenses and the audit trail; changes nothing" },
];

const EMPTY = { username: "", role: "operator", full_name: "", email: "", password: "" };

// Admin accounts (admin role). An account is never deleted - the audit trail refers to
// it - only disabled. A new or reset password is temporary: its owner must change it.
export default function Users() {
  const [users, setUsers] = useState([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [form, setForm] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [resetFor, setResetFor] = useState(null);
  const [resetPw, setResetPw] = useState("");
  const me = getUser();

  const load = useCallback(async () => {
    try {
      setUsers(await listUsers());
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function run(action, message) {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      await action();
      setNotice(message);
      await load();
      return true;
    } catch (err) {
      setError(err.message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function create(e) {
    e.preventDefault();
    const payload = { ...form, username: form.username.trim(), email: form.email.trim() || null,
                      full_name: form.full_name.trim() || null };
    if (await run(() => createUser(payload), `Account ${payload.username.toLowerCase()} created. ` +
        "Give its owner the temporary password; they must change it at first login.")) {
      setForm(EMPTY);
    }
  }

  const locked = (u) => u.locked_until && new Date(u.locked_until) > new Date();

  return (
    <>
      <div className="page-head">
        <h1>Users</h1>
        <p>Every person who uses this console has an account of their own, so every action in the
          audit trail names who did it.</p>
      </div>
      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner success">{notice}</div>}

      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>User</th><th>Role</th><th>State</th><th>Last sign-in</th><th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id}>
                  <td>
                    <div className="mono">{u.username}</div>
                    <div className="note">{u.full_name || ""}{u.email ? ` · ${u.email}` : ""}</div>
                  </td>
                  <td>
                    <select value={u.role} disabled={busy || u.username === me?.username}
                            onChange={(e) => run(() => updateUser(u.id, { role: e.target.value }),
                                                 `${u.username} is now ${e.target.value}.`)}>
                      {ROLES.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
                    </select>
                  </td>
                  <td>
                    {!u.is_active ? <span className="badge revoked">Disabled</span>
                      : locked(u) ? <span className="badge expired">Locked</span>
                      : <span className="badge active">Active</span>}
                    {u.must_change_password && u.is_active && <div className="note">Must change password</div>}
                  </td>
                  <td>
                    {u.last_login_at ? fmtDate(u.last_login_at) : "—"}
                    {u.last_login_ip && <div className="note mono">{u.last_login_ip}</div>}
                  </td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    {locked(u) && (
                      <button className="sm" disabled={busy}
                              onClick={() => run(() => unlockUser(u.id), `${u.username} unlocked.`)}>Unlock</button>
                    )}
                    <button className="sm" style={{ marginLeft: 6 }} disabled={busy}
                            onClick={() => { setResetFor(u); setResetPw(""); }}>Reset password</button>
                    {u.username !== me?.username && (
                      <button className={`sm ${u.is_active ? "danger" : ""}`} style={{ marginLeft: 6 }} disabled={busy}
                              onClick={() => run(() => updateUser(u.id, { is_active: !u.is_active }),
                                                 `${u.username} ${u.is_active ? "disabled; its sessions ended" : "enabled"}.`)}>
                        {u.is_active ? "Disable" : "Enable"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {resetFor && (
        <div className="card">
          <h2>Temporary password for {resetFor.username}</h2>
          <p className="card-sub">Its sessions end, a lockout is lifted, and the owner must choose a new
            password at next login.</p>
          <div className="field">
            <label htmlFor="reset_pw">Temporary password</label>
            <input id="reset_pw" type="password" autoComplete="new-password" value={resetPw}
                   onChange={(e) => setResetPw(e.target.value)} />
            <div className="hint">{PASSWORD_RULES}</div>
          </div>
          <div style={{ display: "flex", gap: 10 }}>
            <button className="primary" disabled={busy || !resetPw}
                    onClick={async () => {
                      if (await run(() => resetUserPassword(resetFor.id, resetPw),
                                    `Temporary password set for ${resetFor.username}.`)) setResetFor(null);
                    }}>Set password</button>
            <button onClick={() => setResetFor(null)}>Cancel</button>
          </div>
        </div>
      )}

      <div className="card">
        <h2>New account</h2>
        <form onSubmit={create}>
          <div className="grid-2">
            <div className="field">
              <label htmlFor="nu_name">User name</label>
              <input id="nu_name" value={form.username} autoComplete="off"
                     onChange={(e) => setForm({ ...form, username: e.target.value })} placeholder="e.g. s.ahmadi" />
            </div>
            <div className="field">
              <label htmlFor="nu_role">Role</label>
              <select id="nu_role" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
                {ROLES.map((r) => <option key={r.id} value={r.id}>{r.label} — {r.text}</option>)}
              </select>
            </div>
            <div className="field">
              <label htmlFor="nu_full">Full name</label>
              <input id="nu_full" value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} />
            </div>
            <div className="field">
              <label htmlFor="nu_email">Email</label>
              <input id="nu_email" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
            </div>
          </div>
          <div className="field">
            <label htmlFor="nu_pw">Temporary password</label>
            <input id="nu_pw" type="password" autoComplete="new-password" value={form.password}
                   onChange={(e) => setForm({ ...form, password: e.target.value })} />
            <div className="hint">{PASSWORD_RULES} The owner must change it at first login.</div>
          </div>
          <button className="primary" type="submit" disabled={busy || !form.username || !form.password}>
            Create account
          </button>
        </form>
      </div>
    </>
  );
}
