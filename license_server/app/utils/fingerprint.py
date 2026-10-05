import hashlib
import logging
import platform
import uuid
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

def get_mac_address() -> Optional[str]:
    """Get the MAC address of the first non-loopback interface"""
    try:
        mac = ':'.join(['{:02x}'.format((uuid.getnode() >> elements) & 0xff)
                       for elements in range(0, 2*6, 2)][::-1])
        return mac
    except:
        return None

def get_machine_id() -> Optional[str]:
    """Get machine ID from system"""
    try:
        if platform.system() == "Linux":
            with open("/etc/machine-id", "r") as f:
                return f.read().strip()
        elif platform.system() == "Windows":
            result = subprocess.run(
                ["reg", "query", "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Cryptography", "/v", "MachineGuid"],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    if "MachineGuid" in line:
                        return line.split()[-1]
    except BaseException as e:
        logger.warning(f"[Fingerprint] could not read the machine id: {e}")
    return None

def get_vm_fingerprint() -> str:
    """Generate unique VM fingerprint based on hardware identifiers"""
    components = []
    
    # MAC address
    mac = get_mac_address()
    if mac:
        components.append(f"mac:{mac}")
    
    # Machine ID
    machine_id = get_machine_id()
    if machine_id:
        components.append(f"machine:{machine_id}")
    
    # Hostname
    try:
        hostname = platform.node()
        if hostname:
            components.append(f"host:{hostname}")
    except BaseException as exc:
        logger.warning(f"[Fingerprint] could not read the hostname, excluding it: {exc}")
    
    # CPU info
    try:
        cpu = platform.processor()
        if cpu:
            components.append(f"cpu:{cpu}")
    except BaseException as e:
        logger.warning(f"[Fingerprint] could not read the CPU model, excluding it: {e}")
    
    # Combine and hash
    fingerprint_string = "|".join(components)
    return hashlib.sha256(fingerprint_string.encode()).hexdigest()
