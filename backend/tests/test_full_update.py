"""
Checkpoint tests for the full-codebase-update run (WS1, WS4, WS5, WS6, WS7).

Run: cd /app/backend && python -m pytest tests/test_full_update.py -v
Uses the local Mongo (preview) + live public APIs for the network-verifier tests.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, "/app/backend")

SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WALLET_OK = "11111111111111111111111111111111"   # valid pubkey shape (system program)


def _sync_db():
    import pymongo
    return pymongo.MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]


_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


async def _fake_user(request, authorization):
    return {"user_id": "test-user"}


# ═══ CHECKPOINT 1 — settlement boundary ════════════════════════════════════════
def test_fabricated_solana_signature_raises():
    from revenue_system.defi_backend_patched import settlement
    fake_sig = "5" * 87  # syntactically plausible, does not exist on-chain
    with pytest.raises(settlement.SettlementVerificationError):
        run(settlement.record_settled_income(10.0, "USD", "solana", fake_sig))


def test_settlement_input_validation():
    from revenue_system.defi_backend_patched import settlement
    E = settlement.SettlementVerificationError
    with pytest.raises(E):
        run(settlement.record_settled_income(-5, "USD", "solana", "x"))
    with pytest.raises(E):
        run(settlement.record_settled_income(5, "USD", "bank-transfer", "x"))
    with pytest.raises(E):
        run(settlement.record_settled_income(5, "USD", "stripe", ""))
    # stripe without STRIPE_SECRET_KEY set → cannot verify → refuses (fail closed)
    os.environ.pop("STRIPE_SECRET_KEY", None)
    with pytest.raises(E):
        run(settlement.record_settled_income(5, "USD", "stripe", "pi_fake"))


def test_single_income_writer_grep():
    out = subprocess.run(
        ["grep", "-rn", "--include=*.py", "--exclude-dir=__pycache__", "--exclude-dir=tests",
         'entry_type": "income', "/app/backend"],
        capture_output=True, text=True).stdout.strip().splitlines()
    files = {line.split(":")[0] for line in out}
    assert files == {"/app/backend/revenue_system/defi_backend_patched/settlement.py"}, \
        f"income writers must be exactly settlement.py, got: {files}"


def test_no_boot_loops_and_income_refused_at_vault():
    from fastapi.testclient import TestClient
    from revenue_system.defi_backend_patched import server as gh
    assert gh.swarm_running is False
    assert gh.treasury_running is False
    admin = {"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    with TestClient(gh.app) as client:
        r = client.post("/vault", json={"source": "t", "amount": 5, "entry_type": "income",
                                        "reconciled": True}, headers=admin)
        assert r.status_code == 400 and "settle" in r.json()["detail"]


def test_distribute_now_refuses_with_circuit_breaker():
    from fastapi.testclient import TestClient
    from revenue_system.defi_backend_patched import server as gh
    admin = {"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    gh.gates.engage_kill_switch()
    try:
        with TestClient(gh.app) as client:
            r = client.post("/treasury/distribute-now", json={"amount": 5}, headers=admin)
            assert r.status_code == 403 and "circuit breaker" in r.json()["detail"]
    finally:
        gh.gates.disengage_kill_switch()


def test_swarm_start_gated_by_flag():
    from fastapi.testclient import TestClient
    from revenue_system.defi_backend_patched import server as gh
    admin = {"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    os.environ["SWARM_AUTOSTART"] = "false"
    with TestClient(gh.app) as client:
        r = client.post("/swarm/start", headers=admin)
        assert r.status_code == 403
        r2 = client.post("/treasury/start", headers=admin)
        assert r2.status_code == 403


def test_no_executed_status_and_no_law_audit_data():
    out = subprocess.run(
        ["grep", "-rn", "--include=*.py", "--exclude-dir=__pycache__",
         "-e", 'status.*"EXECUTED"', "-e", "LAW_AUDIT_DATA",
         "/app/backend/revenue_system/defi_backend_patched", "/app/backend/defi"],
        capture_output=True, text=True).stdout.strip()
    assert out == "", f"legacy status/LAW_AUDIT_DATA found: {out}"


# ═══ CHECKPOINT 4 — janitor rules ═══════════════════════════════════════════════
def _tok_acc(pubkey, mint, amount, ui, program, state="initialized",
             delegate=None, lamports=2039280):
    return {"pubkey": pubkey, "account": {"lamports": lamports, "data": {"parsed": {"info": {
        "mint": mint, "state": state, "delegate": delegate,
        "tokenAmount": {"amount": str(amount), "uiAmount": ui, "decimals": 6}}}}}}


def test_janitor_classification_protected_by_default(monkeypatch):
    from defi import janitor
    TOKEN, T22 = janitor.TOKEN_PROGRAM, janitor.TOKEN_2022_PROGRAM
    accounts = {
        TOKEN: [_tok_acc("A_valued", "MintValued", 5_000_000, 5.0, TOKEN),
                _tok_acc("B_empty", "MintEmpty", 0, 0, TOKEN),
                _tok_acc("C_dust", "MintDust", 100, 0.0001, TOKEN),
                _tok_acc("D_noprice", "MintNoPrice", 900, 0.9, TOKEN),
                _tok_acc("E_frozen", "MintFrozen", 0, 0, TOKEN, state="frozen"),
                _tok_acc("F_delegated", "MintDeleg", 0, 0, TOKEN, delegate="SomeDelegate")],
        T22: [_tok_acc("G_empty22", "Mint22", 0, 0, T22)],
    }

    async def fake_rpc(method, params):
        assert method == "getTokenAccountsByOwner"
        return {"value": accounts[params[1]["programId"]]}

    async def fake_prices(mints):
        return {"MintValued": {"usdPrice": 10.0}, "MintDust": {"usdPrice": 1.0},
                "MintEmpty": {"usdPrice": 1.0}, "Mint22": {"usdPrice": 1.0}}
        # MintNoPrice deliberately absent → PROTECTED (fail closed)

    monkeypatch.setattr(janitor, "rpc_call", fake_rpc)
    monkeypatch.setattr(janitor.jupiter, "prices", fake_prices)
    c = run(janitor.classify_wallet("WALLET"))
    b = c["buckets"]
    assert [a["account"] for a in b["empty_ata"]] == ["B_empty"]
    assert [a["account"] for a in b["empty_ata_2022"]] == ["G_empty22"]
    assert [a["account"] for a in b["dust"]] == ["C_dust"]
    protected = {a["account"] for a in b["PROTECTED"]}
    assert protected == {"A_valued", "D_noprice", "E_frozen", "F_delegated"}


def test_janitor_build_rejects_funded_and_mixed(monkeypatch):
    from fastapi import HTTPException
    from defi import janitor
    monkeypatch.setattr(janitor, "require_user", _fake_user)

    async def funded_account(pk):   # funded between scan and build
        return {"owner": janitor.TOKEN_PROGRAM, "lamports": 2039280,
                "data": {"parsed": {"info": {"owner": WALLET_OK, "mint": "M",
                         "tokenAmount": {"amount": "12345", "uiAmount": 0.012}}}}}
    monkeypatch.setattr(janitor, "_fresh_account", funded_account)
    body = janitor.BuildBody(wallet=WALLET_OK, action="close", accounts=["ACC1"])
    with pytest.raises(HTTPException) as e:
        run(janitor.build(body, None, None))
    assert e.value.status_code == 409 and "funded" in e.value.detail

    # burn on a zero-balance account = a close smuggled into a burn → rejected
    async def empty_account(pk):
        return {"owner": janitor.TOKEN_PROGRAM, "lamports": 2039280,
                "data": {"parsed": {"info": {"owner": WALLET_OK, "mint": "M",
                         "tokenAmount": {"amount": "0", "uiAmount": 0}}}}}
    monkeypatch.setattr(janitor, "_fresh_account", empty_account)
    with pytest.raises(HTTPException) as e2:
        run(janitor.build(janitor.BuildBody(wallet=WALLET_OK, action="burn", accounts=["ACC1"]), None, None))
    assert e2.value.status_code == 409

    with pytest.raises(HTTPException) as e3:
        run(janitor.build(janitor.BuildBody(wallet=WALLET_OK, action="close-and-burn", accounts=["A"]), None, None))
    assert e3.value.status_code == 400


def test_janitor_income_routes_through_settlement_only():
    src = open("/app/backend/defi/janitor.py").read()
    assert "record_settled_income" in src
    assert "vault_entries" not in src   # no direct ledger writes


# ═══ CHECKPOINT 5 — quote/execute guardrails ═══════════════════════════════════
def test_execute_403_when_live_disabled():
    from fastapi.testclient import TestClient
    import server as gateway
    os.environ["DEFI_LIVE_ENABLED"] = "false"
    with TestClient(gateway.app) as client:
        r = client.post("/api/defi/execute",
                        json={"signedTransaction": "x", "requestId": "y"})
        assert r.status_code == 403 and "DEFI_LIVE_ENABLED" in r.json()["detail"]


def test_quote_guardrails(monkeypatch):
    from fastapi.testclient import TestClient
    import server as gateway
    from defi import service
    monkeypatch.setattr(service, "require_user", _fake_user)

    with TestClient(gateway.app) as client:
        # empty allowlist = NOTHING tradeable (empty ≠ allow-all)
        os.environ["DEFI_ALLOWED_MINTS"] = ""
        r = client.get("/api/defi/quote",
                       params={"inputMint": SOL, "outputMint": USDC, "amount": 1000})
        assert r.status_code == 403 and "empty" in r.json()["detail"]
        # unlisted mint rejected
        os.environ["DEFI_ALLOWED_MINTS"] = SOL
        r2 = client.get("/api/defi/quote",
                        params={"inputMint": SOL, "outputMint": USDC, "amount": 1000})
        assert r2.status_code == 403 and "allowlist" in r2.json()["detail"]
        # over-cap trade rejected (1 SOL ≫ $25 cap, priced live)
        os.environ["DEFI_ALLOWED_MINTS"] = f"{SOL},{USDC}"
        r3 = client.get("/api/defi/quote",
                        params={"inputMint": SOL, "outputMint": USDC, "amount": 10**9})
        assert r3.status_code == 403 and "DEFI_MAX_TRADE_USD" in r3.json()["detail"]
    os.environ["DEFI_ALLOWED_MINTS"] = ""


def test_execute_rejects_stale_and_unknown_request(monkeypatch):
    from fastapi.testclient import TestClient
    import server as gateway
    from defi import service
    monkeypatch.setattr(service, "require_user", _fake_user)
    os.environ["DEFI_LIVE_ENABLED"] = "true"
    stale_id = f"test-stale-{uuid.uuid4()}"
    sdb = _sync_db()
    sdb.defi_quotes.insert_one({
        "request_id": stale_id, "created_at": datetime.now(timezone.utc) - timedelta(minutes=5),
        "executed": False, "blocked": False, "usd_in": 1.0})
    try:
        with TestClient(gateway.app) as client:
            r = client.post("/api/defi/execute",
                            json={"signedTransaction": "x", "requestId": stale_id})
            assert r.status_code == 410
            r2 = client.post("/api/defi/execute",
                             json={"signedTransaction": "x", "requestId": "never-issued"})
            assert r2.status_code == 400
            # the stale doc was atomically claimed → replay now 409 (no double relay)
            r3 = client.post("/api/defi/execute",
                             json={"signedTransaction": "x", "requestId": stale_id})
            assert r3.status_code == 409
    finally:
        os.environ["DEFI_LIVE_ENABLED"] = "false"
        sdb.defi_quotes.delete_many({"request_id": stale_id})


def test_n1_no_signing_terms_in_backend():
    out = subprocess.run(
        ["grep", "-rn", "--include=*.py", "--exclude-dir=__pycache__", "--exclude-dir=tests",
         "-e", "PRIVATE_KEY", "-e", "Keypair.fromSecretKey", "-e", "signTransaction",
         "-e", "sendRawTransaction", "-e", "from_secret_key", "/app/backend"],
        capture_output=True, text=True).stdout.strip()
    assert out == "", f"N1 violation: {out}"


# ═══ CHECKPOINT 6 — sentinel/harvester ═══════════════════════════════════════════
def test_sentinel_constructs_no_transactions():
    src = open("/app/backend/defi/sentinel.py").read()
    for term in ("Instruction(", "Transaction(", "Message.", "Keypair", "solders"):
        assert term not in src, f"sentinel must construct no transactions: found {term}"


def test_harvester_has_no_execution_path_and_no_blended_fields():
    import ast
    src = open("/app/backend/defi/harvester.py").read()
    for term in ("swap_execute", "/execute", "submit", "signedTransaction"):
        assert term not in src, f"harvester must have no execution path: found {term}"
    # code only (docstrings state the rule in negation — strip them first)
    tree = ast.parse(src)
    code_only = ast.unparse(ast.Module(
        body=[n for n in tree.body if not isinstance(n, ast.Expr)], type_ignores=[]))
    for term in ("blended", "projected", "expected_return", "score_total"):
        assert term not in code_only, f"forbidden candidate field concept in code: {term}"


def test_harvest_scan_403_when_disabled(monkeypatch):
    from fastapi.testclient import TestClient
    import server as gateway
    from defi import harvester
    monkeypatch.setattr(harvester, "require_user", _fake_user)
    os.environ.pop("HARVEST_ENABLED", None)
    with TestClient(gateway.app) as client:
        r = client.post("/api/defi/harvest/scan", json={"queries": ["SOL"], "size_usd": 5})
        assert r.status_code == 403


# ═══ CHECKPOINT 7 — earn gates ═══════════════════════════════════════════════════
def test_earn_deposit_dark_and_allowlist(monkeypatch):
    from fastapi.testclient import TestClient
    import server as gateway
    from defi import earn
    monkeypatch.setattr(earn, "require_user", _fake_user)
    payload = {"wallet": "W", "protocol": "jupiter-lend", "asset_mint": USDC,
               "shares": "1000000", "action": "deposit"}
    with TestClient(gateway.app) as client:
        os.environ["EARN_ENABLED"] = "false"
        r = client.post("/api/defi/earn/build", json=payload)
        assert r.status_code == 403 and "EARN_ENABLED" in r.json()["detail"]
        # flag on but EMPTY allowlist still blocks every deposit
        os.environ["EARN_ENABLED"] = "true"
        os.environ["EARN_ALLOWED_PROTOCOLS"] = ""
        r2 = client.post("/api/defi/earn/build", json=payload)
        assert r2.status_code == 403 and "EARN_ALLOWED_PROTOCOLS" in r2.json()["detail"]
        # over-per-deposit cap rejected even when allowed
        os.environ["EARN_ALLOWED_PROTOCOLS"] = "jupiter-lend"
        big = dict(payload, shares=str(10**12))   # ~1M USDC ≫ $250 cap
        r3 = client.post("/api/defi/earn/build", json=big)
        assert r3.status_code == 403 and "EARN_MAX_DEPOSIT_USD" in r3.json()["detail"]
        # withdrawal is NEVER gated by these flags (exits always work)
        os.environ["EARN_ENABLED"] = "false"
        os.environ["EARN_ALLOWED_PROTOCOLS"] = ""
        w = dict(payload, action="withdraw")
        r4 = client.post("/api/defi/earn/build", json=w)
        assert r4.status_code != 403, f"withdraw must never be flag-gated, got {r4.status_code}"
    os.environ["EARN_ENABLED"] = "false"


def test_earn_markets_sanity_filter_live():
    from fastapi.testclient import TestClient
    import server as gateway
    with TestClient(gateway.app) as client:
        r = client.get("/api/defi/earn/markets")
        assert r.status_code == 200
        d = r.json()
        sanity = 20.0
        for m in d["markets"]:
            assert m["apy_pct"] is None or m["apy_pct"] <= sanity
            assert m["apy_label"] == "variable — not guaranteed"
            assert m["risk_flags"]


def test_earn_frontend_has_no_projection_language():
    src = open("/app/frontend/public/defi/index.html").read().lower()
    for phrase in ("projected", "will grow", "in one year", "estimated earnings"):
        assert phrase not in src, f"projection language found: {phrase}"
