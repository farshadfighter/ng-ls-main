# License Plans Summary

## Overview

Your application has a **fully implemented and integrated** license system with the following plans. The license system is already working and enforcing quotas across audit and hardening operations.

---

## License Plans

### 1. Pilot (Testing License)
**Plan Type:** `pilot`  
**Duration:** 30 days  
**Purpose:** Customer testing/evaluation

**Limits:**
- ✅ Max Audits: 2
- ✅ Max Hardens: 2

**Status:** ✅ Implemented and Active

---

### 2. 100 Audit / 100 Hardening
**Plan Type:** `plan_100`  
**Duration:** 1 year (365 days)  
**Target:** Small networks

**Limits:**
- ✅ Max Audits: 100
- ✅ Max Hardens: 100

**Status:** ✅ Implemented and Active

---

### 3. 250 Audit / 250 Hardening
**Plan Type:** `plan_250`  
**Duration:** 1 year (365 days)  
**Target:** Medium networks

**Limits:**
- ✅ Max Audits: 250
- ✅ Max Hardens: 250

**Status:** ✅ Implemented and Active

---

### 4. 500 Audit / 500 Hardening
**Plan Type:** `plan_500`  
**Duration:** 1 year (365 days)  
**Target:** Large networks

**Limits:**
- ✅ Max Audits: 500
- ✅ Max Hardens: 500

**Status:** ✅ Implemented and Active

---

### 5. Unlimited
**Plan Type:** `unlimited`  
**Duration:** 1 year (365 days)  
**Target:** Enterprise/unlimited usage

**Limits:**
- ✅ Max Audits: ∞ (Unlimited)
- ✅ Max Hardens: ∞ (Unlimited)

**Status:** ✅ Implemented and Active

---

## How License Enforcement Works

### 1. Asset Management (Asset Creation & Auto Discovery)
**Enforcement:** None — Asset Management (including asset creation and Auto Discovery scans) is **not license-gated**.

**Behavior:**
- Creating assets never checks or consumes any license quota
- Running Auto Discovery scans never checks or consumes any license quota
- There is no quota dimension for assets or discoveries anywhere in the system

---

### 2. Audit Operations
**Enforcement:** `require_quota("audit")` dependency

**Behavior:**
- Consumes 1 audit quota per operation
- Increments `used_audits` counter
- Returns HTTP 403 if quota exhausted

---

### 3. Hardening Operations
**Enforcement:** `require_quota("harden")` dependency

**Behavior:**
- Consumes 1 harden quota per operation
- Increments `used_hardens` counter
- Returns HTTP 403 if quota exhausted

---

## License Validation Flow

### On Application Startup
1. App loads license from `~/.license/.license.dat` (encrypted)
2. Validates with license server
3. Starts hourly heartbeat background task
4. License middleware blocks all `/api/*` requests if invalid

### During Operations
1. User attempts a license-gated operation (audit or harden)
2. Dependency checks quota with license server
3. If quota available: operation proceeds, counter increments
4. If quota exhausted: HTTP 403 returned with error message

### Heartbeat (Every Hour)
1. Background task sends heartbeat to license server
2. Updates `last_heartbeat_at` timestamp
3. If heartbeat missed for 48+ hours: license downgrades to PILOT mode

---

## Important Notes

### Asset Management Licensing Clarification

**Current Implementation:**
- ✅ Asset Management (asset creation and Auto Discovery) is **NOT** license-gated
- ✅ There is no quota dependency on `POST /api/assets/` or on discovery scan endpoints
- ✅ Users can create as many assets and run as many discovery scans as they like, regardless of plan

**How it works:**
- Only audits and hardens are consumed operations tracked against plan quotas
- Assets and discovery scans are unrestricted persistent/operational resources with no entitlement dimension

---

## License Server Architecture

### Separate Server
- **Location:** `/home/sina/netease/license_server/`
- **Database:** PostgreSQL (separate from main app)
- **Port:** Typically 8000 (configurable)
- **Purpose:** Centralized license validation and quota tracking

### Main Application Integration
- **License Client:** `app/core/license_client.py`
- **License State:** `app/core/license_state.py` (in-memory cache)
- **Middleware:** `app/middleware/license_middleware.py`
- **Dependencies:** `app/core/dependencies.py` (quota enforcement)

---

## Admin Operations

### Creating a License for a Customer

```bash
# 1. Admin logs in
curl -X POST http://localhost:8000/api/admin/login \
  -H "Content-Type: application/json" \
  -d '{
    "username": "admin",
    "password": "your-admin-password"
  }'

# Response: { "access_token": "eyJ...", "token_type": "bearer" }

# 2. Create license
curl -X POST http://localhost:8000/api/admin/licenses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer eyJ..." \
  -d '{
    "customer_name": "John Doe",
    "customer_email": "john@company.com",
    "organization_name": "ACME Corp",
    "plan_type": "plan_250"
  }'

# Response includes:
# - license_key: "XXXX-XXXX-XXXX-XXXX" (give this to customer)
# - organization_token: "abc123..." (customer gets after activation)
# - expires_at: "2026-05-12T..."
# - All plan limits
```

### Available Plan Types
- `pilot` - Testing license (30 days, 2 audits, 2 hardens)
- `plan_100` - 100 Audit / 100 Hardening (1 year, 100 audits, 100 hardens)
- `plan_250` - 250 Audit / 250 Hardening (1 year, 250 audits, 250 hardens)
- `plan_500` - 500 Audit / 500 Hardening (1 year, 500 audits, 500 hardens)
- `unlimited` - Unlimited (1 year, unlimited audits and hardens)

---

## Customer Activation Flow

### Step 1: Customer receives license key
Admin sends: `XXXX-XXXX-XXXX-XXXX`

### Step 2: Customer activates in application
```bash
# Get VM fingerprint
curl http://localhost:8000/api/fingerprint

# Activate license
curl -X POST http://localhost:8000/api/licenses/activate \
  -H "Content-Type: application/json" \
  -d '{
    "license_key": "XXXX-XXXX-XXXX-XXXX",
    "vm_fingerprint": "fingerprint_from_above"
  }'

# Response includes organization_token
# App stores: license_key + organization_token + vm_fingerprint
```

### Step 3: Application validates on startup
- Automatic validation every time app starts
- Requires HMAC signature for security
- Checks: active, not expired, VM matches, heartbeat not missed

### Step 4: Hourly heartbeat
- Background task runs every hour
- Keeps license alive
- If missed for 48+ hours: downgrades to PILOT mode

---

## Security Features

### VM Fingerprint Locking
- License locked to specific machine after activation
- Cannot be used on different VM/machine
- Prevents license sharing

### HMAC Signature
- All sensitive operations require HMAC-SHA256 signature
- Prevents tampering and replay attacks
- Uses organization_token as secret key

### Encrypted Storage
- License data stored encrypted in `~/.license/.license.dat`
- Uses Fernet symmetric encryption
- Key stored in `~/.license/.license.key` (chmod 600)

### Rate Limiting
- 60 requests per minute per IP
- Prevents brute force attacks

---

## Testing Your License System

### 1. Start License Server
```bash
cd /home/sina/netease/license_server
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

### 2. Start Main Application
```bash
cd /home/sina/netease
source .venv/bin/activate
uvicorn app.main:app --reload --port 8001
```

### 3. Create Test License
```bash
# Login as admin
curl -X POST http://localhost:8000/api/admin/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin123"}'

# Create pilot license
curl -X POST http://localhost:8000/api/admin/licenses \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_name": "Test User",
    "customer_email": "test@example.com",
    "organization_name": "Test Org",
    "plan_type": "pilot"
  }'
```

### 4. Test Audit Quota Limit
```bash
# Try to run 3 audits (pilot allows only 2)
# The 3rd audit should fail with HTTP 403
```

---

## Comparison Table

| Feature | Pilot | 100 Audit / 100 Hardening | 250 Audit / 250 Hardening | 500 Audit / 500 Hardening | Unlimited |
|---------|-------|---------------------------|----------------------------|----------------------------|-----------|
| **Duration** | 30 days | 365 days | 365 days | 365 days | 365 days |
| **Max Audits** | 2 | 100 | 250 | 500 | ∞ |
| **Max Hardens** | 2 | 100 | 250 | 500 | ∞ |
| **VM Lock** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Heartbeat Required** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Auto-downgrade** | N/A | → Pilot | → Pilot | → Pilot | → Pilot |

---

## Conclusion

✅ **Your license system is fully implemented and working**  
✅ **All 5 plans match your requirements exactly**  
✅ **Asset Management (asset creation and Auto Discovery) is NOT gated by license quotas**  
✅ **Audit and hardening operations are quota-enforced**  
✅ **Security features (VM lock, HMAC, encryption) are active**  
✅ **Heartbeat and auto-downgrade mechanisms are functional**

**No changes needed** - your license system is production-ready!

---

## Quick Reference

**License Server Endpoints:**
- `GET /api/fingerprint` - Get VM fingerprint
- `POST /api/licenses/activate` - Activate license
- `POST /api/licenses/validate` - Validate license
- `POST /api/licenses/heartbeat` - Send heartbeat
- `POST /api/licenses/consume` - Consume operation quota
- `POST /api/admin/login` - Admin login
- `POST /api/admin/licenses` - Create license (admin)
- `GET /api/admin/licenses` - List all licenses (admin)

**Main App Endpoints:**
- `POST /api/license/activate` - Activate license (frontend)
- `GET /api/license/status` - Get license status (frontend)

**Files to Review:**
- License plans: `license_server/app/crud.py` (`get_plan_limits()`)
- License model: `license_server/app/models.py` (`License`)
- Asset creation (no quota dependency): `app/modules/assets/router_with_auth.py`
- License middleware: `app/middleware/license_middleware.py`
- License state: `app/core/license_state.py`
