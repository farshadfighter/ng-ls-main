import { licenseStatus } from "../lib/format.js";

const LABELS = { active: "Active", revoked: "Revoked", expired: "Expired" };

export default function StatusBadge({ license }) {
  const status = licenseStatus(license);
  return <span className={`badge ${status}`}>{LABELS[status]}</span>;
}
