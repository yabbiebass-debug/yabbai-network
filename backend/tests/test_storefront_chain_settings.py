"""Offline unit tests — Storefront & Chain settings (3-class split).
CLASS A plain config (public_base_url/store_assets_dir), CLASS B masked credential
(solana_rpc_url — env wins, never echoed), CLASS C money-capable (Stripe — env-only,
no write path via /api/settings). No network, no Mongo: everything monkeypatched.
"""
import asyncio, os, sys

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import network_db  # noqa: E402
from defi import config  # noqa: E402


def test_env_beats_settings_for_solana_rpc_url(monkeypatch):
    monkeypatch.setenv("SOLANA_RPC_URL", "https://env-rpc.example/?api-key=ENVKEY")
    async def _settings(): return {"solana_rpc_url": "https://settings-rpc.example"}
    monkeypatch.setattr(config, "get_raw_settings", _settings)
    assert asyncio.run(config.rpc_url()) == "https://env-rpc.example/?api-key=ENVKEY", \
        "env SOLANA_RPC_URL must always win over the Mongo settings value"


def test_settings_value_used_when_env_unset(monkeypatch):
    monkeypatch.delenv("SOLANA_RPC_URL", raising=False)
    async def _settings(): return {"solana_rpc_url": "https://settings-rpc.example"}
    monkeypatch.setattr(config, "get_raw_settings", _settings)
    assert asyncio.run(config.rpc_url()) == "https://settings-rpc.example", \
        "with env unset, the /settings value must be used"


def test_default_used_when_both_unset(monkeypatch):
    monkeypatch.delenv("SOLANA_RPC_URL", raising=False)
    async def _settings(): return {"solana_rpc_url": ""}
    monkeypatch.setattr(config, "get_raw_settings", _settings)
    assert asyncio.run(config.rpc_url()) == "https://api.mainnet-beta.solana.com", \
        "with env and settings both empty, fall back to public mainnet-beta"


def test_get_settings_never_returns_rpc_url_in_plaintext():
    doc = {**network_db.DEFAULTS,
           "solana_rpc_url": "https://mainnet.helius-rpc.com/?api-key=SUPERSECRET"}
    out = network_db.sanitize(doc)
    assert "solana_rpc_url" not in out, \
        "RPC URLs can embed an API key — GET /api/settings must return booleans only"
    assert "SUPERSECRET" not in str(out), "no secret substring may leak via sanitize"
    assert out["secrets_set"]["solana_rpc_url"] is True, \
        "secrets_set boolean must still report configured state"


class _FakeSettingsColl:
    def __init__(self): self.writes = []
    async def update_one(self, q, u, upsert=False): self.writes.append(u["$set"])


def test_stripe_secrets_cannot_be_written_via_put_settings(monkeypatch):
    class _DB: pass
    fake = _DB(); fake.settings = _FakeSettingsColl()
    monkeypatch.setattr(network_db, "_db", fake)
    asyncio.run(network_db.save_settings({
        "stripe_secret_key": "sk_live_forged", "STRIPE_SECRET_KEY": "sk_live_forged",
        "stripe_webhook_secret": "whsec_forged", "STRIPE_WEBHOOK_SECRET": "whsec_forged",
        "nvidia_model": "meta/llama-3.3-70b-instruct"}))
    assert fake.settings.writes == [{"nvidia_model": "meta/llama-3.3-70b-instruct"}], \
        f"money-capable Stripe creds must be stripped before any Mongo write: {fake.settings.writes}"
