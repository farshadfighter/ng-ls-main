import { useCallback, useEffect, useMemo, useState } from "react";
import { canAudit, canWrite, getUser, idleMinutes, isAdmin, isAuthed, logout } from "./auth/auth.js";
import { onUnauthorized } from "./api/client.js";
import Login from "./components/Login.jsx";
import GenerateLicense from "./components/GenerateLicense.jsx";
import LicenseList from "./components/LicenseList.jsx";
import TestPanel from "./components/TestPanel.jsx";
import Guide from "./components/Guide.jsx";
import Users from "./components/Users.jsx";
import AuditLog from "./components/AuditLog.jsx";
import ChangePassword from "./components/ChangePassword.jsx";

// Each tab and who sees it. The server enforces the same rules on every call.
const TABS = [
  { id: "generate", label: "Generate", show: canWrite },
  { id: "licenses", label: "Licenses", show: () => true },
  // The Test page activates licenses against the live server, which binds them: a change.
  { id: "test", label: "Test", show: canWrite },
  { id: "users", label: "Users", show: isAdmin },
  { id: "audit", label: "Audit", show: canAudit },
  { id: "guide", label: "Guide", show: () => true },
];

const ACTIVITY_EVENTS = ["mousedown", "keydown", "scroll", "touchstart"];

export default function App() {
  const [authed, setAuthed] = useState(isAuthed());
  const [user, setUserState] = useState(getUser());
  const [tab, setTab] = useState(null);
  const [changingPassword, setChangingPassword] = useState(false);
  const [notice, setNotice] = useState("");

  const signedOut = useCallback((message = "") => {
    setAuthed(false);
    setUserState(null);
    setTab(null);
    setNotice(message);
  }, []);

  // Any 401 from an authenticated call (session ended, revoked, idle) drops us back to login.
  useEffect(() => onUnauthorized(() => signedOut("Your session has ended. Please sign in again.")), [signedOut]);

  // The server ends an idle session on its own; this signs the screen out at the same moment,
  // so an unattended console does not keep showing data.
  useEffect(() => {
    if (!authed) return undefined;
    let timer;
    const arm = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        await logout();
        signedOut("You were signed out after a period of inactivity.");
      }, idleMinutes() * 60 * 1000);
    };
    arm();
    ACTIVITY_EVENTS.forEach((ev) => window.addEventListener(ev, arm, { passive: true }));
    return () => {
      clearTimeout(timer);
      ACTIVITY_EVENTS.forEach((ev) => window.removeEventListener(ev, arm));
    };
  }, [authed, signedOut]);

  const tabs = useMemo(() => (user ? TABS.filter((t) => t.show()) : []), [user]);
  const current = tab && tabs.some((t) => t.id === tab) ? tab : tabs[0]?.id;

  const handleLogout = useCallback(async () => {
    await logout();
    signedOut("");
  }, [signedOut]);

  if (!authed || !user) {
    return <Login notice={notice} onSuccess={() => { setNotice(""); setAuthed(true); setUserState(getUser()); }} />;
  }

  if (user.must_change_password) {
    return (
      <div className="container">
        <ChangePassword forced onDone={() => setUserState(getUser())} />
        <p className="note" style={{ textAlign: "center" }}>
          <button className="ghost" onClick={handleLogout}>Sign out</button>
        </p>
      </div>
    );
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="dot" />
          <span>License Server</span>
          <span className="sub">Admin Console</span>
        </div>
        <nav>
          {tabs.map((t) => (
            <button key={t.id} className={current === t.id ? "active" : ""} onClick={() => setTab(t.id)}>
              {t.label}
            </button>
          ))}
        </nav>
        <div className="spacer" />
        <span className="who">
          {user.username} <span className="badge plan">{user.role}</span>
        </span>
        <button className="ghost" onClick={() => setChangingPassword(true)}>Password</button>
        <button className="ghost" onClick={handleLogout}>Sign out</button>
      </header>

      {changingPassword && (
        <div className="container">
          <ChangePassword onDone={() => setChangingPassword(false)} onCancel={() => setChangingPassword(false)} />
        </div>
      )}

      {current === "generate" && (
        <div className="container">
          <GenerateLicense onCreated={() => setTab("licenses")} />
        </div>
      )}
      {current === "licenses" && (
        <div className="container wide">
          <LicenseList />
        </div>
      )}
      {current === "test" && (
        <div className="container">
          <TestPanel />
        </div>
      )}
      {current === "users" && (
        <div className="container wide">
          <Users />
        </div>
      )}
      {current === "audit" && (
        <div className="container wide">
          <AuditLog />
        </div>
      )}
      {current === "guide" && (
        <div className="container wide">
          <Guide />
        </div>
      )}
    </div>
  );
}
