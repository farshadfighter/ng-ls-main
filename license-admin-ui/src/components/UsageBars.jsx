import { OPERATIONS } from "../lib/plans.js";
import { isUnlimited } from "../lib/format.js";

// Renders used/max progress bars for the five quota dimensions. A license row
// from the API carries max_<op> / used_<op> fields; null max = unlimited.
export default function UsageBars({ license }) {
  return (
    <div className="usage">
      {OPERATIONS.map((op) => {
        const max = license[`max_${op}`];
        const used = license[`used_${op}`] ?? 0;
        const unlimited = isUnlimited(max);
        const pct = unlimited || !max ? 0 : Math.min(100, Math.round((used / max) * 100));
        const cls = pct >= 100 ? "full" : pct >= 80 ? "warn" : "";
        return (
          <div className="row" key={op}>
            <div className="top">
              <span className="name">{op}</span>
              <span className="val">
                {used} / {unlimited ? "∞" : max}
              </span>
            </div>
            <div className={`bar${unlimited ? " unlimited" : ""}`}>
              <span className={cls} style={{ width: unlimited ? "100%" : `${pct}%` }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}
