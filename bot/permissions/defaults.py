# Centralized registry of all permission flags and their default roles authority presets
PERMISSIONS_REGISTRY = {
    "can_invite": {"leader": True, "default": False},
    "can_kick": {"leader": True, "default": False},
    "can_accept_applications": {"leader": True, "default": False},
    "can_reject_applications": {"leader": True, "default": False},
    "can_promote": {"leader": True, "default": False},
    "can_demote": {"leader": True, "default": False},
    "can_edit_clan_description": {"leader": True, "default": False},
    "can_edit_clan_banner": {"leader": True, "default": False},
    "can_edit_clan_icon": {"leader": True, "default": False},
    "can_edit_clan_name": {"leader": True, "default": False},
    "can_manage_roles": {"leader": True, "default": False},
    "can_manage_permissions": {"leader": True, "default": False},
    "can_manage_bank": {"leader": True, "default": False},
    "can_deposit_coins": {"leader": True, "default": True},  # Everyone can deposit coins by default
    "can_withdraw_coins": {"leader": True, "default": False},
    "can_start_clan_war": {"leader": True, "default": False},
    "can_declare_alliance": {"leader": True, "default": False},
    "can_manage_diplomacy": {"leader": True, "default": False},
    "can_create_events": {"leader": True, "default": False},
    "can_manage_quests": {"leader": True, "default": False},
    "can_ping_clan": {"leader": True, "default": False},
    "can_view_logs": {"leader": True, "default": False},
    "can_transfer_leadership": {"leader": True, "default": False},
    "can_delete_clan": {"leader": True, "default": False},
}

def get_default_permission_values(is_leader: bool = False) -> dict[str, bool]:
    """Returns the default values mapping for all registered permissions."""
    return {
        key: config["leader"] if is_leader else config["default"]
        for key, config in PERMISSIONS_REGISTRY.items()
    }
