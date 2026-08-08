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
    "nvidia_api_key", "yabbai_api_key",
    "stripe_secret_key", "paypal_secret", "google_client_secret", "tavily_api_key",
}

DEFAULTS = {
    "route_order": ["emergent", "nvidia", "yabbai"],
    "nvidia_enabled": True,
    "nvidia_base_url": "https://integrate.api.nvidia.com/v1",
    "nvidia_model": "meta/llama-3.3-70b-instruct",
    "emergent_enabled": True,
    "emergent_model": "claude-sonnet-4-6",
    "yabbai_enabled": True,
    "yabbai_url": "",
    "yabbai_model": "llama3.2",
}


async def get_raw_settings() -> dict:
    """Full document including secrets — internal use only (routing)."""
    doc = await _db.settings.find_one({"_id": SETTINGS_ID}) or {}
    merged = {**DEFAULTS, **{k: v for k, v in doc.items() if k != "_id"}}
    return merged


async def save_settings(payload: dict) -> None:
    payload.pop("_id", None)
    payload.pop("secrets_set", None)
    # never let blank secret strings wipe an existing secret
    clean = {}
    for k, v in payload.items():
        if k in SECRET_FIELDS and (v is None or v == "" or v == "********"):
            continue
        clean[k] = v
    if clean:
        await _db.settings.update_one({"_id": SETTINGS_ID}, {"$set": clean}, upsert=True)


def sanitize(doc: dict) -> dict:
    """UI-safe view: secret values replaced by booleans."""
    out = {k: v for k, v in doc.items() if k not in SECRET_FIELDS and k != "_id"}
    out["secrets_set"] = {k: bool(doc.get(k)) for k in SECRET_FIELDS}
    return out
