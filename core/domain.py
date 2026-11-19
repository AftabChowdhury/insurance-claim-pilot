"""Shared business vocabulary."""

# loss_type -> (policy_type used to filter the search index, display label)
LOSS_TYPES: dict[str, tuple[str | None, str]] = {
    "water_sudden": ("home", "Water damage – burst pipe"),
    "flood": ("home", "Water damage – flood / rain"),
    "storm": ("home", "Storm / roof damage"),
    "fire": ("home", "Fire / smoke"),
    "theft": ("auto", "Vehicle theft"),
    "vehicle_damage": ("auto", "Vehicle damage"),
    "travel": (None, "Travel problem"),          # no travel policy indexed yet: search all policy types
    "tenant": ("home", "Tenant problem"),        # renters / contents cover is indexed as policy_type home
    "home_owner": ("home", "Home owner problem"),
    "other": (None, "Other / unclear"),
}

# Final claim statuses written to the claims system
STATUS_RECEIVED = "RECEIVED"
STATUS_APPROVED = "APPROVED"
STATUS_DENIED = "DENIED"
STATUS_PENDING_INFO = "PENDING_INFO"
STATUS_ESCALATED = "ESCALATED"
STATUS_FLAGGED = "FLAGGED_SECURITY"

DECISION_TO_STATUS = {
    "approve": STATUS_APPROVED,
    "deny": STATUS_DENIED,
    "need_info": STATUS_PENDING_INFO,
}


def policy_type_for(loss_type: str) -> str | None:
    return LOSS_TYPES.get(loss_type, (None, ""))[0]


def label_for(loss_type: str) -> str:
    return LOSS_TYPES.get(loss_type, (None, loss_type))[1]
