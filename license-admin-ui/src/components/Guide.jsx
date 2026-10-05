import { PLANS } from "../lib/plans.js";

// Built-in documentation. Content is derived from the actual license server
// source so it stays accurate:
//   - app/utils/fingerprint.py  (fingerprint composition)
//   - app/crud.py               (key/token generation, validation, downgrade)
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
            The license server is a standalone service that issues, validates and tracks usage of
            licenses for the main application. A license is bound to a single machine (VM) via a
            hardware fingerprint, signed requests prevent tampering, and a heartbeat keeps the
            license alive. Each license carries per-operation quotas and an expiry date determined
            by its plan.
          </p>
          <p>A license touches the server at four moments:</p>
          <div className="flow">
            <span className="step">Activate (once)</span>
            <span className="arrow">→</span>
            <span className="step">Validate (on app start)</span>
            <span className="arrow">→</span>
            <span className="step">Heartbeat (hourly)</span>
            <span className="arrow">→</span>
            <span className="step">Consume (per operation)</span>
          </div>
        </section>

        <section id="fingerprint" className="guide-section">
          <h2>Client fingerprint</h2>
          <p>
            On the client machine the SDK builds a fingerprint from stable hardware/OS identifiers,
            joins them with <code className="inline-code">|</code>, and takes a SHA-256 hash. Only
            the resulting hex digest leaves the machine — the raw identifiers never do.
          </p>
          <h3>What is collected</h3>
          <ul>
            <li>
              <strong>MAC address</strong> — derived from <code className="inline-code">uuid.getnode()</code>{" "}
              (first non-loopback interface).
            </li>
            <li>
              <strong>Machine ID</strong> — <code className="inline-code">/etc/machine-id</code> on Linux,
              or the <code className="inline-code">MachineGuid</code> registry value on Windows.
            </li>
            <li>
              <strong>Hostname</strong> — <code className="inline-code">platform.node()</code>.
            </li>
            <li>
              <strong>CPU</strong> — <code className="inline-code">platform.processor()</code>.
            </li>
          </ul>
          <div className="codeblock">{`components = [
  "mac:" + mac_address,        # uuid.getnode()
  "machine:" + machine_id,     # /etc/machine-id | Windows MachineGuid
  "host:" + hostname,          # platform.node()
  "cpu:" + cpu,                # platform.processor()
]
fingerprint = sha256("|".join(components)).hexdigest()`}</div>
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
            <code className="inline-code">organization_token</code> and plan limits. Public endpoint
            (no signature).
          </p>
          <h3>2 · Validate <span className="note">— POST /api/licenses/validate</span></h3>
          <p>
            Sends <code className="inline-code">license_key</code> +{" "}
            <code className="inline-code">organization_token</code> +{" "}
            <code className="inline-code">vm_fingerprint</code>. Confirms the token matches, the
            license is active, not expired, the fingerprint matches, and the heartbeat hasn't been
            missed. Accepts an optional HMAC signature; if headers are present they must verify.
          </p>
          <h3>3 · Heartbeat <span className="note">— POST /api/licenses/heartbeat</span></h3>
          <p>
            A periodic keep-alive. If more than <strong>48 hours</strong> pass without one, the
            license is automatically <strong>downgraded to Pilot</strong> limits.
          </p>
          <h3>4 · Consume <span className="note">— POST /api/licenses/consume</span></h3>
          <p>
            Increments a quota counter (asset / discovery / audit / harden / monitor) after checking
            the limit. <strong>Requires a valid HMAC signature</strong> — only the application
            backend should call it.
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
              <strong>Heartbeat downgrade</strong> — missing heartbeats for 48h+ silently drops the
              license to Pilot limits, preventing indefinite offline use.
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
          <p>The plan chosen at creation sets the quotas and validity period:</p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Plan</th>
                  <th>Assets</th>
                  <th>Discoveries</th>
                  <th>Audits</th>
                  <th>Hardens</th>
                  <th>Monitors</th>
                  <th>Duration</th>
                </tr>
              </thead>
              <tbody>
                {PLANS.map((p) => (
                  <tr key={p.value}>
                    <td>
                      <strong>{p.label}</strong>
                    </td>
                    {["assets", "discoveries", "audits", "hardens", "monitors"].map((op) => (
                      <td key={op}>{p.limits[op] === null ? "∞" : p.limits[op]}</td>
                    ))}
                    <td>{p.durationDays} days</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="note" style={{ marginTop: 12 }}>
            Limits mirror the server's plan catalog. The authoritative values live in the license
            server source.
          </p>
        </section>
      </div>
    </div>
  );
}
