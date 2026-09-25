from .discovery import claim_discovered_agent, discover_agent, discovery_agent_id
from .registry import (
    Identity,
    register_identity,
    issue_token,
    renew_token,
    revoke_token,
    load_registry,
    load_token_registry,
    resolve,
    client_info_name,
)

__all__ = [
    "Identity",
    "register_identity",
    "issue_token",
    "renew_token",
    "revoke_token",
    "load_registry",
    "load_token_registry",
    "resolve",
    "client_info_name",
    "claim_discovered_agent",
    "discover_agent",
    "discovery_agent_id",
]
