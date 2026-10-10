// Admin session handling. The JWT lives in sessionStorage (cleared when the tab
// closes). The server keeps the session too: logout ends it there, and it ends
// by itself after the idle timeout (the server's ADMIN_IDLE_TIMEOUT_MINUTES).
// We also keep the signed-in user (name, role, must_change_password) for the
// header and for showing only what the role may do - the server enforces the
// same rules on every call; hiding a button is only convenience.

import { adminLogin, adminLogout } from "../api/client.js";

const TOKEN_KEY = "license_admin_token";
const USER_KEY = "license_admin_user";
const IDLE_KEY = "license_admin_idle_minutes";

export function getToken() {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function getUser() {
  try {
    return JSON.parse(sessionStorage.getItem(USER_KEY) || "null");
  } catch {
    return null;
  }
}

export function setUser(user) {
  sessionStorage.setItem(USER_KEY, JSON.stringify(user));
}

export function idleMinutes() {
  return Number(sessionStorage.getItem(IDLE_KEY)) || 15;
}

export function isAuthed() {
  return Boolean(getToken() && getUser());
}

export function clearToken() {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(USER_KEY);
  sessionStorage.removeItem(IDLE_KEY);
}

export async function login(username, password, turnstileToken) {
  const data = await adminLogin(username, password, turnstileToken);
  if (!data?.access_token) throw new Error("No token returned by server");
  sessionStorage.setItem(TOKEN_KEY, data.access_token);
  sessionStorage.setItem(IDLE_KEY, String(data.idle_timeout_minutes || 15));
  setUser(data.user);
  return data;
}

export async function logout() {
  try {
    if (getToken()) await adminLogout();
  } catch {
    /* the session may already have ended at the server */
  } finally {
    clearToken();
  }
}

// What the signed-in role may do (mirrors app/core/security.py on the server).
export const role = () => getUser()?.role;
export const canWrite = () => ["operator", "admin"].includes(role());
export const isAdmin = () => role() === "admin";
export const canAudit = () => ["auditor", "admin"].includes(role());
