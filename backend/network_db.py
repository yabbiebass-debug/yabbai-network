"""
Shared settings/connection store for the YABBAI network.

One Mongo document holds all runtime-configurable connections (LLM routing tiers,
payment/auth connections). Read at request time so rotating a key needs no restart.
"""

import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

_client = AsyncIOMotorClient(os.environ["MONGO_URL"])
_db = _client[os.environ["DB_NAME"]]
db = _db
SETTINGS_ID = "network_settings"

# secret fields are stored but never echoed back to the client
SECRET_FIELDS = {
    "nvidia_api_key", "yabbai_api_key", "groq_api_key", "grok_api_key",
    "supabase_service_key", "stripe_secret_key", "paypal_secret",
    "google_client_secret", "tavily_api_key",
}

DEFAULTS = {
    "route_order": ["nvidia", "groq", "grok", "yabbai", "emergent"],
    "nvidia_enabled": True,
    "nvidia_base_url": "https://integrate.api.nvidia.com/v1",
    "nvidia_model": "meta/llama-3.3-70b-instruct",
    "groq_enabled": True,
    "groq_base_url": "https://api.groq.com/openai/v1",
    "groq_model": "llama-3.3-70b-versatile",
    "grok_enabled": True,
    "grok_base_url": "https://api.x.ai/v1",
    "grok_model": "grok-4.5",
    "emergent_enabled": True,
    "emergent_model": "claude-sonnet-4-6",
    "yabbai_enabled": True,
    "yabbai_url": "",
    "yabbai_model": "llama3.2",
    "supabase_url": "https://gecwxvwziktvaiwdhzeg.supabase.co",
}


async def get_raw_settings() -> dict:
    """Full document including secrets — internal use only (routing)."""
    doc = await _db.settings.find_one({"_id": SETTINGS_ID}) or {}
    merged = {**DEFAULTS, **{k: v for k, v in doc.items() if k != "_id"}}
    # Env-configured tiers take precedence (carries to prod deploys).
    env_map = {
        "yabbai_url": os.environ.get("YABBAI_TIER_URL"),
        "yabbai_api_key": os.environ.get("YABBAI_TIER_KEY"),
        "yabbai_model": os.environ.get("YABBAI_TIER_MODEL"),
        "groq_api_key": os.environ.get("GROQ_API_KEY"),
        "groq_model": os.environ.get("GROQ_MODEL"),
        "groq_base_url": os.environ.get("GROQ_BASE_URL"),
        "grok_api_key": os.environ.get("XAI_API_KEY"),
        "grok_model": os.environ.get("XAI_MODEL"),
        "grok_base_url": os.environ.get("XAI_BASE_URL"),
    }
    for k, v in env_map.items():
        if v:
            merged[k] = v
    # Self-heal: any saved route_order missing a tier (or holding stale ids)
    # resets to the canonical five-tier order.
    valid = DEFAULTS["route_order"]
    order = [t for t in (merged.get("route_order") or []) if t in valid]    
    if set(order) != set(valid):
        order = list(valid)
    merged["route_order"] = order
    return merged


async def save_settings(payload: dict) -> None:
    payload.pop("_id", None)
    payload.pop("secrets_set", None)
    # never let blank secret strings wipe an existing secret
    clean = {}
    for k, v in payload.items():
        if k in SECRET_FIELDS and (v is None or v == "" or v == "********"):
            continue
        if k == "route_order" and (not isinstance(v, list) or len(v) == 0):
            continue  # never let a client wipe the routing order
        clean[k] = v
    if clean:
        await _db.settings.update_one({"_id": SETTINGS_ID}, {"$set": clean}, upsert=True)


def sanitize(doc: dict) -> dict:
    """UI-safe view: secret values replaced by booleans."""
    out = {k: v for k, v in doc.items() if k not in SECRET_FIELDS and k != "_id"}
    out["secrets_set"] = {k: bool(doc.get(k)) for k in SECRET_FIELDS}
    return out
