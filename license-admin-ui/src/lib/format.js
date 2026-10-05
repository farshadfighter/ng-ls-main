// Formatting + small data helpers (dates, expiry, status, CSV).

export function fmtDate(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function fmtDateShort(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "2-digit" });
}

// Human "in 3 months" / "5 days ago" relative to now, given an ISO timestamp.
export function relativeTime(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  const diffMs = d.getTime() - Date.now();
  const abs = Math.abs(diffMs);
  const past = diffMs < 0;
  const units = [
    ["year", 365 * 864e5],
    ["month", 30 * 864e5],
    ["day", 864e5],
    ["hour", 36e5],
    ["minute", 6e4],
  ];
  for (const [name, ms] of units) {
    if (abs >= ms || name === "minute") {
      const n = Math.round(abs / ms);
      const plural = n === 1 ? "" : "s";
      return past ? `${n} ${name}${plural} ago` : `in ${n} ${name}${plural}`;
    }
  }
  return "just now";
}

export function isExpired(license) {
  if (!license?.expires_at) return false;
  return new Date(license.expires_at).getTime() < Date.now();
}

// Returns one of: "revoked" | "expired" | "active"
export function licenseStatus(license) {
  if (!license?.is_active) return "revoked";
  if (isExpired(license)) return "expired";
  return "active";
}

export function isUnlimited(max) {
  return max === null || max === undefined;
}

// ---------- CSV ----------

function csvCell(value) {
  const s = value === null || value === undefined ? "" : String(value);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function toCsv(rows, columns) {
  const header = columns.map((c) => csvCell(c.header)).join(",");
  const lines = rows.map((row) =>
    columns.map((c) => csvCell(c.value(row))).join(",")
  );
  return [header, ...lines].join("\r\n");
}
