import { useCallback, useEffect, useState } from "react";
import { isAuthed, getUser, logout } from "./auth/auth.js";
import { onUnauthorized } from "./api/client.js";
import Login from "./components/Login.jsx";
import GenerateLicense from "./components/GenerateLicense.jsx";
import LicenseList from "./components/LicenseList.jsx";
import TestPanel from "./components/TestPanel.jsx";
import Guide from "./components/Guide.jsx";

const TABS = [
  { id: "generate", label: "Generate" },
  { id: "licenses", label: "Licenses" },
  { id: "test", label: "Test" },
  { id: "guide", label: "Guide" },
];

export default function App() {
  const [authed, setAuthed] = useState(isAuthed());
  const [tab, setTab] = useState("generate");

  // Any 401 from an authenticated call drops us back to the login screen.
  useEffect(() => onUnauthorized(() => setAuthed(false)), []);

  const handleLogout = useCallback(() => {
    logout();
    setAuthed(false);
  }, []);

  if (!authed) return <Login onSuccess={() => setAuthed(true)} />;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="dot" />
          <span>License Server</span>
          <span className="sub">Admin Console</span>
        </div>
        <nav>
          {TABS.map((t) => (
            <button
              key={t.id}
              className={tab === t.id ? "active" : ""}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <div className="spacer" />
        <span className="who">{getUser()}</span>
        <button className="ghost" onClick={handleLogout}>
          Sign out
        </button>
      </header>

      {tab === "generate" && (
        <div className="container">
          <GenerateLicense onCreated={() => setTab("licenses")} />
        </div>
      )}
      {tab === "licenses" && (
        <div className="container wide">
          <LicenseList />
        </div>
      )}
      {tab === "test" && (
        <div className="container">
          <TestPanel />
        </div>
      )}
      {tab === "guide" && (
        <div className="container wide">
          <Guide />
        </div>
      )}
    </div>
  );
}
