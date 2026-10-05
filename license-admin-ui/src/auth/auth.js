// Admin session handling. The JWT lives in sessionStorage (cleared when the tab
// closes) — appropriate for an internal admin console. We also remember the
// username for the header. Login hits POST /api/admin/login.

import { adminLogin } from "../api/client.js";

const TOKEN_KEY = "license_admin_token";
const USER_KEY = "license_admin_user";

export function getToken() {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function getUser() {
  return sessionStorage.getItem(USER_KEY);
}

export function isAuthed() {
  return Boolean(getToken());
}

export function clearToken() {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(USER_KEY);
}

export async function login(username, password, turnstileToken) {
  const data = await adminLogin(username, password, turnstileToken);
  if (!data?.access_token) throw new Error("No token returned by server");
  sessionStorage.setItem(TOKEN_KEY, data.access_token);
  sessionStorage.setItem(USER_KEY, username);
  return data;
}

export function logout() {
  clearToken();
}
