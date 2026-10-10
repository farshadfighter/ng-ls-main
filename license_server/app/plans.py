"""
The NGCorion plan catalog: what a license entitles, by product code (SKU).

A license is sold per number of managed assets. Each plan has a base asset
limit; 50-asset add-on packs raise it, so an organisation with 600 assets can
buy the 500 plan and two add-ons instead of the 1,000 plan:

    max_assets = plan.base_assets + 50 * addon_packs     (None = unlimited)

There are no other quotas: audits and hardening runs are not counted.

The pilot is the trial (5 assets, one month). It is also what NGCorion falls
back to on its own when it has not reached this server for 48 hours; that
fallback happens on the client, from its last signed answer, and is undone
when the connection returns. Nothing here changes a license because it was
offline.

The catalog lives in code, not in the database: a plan is part of what the
signed answers promise, and changing one should be a reviewed change.
licenses.plan_code stores the SKU; the limit is stored with the license
(licenses.max_assets) so a license keeps what it was sold with even if the
catalog changes later.
"""
from dataclasses import dataclass
from typing import Dict, Optional

ADDON_CODE = "NGC-ADD-50-1Y"
ADDON_ASSETS = 50
MAX_ADDON_PACKS = 200   # 10,000 extra assets; anything above is a typo, not an order


@dataclass(frozen=True)
class Plan:
    code: str
    name: str
    base_assets: Optional[int]     # None = unlimited
    duration_days: int
    pilot: bool = False

    @property
    def addons_allowed(self) -> bool:
        """Add-ons extend a finite, paid plan; not Unlimited, not the trial."""
        return self.base_assets is not None and not self.pilot

    def max_assets(self, addon_packs: int = 0) -> Optional[int]:
        if self.base_assets is None:
            return None
        return self.base_assets + ADDON_ASSETS * (addon_packs or 0)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "base_assets": self.base_assets,
            "duration_days": self.duration_days,
            "pilot": self.pilot,
            "addons_allowed": self.addons_allowed,
        }


PILOT_CODE = "NGC-PILOT-5-1M"

PLANS: Dict[str, Plan] = {p.code: p for p in (
    Plan(PILOT_CODE, "NGCorion Pilot", 5, 30, pilot=True),
    Plan("NGC-ESS-25-1Y", "NGCorion Essential", 25, 365),
    Plan("NGC-PRO-100-1Y", "NGCorion Professional", 100, 365),
    Plan("NGC-BUS-250-1Y", "NGCorion Business", 250, 365),
    Plan("NGC-ENT-500-1Y", "NGCorion Enterprise", 500, 365),
    Plan("NGC-ENT-1000-1Y", "NGCorion Enterprise", 1000, 365),
    Plan("NGC-ENT-2500-1Y", "NGCorion Enterprise", 2500, 365),
    Plan("NGC-ENT-ULT-1Y", "NGCorion Enterprise Unlimited", None, 365),
)}

# The plan names of the audit/hardening-quota catalog (licenses.plan_type up to
# migration 006), for the portal and for migrating existing licenses. Each maps
# to the plan with the same number (of assets now).
LEGACY_PLAN_TYPES: Dict[str, str] = {
    "pilot": PILOT_CODE,
    "plan_100": "NGC-PRO-100-1Y",
    "plan_250": "NGC-BUS-250-1Y",
    "plan_500": "NGC-ENT-500-1Y",
    "unlimited": "NGC-ENT-ULT-1Y",
}


class UnknownPlan(ValueError):
    """Not a plan code (or a legacy plan name) this server sells."""


class InvalidAddons(ValueError):
    """Add-on packs the plan cannot take, or a count out of range."""


def resolve(code: str) -> Plan:
    """The plan for a SKU (any case) or a legacy plan name ("plan_250")."""
    value = (code or "").strip()
    plan = PLANS.get(value.upper()) or PLANS.get(LEGACY_PLAN_TYPES.get(value.lower(), ""))
    if plan is None:
        raise UnknownPlan(f"unknown plan {code!r}; known plans: {', '.join(PLANS)}")
    return plan


def check_addons(plan: Plan, addon_packs: int) -> int:
    """addon_packs when the plan takes that many; InvalidAddons otherwise."""
    packs = int(addon_packs or 0)
    if packs < 0 or packs > MAX_ADDON_PACKS:
        raise InvalidAddons(f"add-on packs must be between 0 and {MAX_ADDON_PACKS}")
    if packs and not plan.addons_allowed:
        raise InvalidAddons(f"{plan.code} does not take add-on packs")
    return packs


def catalog() -> dict:
    """The catalog as the admin UI and the portal read it."""
    return {
        "plans": [p.as_dict() for p in PLANS.values()],
        "addon": {"code": ADDON_CODE, "assets": ADDON_ASSETS, "max_packs": MAX_ADDON_PACKS},
    }
