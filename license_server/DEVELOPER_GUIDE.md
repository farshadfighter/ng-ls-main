# Developer Guide: License Management

> **Licensing v3 (asset-based plans).** Plans are now SKUs with an asset
> limit plus 50-asset add-on packs; audits and hardening runs are no longer
> counted, `/api/licenses/consume`, `plan_type` and the `max_/used_audits|hardens`
> fields are gone, and the server no longer downgrades a license that was
> offline (NGCorion falls back to the pilot on its own, reversibly). See
> `LICENSE_PLANS_SUMMARY.md`. Where this document still describes quotas,
> `/consume` or `plan_type`, it describes the earlier version.

> **Note on the `client` package.** The standalone SDK that used to live in
> `license_server/client/` (`from client import LicenseClient, HeartbeatService`)
> has been removed — it was an unmaintained duplicate of the client the product
> actually runs. The maintained implementation lives in the main NGcorion app:
> `app/core/license_client.py` and `app/core/heartbeat.py`. The
> `from client import ...` snippets below are kept as illustrations of the call
> sequence; import from `app.core.license_client` instead.

## Signed licensing answers (read this first)

Every answer of `/api/licenses/activate`, `/validate` and `/heartbeat`
carries `signed_token`: the answer signed with the Ed25519 key in
`LICENSE_SIGNING_PRIVATE_KEY` (`app/utils/license_token.py`). NGCorion has the
public half built in (`app/core/license_keys.py`) and acts only on a token it
can verify, whose nonce matches its request and whose fingerprint is its own
machine. A server without the private key cannot unlock NGCorion, whatever it
answers.

* **Key setup:** `python scripts/generate_signing_key.py` prints the private key
  (for this server's `.env` only) and the public key with its id (for
  NGCorion). `GET /api/signing-key` shows which public key the running server
  uses.
* **No key, no answers:** without `LICENSE_SIGNING_PRIVATE_KEY` the licensing
  endpoints answer 503.
* **Machine binding:** the client computes the fingerprint of its own machine
  and sends it; a license is bound to the first fingerprint it is activated
  with. `POST /api/admin/licenses/{key}/reset-binding` (the "Release machine"
  button in the admin UI) frees it for a hardware move or reinstall.
* **Legacy bindings:** NGCorion releases before signing asked
  `GET /api/fingerprint`, which returns this server's own fingerprint (a
  different one each time its container is recreated), so their bindings
  identify no customer machine. `licenses.fingerprint_source` (migration 005)
  tells them apart: NULL for those, `client` for bindings made by a release
  that computes the fingerprint itself (it sends a nonce). A NULL binding may
  move once to a client-computed fingerprint on its next activation. The
  endpoint stays only for the old releases.
* **Key rotation:** add the new public key to NGCorion next to the old one,
  ship that release, switch this server to the new private key, and drop the
  old public key in a later release.
* **Tests:** `tests/test_signed_licensing.py` (needs PostgreSQL). Like the
  portal tests, run each test module on its own: they pin settings before the
  app is imported.

## For Backend Developers: Generating Licenses

### Method 1: Using the Admin API (Recommended)

**Step 1: Get Admin JWT Token**

```bash
curl -X POST http://localhost:8000/api/admin/login \
  -H "Content-Type: application/json" \
  -d '{
    "username": "admin",
    "password": "your-admin-password"
  }'
```

Response:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

**Step 2: Create License**

```bash
TOKEN="your-jwt-token-here"

curl -X POST http://localhost:8000/api/admin/licenses \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_name": "شرکت نمونه",
    "customer_email": "customer@example.com",
    "organization_name": "سازمان مشتری",
    "plan_type": "plan_250"
  }'
```

Response:
```json
{
  "id": 1,
  "license_key": "A3F2-9K7L-M4N8-P2Q5",
  "organization_token": "a1b2c3d4e5f6...",
  "customer_name": "شرکت نمونه",
  "customer_email": "customer@example.com",
  "organization_name": "سازمان مشتری",
  "plan_type": "plan_250",
  "max_audits": 250,
  "max_hardens": 250,
  "used_audits": 0,
  "used_hardens": 0,
  "vm_fingerprint": null,
  "is_active": true,
  "is_pilot_mode": false,
  "created_at": "2025-04-23T10:30:00Z",
  "activated_at": null,
  "expires_at": "2026-04-23T10:30:00Z",
  "last_validated_at": null,
  "last_heartbeat_at": null
}
```

**Important:** Send the `license_key` to the customer. Keep the `organization_token` secret (it's used internally for validation).

### Method 2: Using Python Script

> **Note.** Admin sign-in now needs a personal account, a Cloudflare
> Turnstile token and (for a new account) a password change, so the script
> below no longer works unattended. For automated license creation use the
> portal integration, `POST /internal/portal/licenses` (signed service tokens,
> IP allow list; see `app/routers/internal_portal.py`).

Create `generate_license.py`:

```python
#!/usr/bin/env python3
import requests
import sys

SERVER_URL = "http://localhost:8000"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "your-admin-password"

def get_admin_token():
    """Login and get JWT token"""
    response = requests.post(
        f"{SERVER_URL}/api/admin/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )
    response.raise_for_status()
    return response.json()["access_token"]

def create_license(token, customer_data):
    """Create a new license"""
    response = requests.post(
        f"{SERVER_URL}/api/admin/licenses",
        headers={"Authorization": f"Bearer {token}"},
        json=customer_data
    )
    response.raise_for_status()
    return response.json()

def main():
    # Get admin token
    print("Logging in as admin...")
    token = get_admin_token()
    
    # Customer data
    customer_data = {
        "customer_name": input("Customer Name: "),
        "customer_email": input("Customer Email: "),
        "organization_name": input("Organization Name: "),
        "plan_type": input("Plan Type (pilot/plan_100/plan_250/plan_500/unlimited): ")
    }
    
    # Create license
    print("\nCreating license...")
    license_data = create_license(token, customer_data)
    
    # Display results
    print("\n" + "="*60)
    print("LICENSE CREATED SUCCESSFULLY")
    print("="*60)
    print(f"License Key: {license_data['license_key']}")
    print(f"Customer: {license_data['customer_name']}")
    print(f"Email: {license_data['customer_email']}")
    print(f"Plan: {license_data['plan_type']}")
    print(f"Expires: {license_data['expires_at']}")
    print(f"Max Audits: {license_data['max_audits']}")
    print(f"Max Hardens: {license_data['max_hardens']}")
    print("="*60)
    print("\nSend the License Key to the customer.")
    print("They will use it to activate their software.")

if __name__ == "__main__":
    main()
```

Usage:
```bash
python generate_license.py
```

### Method 3: Direct Database Insert (Not Recommended)

Only use this for testing or emergency situations:

```python
from app.database import SessionLocal
from app import crud, schemas, models

db = SessionLocal()

license_data = schemas.LicenseCreate(
    customer_name="Test Customer",
    customer_email="test@example.com",
    organization_name="Test Org",
    plan_type=models.PlanType.PLAN_250
)

license = crud.create_license(db, license_data)
print(f"License Key: {license.license_key}")
db.close()
```

### Plan Types Reference

| Plan Type | Duration | Max Audits / Hardens | Use Case |
|-----------|----------|-----------------------|----------|
| `pilot` | 30 days | 2 each | Trial/Testing |
| `plan_100` | 365 days | 100 each | Small network |
| `plan_250` | 365 days | 250 each | Medium network |
| `plan_500` | 365 days | 500 each | Large network |
| `unlimited` | 365 days | Unlimited | Enterprise |

Asset Management (asset creation and Auto Discovery) has no license entitlement in any plan.

---

## For Frontend Developers: Integration Guide

### Overview

The frontend needs to implement a license activation and validation flow. The client SDK handles most of the complexity (encryption, signing, fingerprinting).

### Architecture

```
[Frontend UI] → [Client SDK] → [License Server API]
                     ↓
              [Encrypted Storage]
              (~/.license/)
```

### Required Frontend Components

#### 1. License Activation Screen

**Purpose:** First-time setup where user enters their license key.

**UI Elements:**
- Text input for license key (format: XXXX-XXXX-XXXX-XXXX)
- "Activate" button
- Loading indicator
- Error/success messages

**Implementation:**

```python
from client import LicenseClient

class LicenseActivationScreen:
    def __init__(self, server_url):
        self.client = LicenseClient(server_url=server_url)
    
    def activate_license(self, license_key):
        """
        Called when user clicks "Activate" button
        
        Args:
            license_key: User-entered license key (e.g., "A3F2-9K7L-M4N8-P2Q5")
        
        Returns:
            dict: Activation result with plan details
        """
        try:
            result = self.client.activate(license_key)
            
            # Success - show user their plan details
            return {
                "success": True,
                "plan_type": result['plan_type'],
                "is_pilot": result['is_pilot_mode'],
                "limits": result['limits'],
                "message": "License activated successfully!"
            }
        
        except Exception as e:
            # Error - show user-friendly message
            return {
                "success": False,
                "message": str(e)
            }
```

**User Flow:**
1. User receives license key via email
2. User opens application for first time
3. Application shows activation screen
4. User enters license key
5. Application calls `activate_license()`
6. On success: redirect to main application
7. On error: show error message, allow retry

#### 2. License Status Display

**Purpose:** Show current license status and usage.

**UI Elements:**
- Plan type badge
- Expiration date
- Usage bars/meters for each operation type
- "Refresh" button

**Implementation:**

```python
from client import LicenseClient

class LicenseStatusWidget:
    def __init__(self, server_url):
        self.client = LicenseClient(server_url=server_url)
    
    def get_license_status(self):
        """
        Get current license status and usage
        
        Returns:
            dict: License status with limits and usage
        """
        try:
            result = self.client.validate()
            
            if not result['valid']:
                return {
                    "valid": False,
                    "message": result['message']
                }
            
            # Calculate usage percentages
            limits = result['limits']
            usage = result['usage']
            
            usage_percent = {}
            for key in ['audits', 'hardens']:
                max_key = f"max_{key}"
                used_key = f"used_{key}"
                
                if limits[max_key] is None:  # Unlimited
                    usage_percent[key] = 0
                else:
                    usage_percent[key] = (usage[used_key] / limits[max_key]) * 100
            
            return {
                "valid": True,
                "plan_type": result['plan_type'],
                "is_pilot": result['is_pilot_mode'],
                "limits": limits,
                "usage": usage,
                "usage_percent": usage_percent
            }
        
        except Exception as e:
            return {
                "valid": False,
                "message": f"Failed to validate license: {e}"
            }
```

**Display Example:**

```
┌─────────────────────────────────────┐
│ License Status                      │
├─────────────────────────────────────┤
│ Plan: 250 Audit / 250 Hardening     │
│ Status: Active                      │
│ Expires: 2026-04-23                 │
├─────────────────────────────────────┤
│ Usage:                              │
│ Audits:      [████░░░░░░] 100/250   │
│ Hardens:     [██░░░░░░░░] 50/250    │
└─────────────────────────────────────┘
```

#### 3. Operation Consumption

**Purpose:** Consume operation quota when user performs actions.

**Implementation:**

```python
from client import LicenseClient

class OperationManager:
    def __init__(self, server_url):
        self.client = LicenseClient(server_url=server_url)
    
    def perform_operation(self, operation_type, count=1):
        """
        Consume operation quota before performing action

        Note: Asset creation and Auto Discovery are NOT license-gated and
        should never be passed here — only "audit" and "harden" consume
        quota.

        Args:
            operation_type: One of "audit", "harden"
            count: Number of operations to consume
        
        Returns:
            dict: Result with success status
        """
        try:
            result = self.client.consume(operation_type=operation_type, count=count)
            
            if result['valid']:
                return {
                    "success": True,
                    "remaining": self._calculate_remaining(result),
                    "message": f"{operation_type.capitalize()} operation consumed"
                }
            else:
                return {
                    "success": False,
                    "message": result['message']
                }
        
        except Exception as e:
            return {
                "success": False,
                "message": f"Failed to consume operation: {e}"
            }
    
    def _calculate_remaining(self, result):
        """Calculate remaining operations"""
        limits = result['limits']
        usage = result['usage']
        
        remaining = {}
        for key in ['audits', 'hardens']:
            max_key = f"max_{key}"
            used_key = f"used_{key}"
            
            if limits[max_key] is None:
                remaining[key] = "Unlimited"
            else:
                remaining[key] = limits[max_key] - usage[used_key]
        
        return remaining
```

**Usage in Application:**

```python
# Before performing an audit
op_manager = OperationManager(server_url="http://localhost:8000")

result = op_manager.perform_operation("audit", count=1)

if result['success']:
    # Proceed with the audit
    perform_audit()
    print(f"Remaining audits: {result['remaining']['audits']}")
else:
    # Show error to user
    show_error(result['message'])
```

Note: Asset creation and Auto Discovery scans never call `perform_operation()` — they run unconditionally with no quota check.

#### 4. Background Heartbeat Service

**Purpose:** Maintain license validity with automatic heartbeat.

**Implementation:**

```python
from client import LicenseClient, HeartbeatService

class ApplicationLifecycle:
    def __init__(self, server_url):
        self.client = LicenseClient(server_url=server_url)
        self.heartbeat = None
    
    def start_application(self):
        """Called when application starts"""
        # Start heartbeat service (every hour)
        self.heartbeat = HeartbeatService(
            client=self.client,
            interval_seconds=3600  # 1 hour
        )
        self.heartbeat.start()
        print("Heartbeat service started")
    
    def stop_application(self):
        """Called when application closes"""
        if self.heartbeat:
            self.heartbeat.stop()
            print("Heartbeat service stopped")
```

#### 5. Error Handling

**Common Error Scenarios:**

```python
def handle_license_error(error):
    """
    Handle common license errors with user-friendly messages
    """
    error_messages = {
        "License not activated": {
            "title": "License Not Activated",
            "message": "Please activate your license to continue.",
            "action": "show_activation_screen"
        },
        "License expired": {
            "title": "License Expired",
            "message": "Your license has expired. Please contact support to renew.",
            "action": "show_renewal_info"
        },
        "License invalid": {
            "title": "Invalid License",
            "message": "Your license is invalid. Please contact support.",
            "action": "show_support_contact"
        },
        "Quota exceeded": {
            "title": "Quota Exceeded",
            "message": "You have reached your operation limit. Please upgrade your plan.",
            "action": "show_upgrade_options"
        },
        "VM fingerprint mismatch": {
            "title": "License Locked",
            "message": "This license is locked to another machine.",
            "action": "show_support_contact"
        },
        "Rate limit exceeded": {
            "title": "Too Many Requests",
            "message": "Please wait a moment and try again.",
            "action": "retry_after_delay"
        }
    }
    
    for key, info in error_messages.items():
        if key.lower() in str(error).lower():
            return info
    
    # Default error
    return {
        "title": "Error",
        "message": str(error),
        "action": "show_error_dialog"
    }
```

### Complete Frontend Integration Example

```python
from client import LicenseClient, HeartbeatService

class NetworkAssetManagerApp:
    def __init__(self):
        self.server_url = "http://localhost:8000"
        self.client = LicenseClient(server_url=self.server_url)
        self.heartbeat = None
        self.license_status = None
    
    def startup(self):
        """Application startup sequence"""
        # Check if license is activated
        try:
            self.license_status = self.client.validate()
            
            if not self.license_status['valid']:
                # Show activation screen
                self.show_activation_screen()
                return
            
            # Start heartbeat
            self.heartbeat = HeartbeatService(self.client, interval_seconds=3600)
            self.heartbeat.start()
            
            # Show main application
            self.show_main_screen()
        
        except Exception as e:
            if "License not activated" in str(e):
                self.show_activation_screen()
            else:
                self.show_error(f"Startup error: {e}")
    
    def show_activation_screen(self):
        """Show license activation UI"""
        print("=== License Activation ===")
        license_key = input("Enter your license key: ")
        
        try:
            result = self.client.activate(license_key)
            print(f"✓ Activated: {result['plan_type']}")
            self.startup()  # Retry startup
        except Exception as e:
            print(f"✗ Activation failed: {e}")
            self.show_activation_screen()  # Retry
    
    def show_main_screen(self):
        """Main application screen"""
        print("\n=== Network Asset Manager ===")
        print(f"Plan: {self.license_status['plan_type']}")
        print(f"Status: Active")
        print("\nOperations:")
        print("1. Add Asset (not license-gated)")
        print("2. Run Discovery (not license-gated)")
        print("3. Run Audit")
        print("4. Run Hardening")
        print("5. View License Status")
        print("0. Exit")
        
        choice = input("\nSelect operation: ")
        self.handle_operation(choice)
    
    def handle_operation(self, choice):
        """Handle user operation selection"""
        # Only "audit" and "harden" are license-gated operation types.
        # Asset creation and Auto Discovery run unconditionally and never
        # call self.client.consume().
        unrestricted_operations = {
            "1": "asset",
            "2": "discovery",
        }
        quota_operations = {
            "3": "audit",
            "4": "harden",
        }
        
        if choice == "5":
            self.show_license_status()
            return
        
        if choice == "0":
            self.shutdown()
            return
        
        if choice in unrestricted_operations:
            op_type = unrestricted_operations[choice]
            print(f"✓ {op_type.capitalize()} operation started (no quota check)")
            self.perform_actual_operation(op_type)
        
        elif choice in quota_operations:
            op_type = quota_operations[choice]
            
            try:
                result = self.client.consume(operation_type=op_type, count=1)
                
                if result['valid']:
                    print(f"✓ {op_type.capitalize()} operation consumed")
                    # Perform actual operation here
                    self.perform_actual_operation(op_type)
                else:
                    print(f"✗ Failed: {result['message']}")
            
            except Exception as e:
                print(f"✗ Error: {e}")
        
        self.show_main_screen()
    
    def show_license_status(self):
        """Display current license status"""
        try:
            status = self.client.validate()
            
            print("\n=== License Status ===")
            print(f"Plan: {status['plan_type']}")
            print(f"Valid: {status['valid']}")
            print("\nLimits:")
            for key, value in status['limits'].items():
                print(f"  {key}: {value if value else 'Unlimited'}")
            print("\nUsage:")
            for key, value in status['usage'].items():
                print(f"  {key}: {value}")
        
        except Exception as e:
            print(f"✗ Error: {e}")
        
        input("\nPress Enter to continue...")
        self.show_main_screen()
    
    def perform_actual_operation(self, op_type):
        """Perform the actual operation (placeholder)"""
        print(f"Performing {op_type}...")
        # Your actual operation logic here
    
    def shutdown(self):
        """Application shutdown"""
        if self.heartbeat:
            self.heartbeat.stop()
        print("Application closed")

# Run application
if __name__ == "__main__":
    app = NetworkAssetManagerApp()
    app.startup()
```

### Frontend Checklist

- [ ] License activation screen with input validation
- [ ] License status display widget
- [ ] Operation consumption before each action
- [ ] Background heartbeat service
- [ ] Error handling for all license operations
- [ ] Graceful degradation when server unavailable
- [ ] User-friendly error messages
- [ ] License renewal/upgrade prompts
- [ ] Support contact information

### Testing Frontend Integration

```python
# test_frontend.py
from client import LicenseClient

def test_activation():
    client = LicenseClient(server_url="http://localhost:8000")
    result = client.activate("YOUR-TEST-LICENSE-KEY")
    assert result['valid'] == True
    print("✓ Activation test passed")

def test_validation():
    client = LicenseClient(server_url="http://localhost:8000")
    result = client.validate()
    assert result['valid'] == True
    print("✓ Validation test passed")

def test_consumption():
    client = LicenseClient(server_url="http://localhost:8000")
    result = client.consume(operation_type="audit", count=1)
    assert result['valid'] == True
    print("✓ Consumption test passed")

if __name__ == "__main__":
    test_activation()
    test_validation()
    test_consumption()
    print("\n✓ All tests passed!")
```

### Configuration

**Frontend Configuration File:**

```python
# config.py
class Config:
    # License Server
    LICENSE_SERVER_URL = "http://localhost:8000"  # Change for production
    
    # Heartbeat
    HEARTBEAT_INTERVAL = 3600  # 1 hour in seconds
    
    # Retry Settings
    MAX_RETRIES = 3
    RETRY_DELAY = 5  # seconds
    
    # UI Messages
    MESSAGES = {
        "activation_success": "License activated successfully!",
        "activation_failed": "Failed to activate license. Please check your key.",
        "quota_exceeded": "Operation limit reached. Please upgrade your plan.",
        "license_expired": "Your license has expired. Please renew.",
    }
```

---

## Summary

**Backend (License Generation):**
1. Use admin API with JWT authentication
2. Create licenses with customer details and plan type
3. Send license key to customer via email

**Frontend (License Integration):**
1. Implement activation screen for first-time setup
2. Display license status and usage
3. Consume operations before each action
4. Run background heartbeat service
5. Handle errors gracefully with user-friendly messages

**Key Files:**
- Backend: Use admin API endpoints
- Frontend: Use `client/` SDK modules
- Config: Update server URL for production
