// Thin fetch wrapper around the License Server API.
//
// Base URL: VITE_LICENSE_API_URL when set (production build pointed at a
// deployed server), otherwise empty so requests hit same-origin paths and the
// Vite dev proxy (see vite.config.js) forwards them to the license server.
//
// The Bearer token is injected from the auth module on every admin call. A 401
// clears the session and notifies listeners so the app can bounce to Login.

import { getToken, clearToken } from "../auth/auth.js";

const BASE = (import.meta.env.VITE_LICENSE_API_URL || "").replace(/\/$/, "");

const unauthorizedListeners = new Set();
export function onUnauthorized(fn) {
  unauthorizedListeners.add(fn);
  return () => unauthorizedListeners.delete(fn);
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// Pull a human message out of a FastAPI error body ({detail: ...}).
async function parseError(res) {
  let detail;
  try {
    const body = await res.json();
    detail = body?.detail;
  } catch {
    /* non-JSON body */
  }
  if (Array.isArray(detail)) {
    // Pydantic validation errors -> "field: msg" lines
    detail = detail
      .map((e) => {
        const loc = Array.isArray(e.loc) ? e.loc.filter((p) => p !== "body").join(".") : "";
        return loc ? `${loc}: ${e.msg}` : e.msg;
      })
      .join("; ");
  }
  return detail || res.statusText || `Request failed (${res.status})`;
}

async function request(method, path, { body, auth = false, raw = false } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth) {
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }

  let res;
  try {
    res = await fetch(`${BASE}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new ApiError(
      "Cannot reach the license server. Is it running and is the API URL correct?",
      0
    );
  }

  if (res.status === 401 && auth) {
    clearToken();
    unauthorizedListeners.forEach((fn) => fn());
    throw new ApiError(await parseError(res), 401);
  }

  if (!res.ok) throw new ApiError(await parseError(res), res.status);

  if (res.status === 204) return null;
  const text = await res.text();
  if (raw) return text;
  return text ? JSON.parse(text) : null;
}

export const api = {
  get: (path, opts) => request("GET", path, opts),
  post: (path, body, opts) => request("POST", path, { ...opts, body }),
  patch: (path, body, opts) => request("PATCH", path, { ...opts, body }),
  del: (path, opts) => request("DELETE", path, opts),
  // A non-JSON body (CSV) as text.
  text: (path, opts) => request("GET", path, { ...opts, raw: true }),
};

// ---------- Endpoint helpers ----------

export const adminLogin = (username, password, turnstileToken) =>
  api.post("/api/admin/login", {
    username,
    password,
    "cf-turnstile-response": turnstileToken,
  });

export const adminLogout = () => api.post("/api/admin/logout", undefined, { auth: true });
export const getMe = () => api.get("/api/admin/me", { auth: true });
export const changeMyPassword = (current_password, new_password) =>
  api.post("/api/admin/me/password", { current_password, new_password }, { auth: true });
export const getLoginBanner = () => api.get("/api/admin/login-banner");

// Admin accounts (admin role).
export const listUsers = () => api.get("/api/admin/users", { auth: true });
export const createUser = (payload) => api.post("/api/admin/users", payload, { auth: true });
export const updateUser = (id, change) => api.patch(`/api/admin/users/${id}`, change, { auth: true });
export const resetUserPassword = (id, password) =>
  api.post(`/api/admin/users/${id}/reset-password`, { password }, { auth: true });
export const unlockUser = (id) => api.post(`/api/admin/users/${id}/unlock`, undefined, { auth: true });

// The audit trail (auditor and admin roles).
const query = (params) => {
  const q = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") q.set(k, v);
  });
  const text = q.toString();
  return text ? `?${text}` : "";
};
export const searchAudit = (params) => api.get(`/api/admin/audit${query(params)}`, { auth: true });
export const exportAudit = (params) => api.text(`/api/admin/audit/export.csv${query(params)}`, { auth: true });

export const createLicense = (payload) =>
  api.post("/api/admin/licenses", payload, { auth: true });

export const listLicenses = (skip = 0, limit = 1000) =>
  api.get(`/api/admin/licenses?skip=${skip}&limit=${limit}`, { auth: true });

export const getLicense = (licenseKey) =>
  api.get(`/api/admin/licenses/${encodeURIComponent(licenseKey)}`, { auth: true });

// Soft delete: flips is_active to false, the row stays in the database.
export const revokeLicense = (licenseKey) =>
  api.del(`/api/admin/licenses/${encodeURIComponent(licenseKey)}`, { auth: true });

// Hard delete: removes the record entirely. Irreversible — used to prune
// legacy licenses that no longer need to be kept around.
export const deleteLicense = (licenseKey) =>
  api.del(`/api/admin/licenses/${encodeURIComponent(licenseKey)}/permanent`, { auth: true });

// Release a license from the machine it is bound to (hardware move or reinstall);
// the customer's next activation binds it again.
export const resetBinding = (licenseKey) =>
  api.post(`/api/admin/licenses/${encodeURIComponent(licenseKey)}/reset-binding`, undefined, { auth: true });

// Change a license's plan and/or add-on packs ({plan_code?, addon_packs?}); the term stays.
export const changePlan = (licenseKey, change) =>
  api.post(`/api/admin/licenses/${encodeURIComponent(licenseKey)}/plan`, change, { auth: true });

// Offline (air-gapped) licenses: mark a license offline, and sign a one-year license file for
// the request file the customer's NGCorion made.
export const setOffline = (licenseKey, allowed) =>
  api.post(`/api/admin/licenses/${encodeURIComponent(licenseKey)}/offline`, { allowed }, { auth: true });

export const issueOfflineFile = (licenseKey, requestText) =>
  api.post(`/api/admin/licenses/${encodeURIComponent(licenseKey)}/offline-file`, { request: requestText }, { auth: true });

export const listOfflineIssues = (licenseKey) =>
  api.get(`/api/admin/licenses/${encodeURIComponent(licenseKey)}/offline-issues`, { auth: true });

// Checks an offline request (.ngcreq) or license file (.ngclic) against this server; changes nothing.
export const inspectOfflineFile = (content) =>
  api.post("/api/admin/offline/inspect", { content }, { auth: true });

// Public client endpoints (used by the Test panel) — no auth, no signature.
export const testActivate = (license_key, vm_fingerprint) =>
  api.post("/api/licenses/activate", { license_key, vm_fingerprint });

export const testValidate = (license_key, organization_token, vm_fingerprint) =>
  api.post("/api/licenses/validate", { license_key, organization_token, vm_fingerprint });

export const serverHealth = () => api.get("/health");
