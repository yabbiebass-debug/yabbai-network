#!/usr/bin/env python3
"""
YABBAI DeFi Engine — Simulator Demo

Wires every component together and runs a simulated session so you can SEE the
whole thing work end-to-end with zero risk and no network:

  PriceFeed → RoutingEngine → Strategy → ModeController → RiskGate → PaperExecutor

Run:  python -m simulator.demo

This answers your real question honestly: start with $14, run it "overnight"
(simulated as many ticks), and read the final portfolio curve. The number you
see is what the strategy WOULD have done — including fees and slippage — without
risking a cent. That is the entire purpose of the simulator.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simulator.risk.risk_gate import RiskGate, RiskLimits
from simulator.core.mode_controller import ModeController, Mode
from simulator.core.paper_executor import PaperExecutor, VirtualPortfolio
from simulator.routing.routing_engine import RoutingEngine
from simulator.routing.price_feed import PriceFeed
from simulator.strategies.baseline_strategy import MomentumMeanReversionStrategy


def run_demo(ticks: int = 480, mode: Mode = Mode.AUTO):
    # A fake allowlisted token for the synthetic run
    DEMO_TOKEN = "So11111111111111111111111111111111111111112"

    # 1. Limits — the safety box. With $14, keep trades tiny.
    limits = RiskLimits(
        max_trade_usd=4.0,
        daily_loss_cap_usd=4.0,        # halt everything if down $4 in a day
        daily_volume_cap_usd=40.0,
        stop_loss_pct=20.0,            # your requested 20% stop
        take_profit_pct=50.0,          # take profit at +50% (a ceiling, not a promise)
        max_open_positions=1,
        token_allowlist=[DEMO_TOKEN],
        kill_switch=False,
    )
    problems = limits.validate()
    if problems:
        print("Limit warnings:", problems)

    gate = RiskGate(limits)

    # 2. Portfolio + paper executor — starts with $14
    portfolio = VirtualPortfolio(starting_usd=14.0, cash_usd=14.0)
    feed = PriceFeed(window=50, mode="synthetic")
    executor = PaperExecutor(portfolio, feed.price_lookup)

    # 3. Mode controller (AUTO for the demo — runs unattended within limits)
    controller = ModeController(gate, executor, mode=mode)

    # 4. Routing engine + strategy (no LLM advisor in the offline demo)
    engine = RoutingEngine(gate, controller, feed, llm_advisor=None)
    engine.set_strategy(MomentumMeanReversionStrategy())

    # Warm up the price history so indicators are valid
    for _ in range(20):
        feed.update_synthetic(DEMO_TOKEN)

    print(f"\n=== YABBAI DeFi Simulator — {mode.value.upper()} mode ===")
    print(f"Starting capital: ${portfolio.starting_usd:.2f}")
    print(f"Stop-loss: {limits.stop_loss_pct}%  Take-profit: {limits.take_profit_pct}%")
    print(f"Running {ticks} ticks (simulated 'overnight')...\n")

    # 5. The loop — each tick = one decision cycle
    for t in range(ticks):
        feed.update_synthetic(DEMO_TOKEN)
        engine.tick()
        status = gate.status()
        if status["halted"]:
            print(f"  [tick {t}] HALTED: daily loss cap or kill switch tripped. "
                  "Engine stops trading until reset — exactly as designed.")
            break

    # 6. Final honest report
    snap = executor.snapshot()
    print("\n=== RESULT (paper, includes fees + slippage) ===")
    print(f"Starting:        ${snap['starting_usd']:.2f}")
    print(f"Final value:     ${snap['total_value_usd']:.2f}")
    print(f"P&L:             ${snap['unrealized_plus_realized_pnl_usd']:+.2f} "
          f"({snap['pnl_pct']:+.2f}%)")
    print(f"Trades executed: {snap['trade_count']}")
    print(f"Open positions:  {snap['open_positions']}")
    print("\nHONEST NOTE: this is ONE synthetic run. Real markets differ every time.")
    print("Run it many times and most simple strategies hover around break-even")
    print("after fees — which is the truth the simulator exists to show you.\n")
    return snap


if __name__ == "__main__":
    run_demo()
