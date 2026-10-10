import { ADDON, PLANS, fmtAssets, planLabel } from "../lib/plans.js";

// Built-in documentation. Content is derived from the actual license server
// source so it stays accurate:
//   - NGCorion app/core/machine_fingerprint.py (fingerprint composition)
//   - app/crud.py               (key/token generation, validation, plan changes)
//   - app/plans.py              (the plan catalog)
//   - app/utils/license_token.py (signed answers)
//   - app/utils/signing.py      (HMAC request signing)
//   - app/core/security.py      (admin JWT)
//   - app/models.py             (license fields, plans)
export default function Guide() {
  const sections = [
    { id: "overview", label: "Overview" },
    { id: "fingerprint", label: "Client fingerprint" },
    { id: "token", label: "Token structure" },
    { id: "validation", label: "Validation flow" },
    { id: "security", label: "Security model" },
    { id: "plans", label: "Plans & limits" },
  ];

  return (
    <div className="guide">
      <nav className="toc">
        {sections.map((s) => (
          <a key={s.id} href={`#${s.id}`}>
            {s.label}
          </a>
        ))}
      </nav>

      <div>
        <div className="page-head">
          <h1>How the licensing system works</h1>
          <p>A reference for operators issuing and managing licenses.</p>
        </div>

        <section id="overview" className="guide-section">
          <h2>Overview</h2>
          <p>
            The license server issues and validates NGCorion licenses. A license is bound to a
            single machine via a fingerprint NGCorion computes, every answer is signed with the
            server's Ed25519 key, and a heartbeat keeps the license current. Each license carries an{" "}
            <strong>asset limit</strong> (its plan plus add-on packs) and an expiry date.
          </p>
          <p>A license touches the server at three moments:</p>
          <div className="flow">
            <span className="step">Activate (once)</span>
            <span className="arrow">→</span>
            <span className="step">Validate (on app start)</span>
            <span className="arrow">→</span>
            <span className="step">Heartbeat (hourly, with the asset count)</span>
          </div>
        </section>

        <section id="fingerprint" className="guide-section">
          <h2>Client fingerprint</h2>
          <p>
            NGCorion computes the fingerprint on the licensed machine from identifiers that survive
            container rebuilds, and sends only a SHA-256 digest — the raw identifiers never leave
            the machine.
          </p>
          <h3>What is collected</h3>
          <ul>
            <li>
              <strong>Hardware/VM UUID</strong> — DMI <code className="inline-code">product_uuid</code>.
            </li>
            <li>
              <strong>Host machine ID</strong> — the host's <code className="inline-code">/etc/machine-id</code>,
              mounted read-only into the container.
            </li>
          </ul>
          <p>
            The fingerprint is <strong>locked on first activation</strong>: the server stores it on
            the license, and every later validate/heartbeat must present the same value. A second
            machine using the same key is rejected with “already activated on another VM”.
          </p>
        </section>

        <section id="token" className="guide-section">
          <h2>Token structure</h2>
          <p>Each license is identified by two values, both generated server-side at creation:</p>
          <h3>License key</h3>
          <p>
            A human-friendly identifier in the form{" "}
            <code className="inline-code">XXXX-XXXX-XXXX-XXXX</code> — four groups of four
            characters drawn from <code className="inline-code">A–Z</code> and{" "}
            <code className="inline-code">0–9</code> using a cryptographically secure RNG
            (<code className="inline-code">secrets</code>). It is the public handle for the license.
          </p>
          <h3>Organization token</h3>
          <p>
            A 64-character SHA-256 digest that acts as the license's <strong>shared secret</strong>:
          </p>
          <div className="codeblock">{`unique = f"{organization_name}:{customer_email}:{secrets.token_hex(16)}"
organization_token = sha256(unique).hexdigest()`}</div>
          <p>
            The random 16-byte salt makes the token unguessable even if the org name and email are
            known. It is returned <strong>once</strong> at creation (and shown on the credential
            card) and is later used as the HMAC key when signing protected requests — so treat it
            like a password.
          </p>
        </section>

        <section id="validation" className="guide-section">
          <h2>Validation flow</h2>
          <h3>1 · Activate <span className="note">— POST /api/licenses/activate</span></h3>
          <p>
            Sends <code className="inline-code">license_key</code> +{" "}
            <code className="inline-code">vm_fingerprint</code>. The server checks the key exists,
            is active and not expired, binds the fingerprint if unset, and returns the{" "}
            <code className="inline-code">organization_token</code>, the plan and the asset limit.
            Public endpoint (the license key is the credential).
          </p>
          <h3>2 · Validate <span className="note">— POST /api/licenses/validate</span></h3>
          <p>
            Sends <code className="inline-code">license_key</code> +{" "}
            <code className="inline-code">organization_token</code> +{" "}
            <code className="inline-code">vm_fingerprint</code>. Confirms the token matches, the
            license is active, not expired, and the fingerprint matches. Accepts an optional HMAC signature; if headers are present they must verify.
          </p>
          <h3>3 · Heartbeat <span className="note">— POST /api/licenses/heartbeat</span></h3>
          <p>
            An hourly keep-alive that also reports how many assets NGCorion manages (shown on the
            license as <em>reported assets</em>). Its signed answer carries the license's current
            plan and limit, so a plan change reaches the customer within the hour.
          </p>
          <h3>Offline: the pilot fallback</h3>
          <p>
            When NGCorion has had no signed answer for <strong>48 hours</strong>, it falls back on
            its own to the pilot entitlement (<strong>5 assets</strong>, for at most 30 days). As
            soon as it reaches the server again it returns to the license's own plan. The server
            never changes a license because it was offline.
          </p>
          <h3>Offline (air-gapped) licenses</h3>
          <p>
            For an NGCorion that never reaches this server: mark the license <em>offline</em>. The
            customer downloads a request file on NGCorion's License page, you upload it on the license
            (Offline license → Issue license file), and the signed license file you get back is uploaded
            on NGCorion. It is bound to that machine and valid one year (never past the license's
            expiry); no heartbeats, no 48-hour rule. It cannot be revoked before it ends, so every issued
            file is listed on the license.
          </p>
          <p>
            To look at a file without issuing anything, use <em>Test → Check an offline file</em>: for a
            request it shows what it asks for and whether a license file can be issued; for a license
            file, whether this server signed it, how long it is valid, and whether it is in the issue
            record.
          </p>
          <h3>Request signing</h3>
          <p>Protected requests are signed with HMAC-SHA256 over a canonical payload:</p>
          <div className="codeblock">{`payload   = json.dumps(request_data, sort_keys=True)
message   = f"{payload}:{timestamp}"          # ISO-8601 timestamp
signature = hmac_sha256(key=organization_token, msg=message).hexdigest()

# sent as headers:
X-Timestamp: <iso timestamp>
X-Signature: <hex signature>`}</div>
          <p>
            The server recomputes the signature with the stored organization token and compares in
            constant time. Requests whose timestamp is more than <strong>300 seconds</strong> from
            the server clock are rejected, which blocks replay attacks.
          </p>
        </section>

        <section id="security" className="guide-section">
          <h2>Security model</h2>
          <ul>
            <li>
              <strong>Admin auth (this console)</strong> — username/password exchanged for a JWT
              (HS256) that expires after 24h. The token is sent as a{" "}
              <code className="inline-code">Bearer</code> header on every admin call and is required
              for all license generation and management endpoints.
            </li>
            <li>
              <strong>Signed client requests</strong> — HMAC-SHA256 keyed on the organization token,
              with a 300-second timestamp window and constant-time comparison.
            </li>
            <li>
              <strong>One-VM binding</strong> — the fingerprint is locked on first activation;
              mismatches are refused, so a key can't be shared across machines.
            </li>
            <li>
              <strong>Expiry</strong> — every license has an <code className="inline-code">expires_at</code>{" "}
              set from the plan duration; expired licenses fail validation.
            </li>
            <li>
              <strong>Signed answers</strong> — every activate/validate/heartbeat answer is signed
              (Ed25519) with the client's nonce in it; NGCorion has the public key built in and acts
              only on what a valid signature says, so a fake server cannot unlock it.
            </li>
            <li>
              <strong>Offline fallback</strong> — after 48h without a signed answer NGCorion drops
              to the pilot entitlement until it reconnects, preventing indefinite offline use.
            </li>
            <li>
              <strong>Revocation</strong> — revoking sets{" "}
              <code className="inline-code">is_active = false</code> (a soft delete); the record is
              kept for audit and all validation then fails. There is no reactivate endpoint, so
              revocation is one-way from this console.
            </li>
          </ul>
        </section>

        <section id="plans" className="guide-section">
          <h2>Plans &amp; limits</h2>
          <p>
            A license is sold per number of managed assets. The plan sets the base limit and the
            term; <strong>add-on packs</strong> ({ADDON.code}, {ADDON.assets} assets each) raise a
            finite paid plan's limit. An organisation with 600 assets buys the 500 plan with two
            add-ons. Audits and hardening runs are not counted.
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Plan</th>
                  <th>Code</th>
                  <th className="num">Assets</th>
                  <th>Add-ons</th>
                  <th>Term</th>
                </tr>
              </thead>
              <tbody>
                {PLANS.map((p) => (
                  <tr key={p.code}>
                    <td>
                      <strong>{planLabel(p.code)}</strong>
                    </td>
                    <td className="mono">{p.code}</td>
                    <td className="num">{fmtAssets(p.assets)}</td>
                    <td>{p.addonsAllowed ? "Yes" : "—"}</td>
                    <td>{p.durationDays} days</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="note" style={{ marginTop: 12 }}>
            The limit is stored with each license when it is created or changed, so later catalog
            changes do not alter licenses already sold. The authoritative catalog is{" "}
            <code className="inline-code">license_server/app/plans.py</code>.
          </p>
        </section>
      </div>
    </div>
  );
}
