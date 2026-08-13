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
    "cerebras_api_key", "google_api_key", "openrouter_api_key",
    "supabase_service_key", "stripe_secret_key", "paypal_secret",
    "google_client_secret", "tavily_api_key", "jupiter_api_key",
}

DEFAULTS = {
    # Free-first ladder; paid emergent tier last, always. grok kept but dormant
    # (enable via XAI_ENABLED); yabbai (laptop) optional + disabled by default.
    "route_order": ["nvidia", "groq", "cerebras", "google", "openrouter",
                    "grok", "yabbai", "emergent"],
    "nvidia_enabled": True,
    "nvidia_base_url": "https://integrate.api.nvidia.com/v1",
    "nvidia_model": "meta/llama-3.3-70b-instruct",
    "groq_enabled": True,
    "groq_base_url": "https://api.groq.com/openai/v1",
    "groq_model": "llama-3.3-70b-versatile",
    "cerebras_enabled": True,
    "cerebras_base_url": "https://api.cerebras.ai/v1",
    "cerebras_model": "gpt-oss-120b",
    "google_enabled": True,
    "google_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    "google_model": "gemini-2.5-flash",
    "openrouter_enabled": True,
    "openrouter_base_url": "https://openrouter.ai/api/v1",
    "openrouter_model": "meta-llama/llama-3.3-70b-instruct:free",
    "openrouter_paid_model": "",
    "grok_enabled": False,
    "grok_base_url": "https://api.x.ai/v1",
    "grok_model": "grok-4.5",
    "emergent_enabled": True,
    "emergent_model": "claude-sonnet-4-6",
    "yabbai_enabled": False,
    "yabbai_url": "",
    "yabbai_model": "llama3.2",
    "supabase_url": "https://gecwxvwziktvaiwdhzeg.supabase.co",
    # GoldScout news scout — Tavily. Scanner is manual-only unless a positive
    # interval is set (env GOLDSCOUT_INTERVAL_SECS). Free/dev tier is 100
    # credits/month — enforce a hard ceiling in-app, cache aggressively.
    "goldscout_interval_secs": 0,
    "goldscout_cache_ttl_hours": 24,
    "goldscout_default_depth": "basic",   # basic=1cr, advanced=2cr per Tavily call
    "tavily_monthly_limit": 100,
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
        "cerebras_api_key": os.environ.get("CEREBRAS_API_KEY"),
        "cerebras_model": os.environ.get("CEREBRAS_MODEL"),
        "cerebras_base_url": os.environ.get("CEREBRAS_BASE_URL"),
        "google_api_key": os.environ.get("GOOGLE_AI_KEY"),
        "google_model": os.environ.get("GOOGLE_AI_MODEL"),
        "google_base_url": os.environ.get("GOOGLE_AI_BASE_URL"),
        "openrouter_api_key": os.environ.get("OPENROUTER_API_KEY"),
        "openrouter_model": os.environ.get("OPENROUTER_MODEL"),
        "openrouter_base_url": os.environ.get("OPENROUTER_BASE_URL"),
        "openrouter_paid_model": os.environ.get("OPENROUTER_PAID_MODEL"),
        "tavily_api_key": os.environ.get("TAVILY_API_KEY"),
        "jupiter_api_key": os.environ.get("JUPITER_API_KEY"),
    }
    # Integer envs (interval etc.) — coerce safely; blank/invalid keeps merged value.
    int_env_map = {
        "goldscout_interval_secs": "GOLDSCOUT_INTERVAL_SECS",
        "goldscout_cache_ttl_hours": "GOLDSCOUT_CACHE_TTL_HOURS",
        "tavily_monthly_limit": "TAVILY_MONTHLY_LIMIT",
    }
    for k, env_name in int_env_map.items():
        v = os.environ.get(env_name)
        if v is not None and v.strip():
            try:
                merged[k] = max(0, int(v))
            except ValueError:
                pass
    for k, v in env_map.items():
        if v:
            merged[k] = v
    # String env override for depth (basic|advanced).
    depth_env = os.environ.get("GOLDSCOUT_DEFAULT_DEPTH")
    if depth_env and depth_env.strip().lower() in ("basic", "advanced"):
        merged["goldscout_default_depth"] = depth_env.strip().lower()
    # Boolean env overrides win over saved settings (set "true"/"false").
    bool_env_map = {
        "nvidia_enabled": "NVIDIA_ENABLED",
        "groq_enabled": "GROQ_ENABLED",
        "cerebras_enabled": "CEREBRAS_ENABLED",
        "google_enabled": "GOOGLE_AI_ENABLED",
        "openrouter_enabled": "OPENROUTER_ENABLED",
        "grok_enabled": "XAI_ENABLED",
        "yabbai_enabled": "YABBAI_TIER_ENABLED",
        "emergent_enabled": "EMERGENT_ENABLED",
    }
    for k, env_name in bool_env_map.items():
        v = os.environ.get(env_name)
        if v is not None:
            merged[k] = v.strip().lower() in ("1", "true", "yes", "on")
    # Self-heal: any saved route_order missing a tier (or holding stale ids)
    # resets to the canonical free-first order.
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
