# License System - Complete Guide

## Overview

Your license system is a **separate server-based licensing solution** that validates and tracks license usage for your main application.

---

## Answers to Your Questions

### 1. How is the license checked? How often?

**License Checking Mechanism:**

The license is checked at **three different times**:

#### a) **Activation (One-time)**
- When user first enters their license key
- Endpoint: `POST /api/licenses/activate`
- Locks the license to a specific VM/machine using hardware fingerprint
- Returns `organization_token` which must be saved

#### b) **Validation (On Application Startup)**
- Every time the application starts
- Endpoint: `POST /api/licenses/validate`
- Requires: `license_key`, `organization_token`, `vm_fingerprint`
- Requires HMAC signature for security
- Checks:
  - License is active
  - Not expired
  - VM fingerprint matches
  - Heartbeat not missed (< 48 hours)

#### c) **Heartbeat (Every Hour - Background)**
- Automatic background check every hour
- Endpoint: `POST /api/licenses/heartbeat`
- Purpose: Keep license alive
- **Critical:** If heartbeat is missed for 48+ hours, license downgrades to PILOT mode

#### d) **Operation Consumption (Before Each Action)**
- Before performing a license-gated operation (audit or harden)
- Endpoint: `POST /api/licenses/consume`
- Checks quota and increments usage counter
- Prevents exceeding plan limits
- Note: Asset creation and Auto Discovery scans are NOT license-gated and never call this endpoint

**Summary:**
- ✅ Once: Activation
- ✅ Every app start: Validation
- ✅ Every hour: Heartbeat (background)
- ✅ Before each audit/harden operation: Consume

---

### 2. Where is the license stored?

**License Storage Locations:**

#### Server-Side (License Server)
- **Database:** PostgreSQL database
- **Table:** `licenses`
- **Location:** Configured in `.env` file (`DATABASE_URL`)
- **Contains:**
  - License key
  - Organization token
  - Customer info
  - Plan limits (max_audits, max_hardens)
  - Usage counters (used_audits, used_hardens)
  - VM fingerprint
  - Expiration date
  - Last heartbeat timestamp

#### Client-Side (Your Application)
The license server doesn't include client storage - **your frontend must store:**

```javascript
// Store in localStorage or secure storage
localStorage.setItem('license', JSON.stringify({
  license_key: 'XXXX-XXXX-XXXX-XXXX',
  organization_token: 'org_abc123...',
  vm_fingerprint: 'fingerprint_hash...'
}));
```

**Important:** 
- License key and organization token must be stored securely on client
- VM fingerprint should be fetched from server each time
- Never store admin credentials on client

---

### 3. How to generate a license key?

**License Generation Process:**

#### Option A: Admin API (Recommended)
**You (the vendor) generate licenses for customers**

1. Admin logs in to get JWT token
2. Admin creates license via API
3. Admin sends license key to customer

**Customer CANNOT generate their own license**

#### Option B: Manual Database Insert (Not Recommended)
Direct database manipulation - only for testing

**Process:**

```bash
# Step 1: Admin Login
curl -X POST http://localhost:8000/api/admin/login \
  -H "Content-Type: application/json" \
  -d '{
    "username": "admin",
    "password": "your-admin-password"
  }'

# Response: { "access_token": "eyJ...", "token_type": "bearer" }

# Step 2: Create License
curl -X POST http://localhost:8000/api/admin/licenses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer eyJ..." \
  -d '{
    "customer_name": "John Doe",
    "customer_email": "john@company.com",
    "organization_name": "ACME Corp",
    "plan_type": "plan_250"
  }'

# Response: Full license details including license_key
```

**The system automatically generates:**
- ✅ License key (format: `XXXX-XXXX-XXXX-XXXX`)
- ✅ Organization token (SHA-256 hash)
- ✅ Expiration date (based on plan)
- ✅ Plan limits

**You give the customer:**
- ✅ License key only
- ❌ NOT the organization token (they get it after activation)

---

### 4. Can users use the program without a license?

**Current Implementation: NO LICENSE CHECK IN MAIN APP**

Based on my analysis:
- ❌ The main application (`app/`) has **NO license integration**
- ❌ No license validation on startup
- ❌ No license checks before operations
- ✅ License server exists but is **separate and not connected**

**What this means:**
- Users can currently use all features without a license
- The license server is ready but not integrated into your main app
- **You need to add license checks to your main application**

**What you need to do:**

```python
# In your main app startup (app/main.py)
from fastapi import FastAPI, HTTPException
import requests

app = FastAPI()

LICENSE_SERVER_URL = "http://localhost:8000"

@app.on_event("startup")
async def validate_license():
    """Check license on application startup"""
    # Load from config/storage
    license_key = get_stored_license_key()
    org_token = get_stored_org_token()
    
    if not license_key:
        raise HTTPException(403, "No license found. Please activate.")
    
    # Get fingerprint
    fp_response = requests.get(f"{LICENSE_SERVER_URL}/api/fingerprint")
    fingerprint = fp_response.json()["fingerprint"]
    
    # Validate license
    response = requests.post(
        f"{LICENSE_SERVER_URL}/api/licenses/validate",
        json={
            "license_key": license_key,
            "organization_token": org_token,
            "vm_fingerprint": fingerprint
        },
        headers={
            "X-Signature": generate_signature(...),
            "X-Timestamp": timestamp
        }
    )
    
    if not response.json()["valid"]:
        raise HTTPException(403, "Invalid license")
```

---

## Complete Manual Testing Guide

### Prerequisites

1. **Install Dependencies**
```bash
cd license_server
pip install -r requirements.txt
```

2. **Setup Database**
```bash
# Install PostgreSQL
sudo apt-get install postgresql

# Create database
sudo -u postgres psql
CREATE DATABASE license_db;
CREATE USER license_user WITH PASSWORD 'secure_password';
GRANT ALL PRIVILEGES ON DATABASE license_db TO license_user;
\q
```

3. **Configure Environment**
```bash
cd license_server
cp .env.example .env
nano .env
```

Edit `.env`:
```env
DATABASE_URL=postgresql://license_user:secure_password@localhost:5432/license_db
SECRET_KEY=your-very-long-random-secret-key-here
ADMIN_USERNAME=admin
ADMIN_PASSWORD=MySecurePassword123
```

4. **Run Migrations**
```bash
cd license_server
alembic upgrade head
```

5. **Start License Server**
```bash
cd license_server
uvicorn app.main:app --reload --port 8000
```

---

### Step-by-Step Testing

#### Step 1: Verify Server is Running

```bash
curl http://localhost:8000/health
# Expected: {"status": "healthy"}
```

#### Step 2: Admin Login

```bash
curl -X POST http://localhost:8000/api/admin/login \
  -H "Content-Type: application/json" \
  -d '{
    "username": "admin",
    "password": "MySecurePassword123"
  }'
```

**Save the `access_token` from response!**

#### Step 3: Create a License

```bash
# Replace YOUR_TOKEN with the access_token from Step 2
curl -X POST http://localhost:8000/api/admin/licenses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "customer_name": "Test Customer",
    "customer_email": "test@example.com",
    "organization_name": "Test Company",
    "plan_type": "plan_250"
  }'
```

**Response will include:**
```json
{
  "id": 1,
  "license_key": "A1B2-C3D4-E5F6-G7H8",
  "organization_token": "org_abc123...",
  "customer_name": "Test Customer",
  "plan_type": "plan_250",
  "max_audits": 250,
  "max_hardens": 250,
  ...
}
```

**Save the `license_key`!** This is what you give to the customer.

#### Step 4: Get VM Fingerprint

```bash
curl http://localhost:8000/api/fingerprint
```

**Response:**
```json
{
  "fingerprint": "c74e64e016dd7134b9653d6f7343e1c4f3d4d10ade4b7143ac75a06d7c56c3aa"
}
```

**Save this fingerprint!**

#### Step 5: Activate License (Customer Side)

```bash
curl -X POST http://localhost:8000/api/licenses/activate \
  -H "Content-Type: application/json" \
  -d '{
    "license_key": "A1B2-C3D4-E5F6-G7H8",
    "vm_fingerprint": "c74e64e016dd7134b9653d6f7343e1c4f3d4d10ade4b7143ac75a06d7c56c3aa"
  }'
```

**Response:**
```json
{
  "valid": true,
  "message": "License activated successfully",
  "organization_token": "org_secret_token_here",
  "plan_type": "plan_250",
  "limits": { ... },
  "usage": { ... }
}
```

**Save the `organization_token`!** Customer needs this for all future requests.

#### Step 6: Validate License (With Signature)

This requires HMAC signature. Use Python:

```python
import requests
import hmac
import hashlib
import json
from datetime import datetime

# Your data
license_key = "A1B2-C3D4-E5F6-G7H8"
org_token = "org_secret_token_here"
fingerprint = "c74e64e016dd7134b9653d6f7343e1c4f3d4d10ade4b7143ac75a06d7c56c3aa"

# Create request data
data = {
    "license_key": license_key,
    "organization_token": org_token,
    "vm_fingerprint": fingerprint
}

# Generate signature
timestamp = datetime.utcnow().isoformat() + "Z"
payload = json.dumps(data, sort_keys=True)
message = f"{payload}:{timestamp}"
signature = hmac.new(
    org_token.encode(),
    message.encode(),
    hashlib.sha256
).hexdigest()

# Make request
response = requests.post(
    "http://localhost:8000/api/licenses/validate",
    json=data,
    headers={
        "X-Signature": signature,
        "X-Timestamp": timestamp
    }
)

print(response.json())
```

#### Step 7: Send Heartbeat

```bash
curl -X POST http://localhost:8000/api/licenses/heartbeat \
  -H "Content-Type: application/json" \
  -d '{
    "license_key": "A1B2-C3D4-E5F6-G7H8",
    "organization_token": "org_secret_token_here",
    "vm_fingerprint": "c74e64e016dd7134b9653d6f7343e1c4f3d4d10ade4b7143ac75a06d7c56c3aa"
  }'
```

#### Step 8: Consume Operation (With Signature)

```python
# Similar to Step 6, but with operation data
data = {
    "license_key": license_key,
    "organization_token": org_token,
    "vm_fingerprint": fingerprint,
    "operation_type": "audit",  # or "harden" — these are the only valid operation types
    "count": 1
}

# Generate signature (same as Step 6)
# ...

response = requests.post(
    "http://localhost:8000/api/licenses/consume",
    json=data,
    headers={
        "X-Signature": signature,
        "X-Timestamp": timestamp
    }
)

print(response.json())
```

#### Step 9: List All Licenses (Admin)

```bash
curl -X GET http://localhost:8000/api/admin/licenses \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
```

#### Step 10: Deactivate License (Admin)

```bash
curl -X DELETE http://localhost:8000/api/admin/licenses/A1B2-C3D4-E5F6-G7H8 \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
```

---

## Testing with Swagger UI

1. Open browser: `http://localhost:8000/docs`
2. Click on any endpoint to test
3. For admin endpoints:
   - First call `/api/admin/login`
   - Copy the `access_token`
   - Click "Authorize" button at top
   - Enter: `Bearer YOUR_TOKEN`
   - Now you can test admin endpoints

---

## Plan Types and Limits

| Plan | Duration | Audits | Hardens |
|------|----------|--------|---------|
| **PILOT** | 30 days | 2 | 2 |
| **PLAN_100** | 365 days | 100 | 100 |
| **PLAN_250** | 365 days | 250 | 250 |
| **PLAN_500** | 365 days | 500 | 500 |
| **UNLIMITED** | 365 days | ∞ | ∞ |

Asset Management (asset creation and Auto Discovery) has no license entitlement in any plan — it is not quota-limited.

---

## Important Security Notes

1. **Admin Credentials:** Change default admin password in production
2. **Secret Key:** Use cryptographically secure random key
3. **HTTPS:** Always use HTTPS in production
4. **Organization Token:** Never expose in logs or client-side code
5. **Database:** Secure PostgreSQL with strong password
6. **Rate Limiting:** Already implemented (60 requests/minute)

---

## What You Need to Do Next

### 1. Integrate License Checks into Main Application

Your main app (`app/`) currently has **NO license validation**. You need to:

- ✅ Add license validation on startup
- ✅ Add license checks before audit/harden operations
- ✅ Implement heartbeat background task
- ✅ Handle license errors gracefully
- ✅ Show license status in UI

### 2. Frontend Integration

- ✅ Create license activation screen
- ✅ Store license data securely
- ✅ Implement HMAC signature generation
- ✅ Add license status display
- ✅ Handle license expiration

### 3. Deployment

- ✅ Deploy license server separately
- ✅ Configure production database
- ✅ Set up HTTPS
- ✅ Configure firewall rules
- ✅ Set up monitoring

---

## Quick Reference

**License Flow:**
1. Admin creates license → gives `license_key` to customer
2. Customer activates with `license_key` + `vm_fingerprint` → gets `organization_token`
3. Customer stores `license_key` + `organization_token` + `vm_fingerprint`
4. App validates on startup (with signature)
5. App sends heartbeat every hour
6. App consumes audit/harden operations before those actions (with signature)

**Key Endpoints:**
- `GET /api/fingerprint` - Get VM fingerprint (no auth)
- `POST /api/licenses/activate` - Activate license (no auth)
- `POST /api/licenses/validate` - Validate license (signature required)
- `POST /api/licenses/heartbeat` - Keep alive (no signature)
- `POST /api/licenses/consume` - Use operation (signature required)
- `POST /api/admin/login` - Admin login
- `POST /api/admin/licenses` - Create license (admin only)

---

## 