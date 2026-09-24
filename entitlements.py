"""What each plan may do. Present, wired in, and switched off.

Every metered feature already asks this module for permission. Today the answer is always yes,
because ENFORCE is False - the site launches free and open and nothing here changes what anyone
sees. When we do introduce tiers, the work is editing LIMITS and setting ENFORCE (or the
ENFORCE_ENTITLEMENTS env var), not going back through the endpoints to add checks.

The numbers in LIMITS are placeholders. They are deliberately NOT a pricing decision - we are
logging real usage now (usage.py) precisely so those numbers can be set from what people actually
do rather than from a guess made before launch.
"""

import os

# The one switch. Everything below is inert until this is true.
ENFORCE = os.environ.get("ENFORCE_ENTITLEMENTS", "").strip().lower() in ("1", "true", "yes", "on")

FREE = "free"
PLANS = (FREE, "pro", "founding", "team")

# Metered features. The name is what gets written to usage_events.event_type, so usage and
# entitlements always talk about the same thing.
FEATURES = ("ask_ntd", "ask_cig", "card_export", "data_export")

# plan -> feature -> calls allowed per PERIOD_DAYS. None means unlimited.
# PLACEHOLDERS, inert. Set from the usage data before any of this is switched on.
PERIOD_DAYS = 30
LIMITS = {
    "free":     {"ask_ntd": 25, "ask_cig": 25, "card_export": 10, "data_export": 0},
    "pro":      {"ask_ntd": None, "ask_cig": None, "card_export": None, "data_export": 100},
    "founding": {"ask_ntd": None, "ask_cig": None, "card_export": None, "data_export": None},
    "team":     {"ask_ntd": None, "ask_cig": None, "card_export": None, "data_export": None},
}


def normalize(plan):
    """Anything unrecognised is treated as free - including None, for an anonymous visitor."""
    p = (plan or "").strip().lower()
    return p if p in PLANS else FREE


def limit_for(feature, plan=FREE):
    """The configured allowance, whether or not it is being enforced. None means unlimited."""
    return LIMITS.get(normalize(plan), {}).get(feature)


def can_use(feature, plan=FREE, used=None):
    """-> (allowed, limit_or_none).

    `used` is this plan-holder's count in the current period, when the caller knows it. While
    ENFORCE is False this always allows, and the limit is returned anyway so a caller can show or
    log "12 of 25" without anything being blocked.
    """
    lim = limit_for(feature, plan)
    if not ENFORCE or lim is None:
        return True, lim
    if used is None:
        return True, lim            # nothing counted yet: never block on missing information
    return used < lim, lim


def describe():
    """For the Command Center's Usage view: what would apply if this were switched on."""
    return {"enforcing": ENFORCE, "period_days": PERIOD_DAYS, "plans": list(PLANS),
            "features": list(FEATURES), "limits": LIMITS}
