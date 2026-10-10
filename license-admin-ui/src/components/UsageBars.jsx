import { fmtAssets } from "../lib/plans.js";
import { isUnlimited, relativeTime } from "../lib/format.js";

// The license's one entitlement: managed assets. `max_assets` is what it was
// sold (plan + add-ons, null = unlimited); `reported_assets` is what the
// licensed machine last said it manages, in its heartbeat. NGCorion enforces
// the limit itself; above it the bar shows the overrun.
export default function UsageBars({ license }) {
  const max = license.max_assets;
  const used = license.reported_assets;
  const unlimited = isUnlimited(max);
  const known = used !== null && used !== undefined;
  const pct = unlimited || !max || !known ? 0 : Math.min(100, Math.round((used / max) * 100));
  const cls = pct >= 100 ? "full" : pct >= 80 ? "warn" : "";
  return (
    <div className="usage">
      <div className="row">
        <div className="top">
          <span className="name">Assets</span>
          <span className="val">
            {known ? fmtAssets(used) : "—"} / {unlimited ? "∞" : fmtAssets(max)}
          </span>
        </div>
        <div className={`bar${unlimited ? " unlimited" : ""}`}>
          <span className={cls} style={{ width: unlimited ? "100%" : `${pct}%` }} />
        </div>
        <div className="note" style={{ marginTop: 6 }}>
          {known
            ? `Reported by the licensed machine ${relativeTime(license.reported_assets_at)}`
            : "Not reported yet (sent with the machine's heartbeat)"}
          {known && !unlimited && used > max && ` · ${fmtAssets(used - max)} over the limit`}
        </div>
      </div>
    </div>
  );
}
