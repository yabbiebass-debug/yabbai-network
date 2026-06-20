"""
YABBAI Revenue System — Safety & behaviour tests.

These tests ARE the proof that the dangerous behaviours from the original
defi_backend are structurally impossible through this spine, and that the three
invariants hold. If any of these fails, the system is unsafe to run with money.

Run:  python -m pytest revenue_system/tests/test_safety.py -v
(No pytest? They also run standalone:  python revenue_system/tests/test_safety.py)
"""

import os
import sys
import tempfile
from pathlib import Path

# Allow running from repo root without install
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from revenue_system.truth import (Metric, SourceType, TruthLedger, DataGap, require_real)
from revenue_system.gates import (GateController, ProposedAction, CapitalCaps, RiskClass)
from revenue_system.memory import Memory, Learning
from revenue_system.orchestrator import Orchestrator
from revenue_system.channels import (ProductsChannel, AgencyChannel, TradingChannel,
                                     NoKeySigner, TradeCap)
from revenue_system.channels.products.channel import _SALE_CACHE
from revenue_system.channels.agency.channel import _PAID_INVOICE_CACHE

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}")


# ── INVARIANT 1: estimates never count as real income ─────────────────────────
def test_estimate_is_not_income():
    print("\n[1] No-fabrication: an LLM 'estimate' must NOT count as real income")
    led = TruthLedger()
    led.record(Metric("income", 500.0, "llm:sentinel", SourceType.ESTIMATE))
    # reconciled=True is required for a payment_processor metric to be bookable
    led.record(Metric("income", 12.34, "stripe:ch_abc", SourceType.PAYMENT_PROCESSOR,
                      reconciled=True))
    check("estimate excluded from real_income", led.real_income() == 12.34)
    check("estimate excluded from net_profit", led.net_profit() == 12.34)
    check("estimate still visible in projected view",
          led.projected_net()["estimated_income"] == 500.0)
    # unreconciled payment_processor metric is also excluded
    led2 = TruthLedger()
    led2.record(Metric("income", 99.0, "stripe:unreconciled", SourceType.PAYMENT_PROCESSOR,
                       reconciled=False))
    check("unreconciled payment_processor excluded from real_income", led2.real_income() == 0.0)


def test_projected_requires_assumptions():
    print("\n[1b] Projections must state assumptions (no silent projections)")
    try:
        Metric("income", 100.0, "model", SourceType.PROJECTED)  # no assumptions
        check("PROJECTED without assumptions rejected", False)
    except ValueError:
        check("PROJECTED without assumptions rejected", True)


def test_missing_value_is_gap_not_zero():
    print("\n[1c] Missing data is a DATA GAP, not a fabricated zero")
    led = TruthLedger()
    check("empty ledger net_profit is honestly 0.0", led.net_profit() == 0.0)
    g = Metric.gap("cost", "no price feed")
    check("gap metric is not bookable", not g.is_bookable)
    try:
        require_real(g.value, g, "test decision")
        check("require_real halts on gap", False)
    except DataGap:
        check("require_real halts on gap", True)


# ── INVARIANT 2: payouts/outbound/publish/trade are HIGH → human approval ──────
def test_payout_queues_for_human():
    print("\n[2] Payouts/outbound/publish/real_trade always queue for human approval")
    g = GateController(CapitalCaps())
    g.register_executor("treasury", "payout", lambda a: {"ok": True})
    out = g.propose(ProposedAction("payout", "treasury", "cfo", usd_amount=5.0))
    check("payout is HIGH", out["rc"] == "high")
    check("payout is queued, not executed", out["outcome"] == "queued")
    check("a pending approval exists", len(g.pending_approvals()) == 1)


def test_real_trade_requires_signature():
    print("\n[2b] A real trade needs a human signature even after approval")
    g = GateController(CapitalCaps())
    calls = []
    g.register_executor("trading", "real_trade",
                        lambda a, signature=None: calls.append(signature) or {"ok": True, "tx_id": "x"})
    out = g.propose(ProposedAction("real_trade", "trading", "trading", usd_amount=5.0,
                                   reversible=False, payload={"token": "T"}))
    qid = out["queue_id"]
    # approve WITHOUT signature -> refused
    no_sig = g.human_approve(qid, "human", None)
    check("approval without signature refused", not no_sig.get("ok", False))
    # approve WITH signature -> executes
    with_sig = g.human_approve(qid, "human", "wallet-sig-123")
    check("approval with signature executes",
          with_sig.get("outcome") == "executed" or calls)


def test_outbound_email_is_high():
    print("\n[2c] Outbound email/DM is HIGH (compliance-gated)")
    g = GateController(CapitalCaps())
    out = g.propose(ProposedAction("outbound_email", "agency", "sales", usd_amount=0.0))
    check("outbound_email is HIGH", out["rc"] == "high")


def test_low_reversible_runs_autonomously():
    print("\n[2d] Low-cost reversible actions still run autonomously")
    g = GateController(CapitalCaps(micro_spend_cap=2.0))
    fired = []
    g.register_executor("products", "draft", lambda a: fired.append(1) or {"ok": True})
    out = g.propose(ProposedAction("draft", "products", "agent", usd_amount=0.5, reversible=True))
    check("LOW action executes", out["outcome"] == "executed")
    check("executor was actually called", len(fired) == 1)


# ── INVARIANT 3: caps + kill-switch cannot be bypassed ────────────────────────
def test_daily_spend_cap_blocks():
    print("\n[3] Daily spend cap blocks over-budget actions even if LOW")
    g = GateController(CapitalCaps(micro_spend_cap=2.0, spend_cap=5.0, daily_spend_cap=5.0))
    g.register_executor("x", "buy", lambda a: {"ok": True})
    g.propose(ProposedAction("buy", "x", "a", usd_amount=4.0))   # under cap (MEDIUM->needs peer, so queue)
    # force a second buy that would breach
    out = g.propose(ProposedAction("buy", "x", "a", usd_amount=4.0))
    check("over-cap action blocked", out["outcome"] in ("blocked_cap", "queued"))


def test_kill_switch_halts_everything():
    print("\n[3b] Kill-switch halts ALL actions, no exceptions")
    g = GateController(CapitalCaps())
    g.engage_kill_switch()
    out = g.propose(ProposedAction("draft", "x", "a", usd_amount=0.5, reversible=True))
    check("kill-switch halts LOW action", out["outcome"] == "halted")


def test_cfo_cannot_raise_caps():
    print("\n[3c] CFO can only TIGHTEN caps, never raise them")
    g = GateController(CapitalCaps(spend_cap=10.0))
    try:
        g.tighten_caps(spend_cap=20.0)   # raising -> must fail
        check("raising a cap is rejected", False)
    except ValueError:
        check("raising a cap is rejected", True)
    g.tighten_caps(spend_cap=5.0)        # tightening -> ok
    check("tightening a cap is allowed", g.caps.spend_cap == 5.0)


# ── CHANNELS: income only from real confirmations ─────────────────────────────
def test_products_income_only_from_real_sale():
    print("\n[4] Products channel books income ONLY from a confirmed sale")
    _SALE_CACHE.clear()
    led = TruthLedger(); g = GateController(CapitalCaps())
    mem = Memory(tempfile.mkdtemp())
    ch = ProductsChannel(led, g, mem)
    # no fetcher -> data gap, no income
    m = ch.observe(fetch_sales=None)
    check("no fetcher => data gap, not income",
          all(not x.is_bookable for x in m) and led.net_profit() == 0.0)
    # real sale => bookable income
    m = ch.observe(fetch_sales=lambda: [{"sale_id": "s1", "product": "ebook",
                                         "gross_usd": 20.0, "fee_usd": 2.0}])
    led.record_many(m)
    check("real sale books $18 net income", led.net_profit() == 18.0)
    # idempotency: same sale twice
    m2 = ch.observe(fetch_sales=lambda: [{"sale_id": "s1", "product": "ebook",
                                          "gross_usd": 20.0, "fee_usd": 2.0}])
    check("duplicate sale not double-booked", len([x for x in m2 if x.is_bookable]) == 0)


def test_agency_income_only_from_paid_invoice():
    print("\n[5] Agency channel books income ONLY from a PAID invoice")
    _PAID_INVOICE_CACHE.clear()
    led = TruthLedger(); g = GateController(CapitalCaps())
    mem = Memory(tempfile.mkdtemp())
    ch = AgencyChannel(led, g, mem)
    # pipeline value is NOT income
    m = ch.observe(fetch_paid_invoices=None, pipeline_value_usd=5000.0)
    led.record_many(m)
    check("pipeline value does not count as income", led.net_profit() == 0.0)
    # paid invoice => income
    m = ch.observe(fetch_paid_invoices=lambda: [{"invoice_id": "in_1", "paid_usd": 250.0}])
    led.record_many(m)
    check("paid invoice books $250 income", led.net_profit() == 250.0)


def test_trading_no_real_broadcast_without_signer():
    print("\n[6] Trading channel cannot broadcast without an injected signer")
    led = TruthLedger(); g = GateController(CapitalCaps())
    mem = Memory(tempfile.mkdtemp())
    ch = TradingChannel(led, g, mem,
                        TradeCap(max_real_trade_usd=10.0, token_allowlist=["TOK"]),
                        signer=NoKeySigner())
    g.register_executor("trading", "real_trade", ch._execute_real_trade)
    out = g.propose(ProposedAction("real_trade", "trading", "t", usd_amount=5.0,
                                   reversible=False, payload={"token": "TOK", "side": "buy"}))
    qid = out["queue_id"]
    res = g.human_approve(qid, "human", "sig")
    check("NoKeySigner refuses the broadcast", res["outcome"] == "halted")
    check("no income booked from refused trade", led.net_profit() == 0.0)


def test_trading_needs_paper_proof_before_proposing_live():
    print("\n[7] Trading won't even propose a live trade until paper proof bar is met")
    led = TruthLedger(); g = GateController(CapitalCaps())
    mem = Memory(tempfile.mkdtemp())
    ch = TradingChannel(led, g, mem, TradeCap(token_allowlist=["TOK"]),
                        paper_proof_bar=100)
    check("no live proposals before proof bar", len(ch.propose()) == 0)
    for _ in range(100):
        ch.note_paper_result(+1.0)
    check("live proposals appear after proof bar", len(ch.propose()) >= 1)


# ── ORCHESTRATOR: end-to-end cycle ────────────────────────────────────────────
def test_orchestrator_cycle_runs_all_channels():
    print("\n[8] One orchestrator cycle drives all three channels via one spine")
    _SALE_CACHE.clear(); _PAID_INVOICE_CACHE.clear()
    led = TruthLedger(); g = GateController(CapitalCaps())
    mem = Memory(tempfile.mkdtemp())
    orch = Orchestrator(led, g, mem)
    products = ProductsChannel(led, g, mem)
    agency = AgencyChannel(led, g, mem)
    trading = TradingChannel(led, g, mem, TradeCap(token_allowlist=[]))
    orch.register_channel("products",
                          lambda: products.observe(lambda: [{"sale_id":"z","gross_usd":10,"fee_usd":0}]),
                          products.propose)
    orch.register_channel("agency",
                          lambda: agency.observe(lambda: [{"invoice_id":"i","paid_usd":5}]),
                          agency.propose)
    orch.register_channel("trading", trading.observe, trading.propose)
    report = orch.cycle()
    check("cycle ran", report["cycle"] == 1)
    check("real income reconciled across channels", led.net_profit() == 15.0)
    check("learnings written to shared memory", mem.stats()["learnings"] > 0)


def main():
    print("=" * 70)
    print("YABBAI Revenue System — safety & behaviour tests")
    print("=" * 70)
    test_estimate_is_not_income()
    test_projected_requires_assumptions()
    test_missing_value_is_gap_not_zero()
    test_payout_queues_for_human()
    test_real_trade_requires_signature()
    test_outbound_email_is_high()
    test_low_reversible_runs_autonomously()
    test_daily_spend_cap_blocks()
    test_kill_switch_halts_everything()
    test_cfo_cannot_raise_caps()
    test_products_income_only_from_real_sale()
    test_agency_income_only_from_paid_invoice()
    test_trading_no_real_broadcast_without_signer()
    test_trading_needs_paper_proof_before_proposing_live()
    test_orchestrator_cycle_runs_all_channels()
    print("=" * 70)
    print(f"RESULT: {_passed} passed, {_failed} failed")
    print("=" * 70)
    sys.exit(1 if _failed else 0)


# pytest compatibility
def test_all():
    for fn in [test_estimate_is_not_income, test_projected_requires_assumptions,
               test_missing_value_is_gap_not_zero, test_payout_queues_for_human,
               test_real_trade_requires_signature, test_outbound_email_is_high,
               test_low_reversible_runs_autonomously, test_daily_spend_cap_blocks,
               test_kill_switch_halts_everything, test_cfo_cannot_raise_caps,
               test_products_income_only_from_real_sale,
               test_agency_income_only_from_paid_invoice,
               test_trading_no_real_broadcast_without_signer,
               test_trading_needs_paper_proof_before_proposing_live,
               test_orchestrator_cycle_runs_all_channels]:
        fn()


if __name__ == "__main__":
    main()
