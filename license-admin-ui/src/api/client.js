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

async function request(method, path, { body, auth = false } = {}) {
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
  return text ? JSON.parse(text) : null;
}

export const api = {
  get: (path, opts) => request("GET", path, opts),
  post: (path, body, opts) => request("POST", path, { ...opts, body }),
  del: (path, opts) => request("DELETE", path, opts),
};

// ---------- Endpoint helpers ----------

export const adminLogin = (username, password, turnstileToken) =>
  api.post("/api/admin/login", {
    username,
    password,
    "cf-turnstile-response": turnstileToken,
  });

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

// Public client endpoints (used by the Test panel) — no auth, no signature.
export const testActivate = (license_key, vm_fingerprint) =>
  api.post("/api/licenses/activate", { license_key, vm_fingerprint });

export const testValidate = (license_key, organization_token, vm_fingerprint) =>
  api.post("/api/licenses/validate", { license_key, organization_token, vm_fingerprint });

export const serverHealth = () => api.get("/health");
