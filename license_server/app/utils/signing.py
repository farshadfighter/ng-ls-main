import hmac
import hashlib
import json
from datetime import datetime, timezone

def generate_signature(data: dict, secret: str, timestamp: str) -> str:
    """Generate HMAC-SHA256 signature for request data"""
    # Sort keys for consistent serialization
    payload = json.dumps(data, sort_keys=True)
    message = f"{payload}:{timestamp}"
    signature = hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    return signature

def verify_signature(
    data: dict,
    signature: str,
    timestamp: str,
    secret: str,
    max_age_seconds: int = 300
) -> bool:
    """Verify HMAC-SHA256 signature and timestamp"""
    # Check timestamp freshness
    try:
        request_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        current_time = datetime.now(timezone.utc)
        age = (current_time - request_time).total_seconds()
        
        if abs(age) > max_age_seconds:
            return False
    except (ValueError, AttributeError):
        return False
    
    # Verify signature
    expected_signature = generate_signature(data, secret, timestamp)
    return hmac.compare_digest(signature, expected_signature)
