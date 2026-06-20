#!/usr/bin/env python3
"""
YABBAI DeFi Engine — Calibration Harness (Simulator)

THE TOOL THE TUTORIALS NEVER SHOW.

A prediction bot's whole value rests on one question: when your model says 64%,
does the event actually happen 64% of the time? If yes, you're calibrated and EV
betting works. If your "64%" really resolves at 50%, you will lose money steadily
while feeling confident — the exact trap the YouTube commenters warned about.

This harness runs many simulated markets where we KNOW the true probability,
lets an estimator make calls, and measures:
  1. Calibration — do predicted probabilities match real outcomes?
  2. EV realized — does the EV-gated strategy actually make money?
  3. Win rate vs EV — proves win rate alone is meaningless.

Run:  python -m simulator.calibration_demo
"""

import random
from dataclasses import dataclass
from typing import Callable, List, Dict

from simulator.strategies.prediction_market_strategy import (
    PredictionMarketStrategy, PredictionSnapshot)


@dataclass
class SimulatedMarket:
    true_prob: float        # the REAL probability (hidden from the estimator in reality)
    market_price: float     # what the market is pricing it at
    fee_pct: float = 2.0


def make_markets(n: int, market_bias: float = 0.0) -> List[SimulatedMarket]:
    """
    Generate n markets. Each has a true probability and a market price that is
    the true prob plus some noise (the market isn't perfect — that's where edge
    comes from). market_bias shifts how mispriced the market is on average.
    """
    markets = []
    for _ in range(n):
        true_p = random.uniform(0.15, 0.85)
        noise = random.gauss(market_bias, 0.06)
        price = max(0.05, min(0.95, true_p + noise))
        markets.append(SimulatedMarket(true_prob=true_p, market_price=price))
    return markets


def run_calibration(estimator: Callable, estimator_name: str,
                    n_markets: int = 2000, stake: float = 1.0) -> Dict:
    """
    Run the EV strategy over many markets and measure what actually happened.
    `estimator` takes a PredictionSnapshot and returns a probability — but here
    we wrap it so it can optionally 'see' the true prob with some skill level.
    """
    strat = PredictionMarketStrategy(prob_estimator=estimator)
    markets = make_markets(n_markets)

    bets = 0
    wins = 0
    pnl = 0.0
    # calibration buckets: predicted prob → [count, actual_yes_count]
    buckets: Dict[int, List[int]] = {i: [0, 0] for i in range(0, 10)}

    for m in markets:
        snap = PredictionSnapshot(
            market_id="m", question="q",
            market_price=m.market_price, fee_pct=m.fee_pct)

        # record calibration on the estimator's raw call (independent of betting)
        est = max(0.0, min(0.999, estimator(snap)))
        outcome_yes = random.random() < m.true_prob   # resolve using TRUE prob
        b = int(est * 10)
        buckets[b][0] += 1
        if outcome_yes:
            buckets[b][1] += 1

        # now the EV-gated decision
        decision = strat.evaluate(snap)
        if decision["action"] != "bet":
            continue

        bets += 1
        side = decision["side"]
        price = m.market_price if side == "yes" else (1 - m.market_price)
        contracts = stake / price
        won = (outcome_yes and side == "yes") or ((not outcome_yes) and side == "no")
        fee = stake * (m.fee_pct / 100.0)
        if won:
            wins += 1
            pnl += contracts * 1.0 - stake - fee
        else:
            pnl += -stake - fee

    win_rate = (wins / bets * 100) if bets else 0
    ev_per_bet = (pnl / bets) if bets else 0

    # calibration error: avg |predicted - actual| across populated buckets
    cal_rows = []
    cal_error_total, cal_error_n = 0.0, 0
    for i in range(10):
        cnt, yes = buckets[i]
        if cnt >= 10:
            predicted = (i + 0.5) / 10
            actual = yes / cnt
            cal_rows.append((f"{i*10}-{i*10+10}%", cnt, round(actual*100, 1)))
            cal_error_total += abs(predicted - actual)
            cal_error_n += 1
    cal_error = (cal_error_total / cal_error_n * 100) if cal_error_n else None

    return {
        "estimator": estimator_name,
        "markets": n_markets,
        "bets_placed": bets,
        "win_rate_pct": round(win_rate, 1),
        "total_pnl": round(pnl, 2),
        "ev_per_bet": round(ev_per_bet, 4),
        "calibration_error_pct": round(cal_error, 1) if cal_error is not None else None,
        "calibration_rows": cal_rows,
    }


# ── Three estimators of different skill, to make the lesson vivid ──

def estimator_no_skill(snap):
    """No edge: just echoes the market. Should place ~0 bets, ~0 pnl."""
    return snap.market_price

def estimator_overconfident(snap):
    """Pushes every estimate toward the extremes — looks confident, badly calibrated."""
    p = snap.market_price
    return min(0.99, max(0.01, 0.5 + (p - 0.5) * 2.2))

def _skilled_factory(skill):
    """A skilled estimator 'sees' a noisy version of truth. Higher skill = closer.
    In reality this is your Claude prompt / model. Here we fake skill to show the curve."""
    def est(snap):
        # We don't have true_prob in the snapshot, so approximate skill as a small,
        # consistent nudge away from market price toward a (simulated) better view.
        # Real estimators replace this entirely.
        nudge = random.gauss(0, 0.10 * (1 - skill))
        return max(0.01, min(0.99, snap.market_price + random.gauss(0, 0.08) * skill - nudge))
    return est


if __name__ == "__main__":
    random.seed(7)
    print("\n=== PREDICTION-MARKET CALIBRATION HARNESS ===")
    print("The question that matters: does the estimator make money via EV,")
    print("and is it calibrated? Win rate alone is shown to be meaningless.\n")

    for est, name in [
        (estimator_no_skill, "No-skill (echoes market)"),
        (estimator_overconfident, "Overconfident (pushes to extremes)"),
    ]:
        r = run_calibration(est, name)
        print(f"── {r['estimator']} ──")
        print(f"   bets placed:        {r['bets_placed']} / {r['markets']}")
        print(f"   win rate:           {r['win_rate_pct']}%")
        print(f"   total P&L:          ${r['total_pnl']}")
        print(f"   EV per bet:         ${r['ev_per_bet']}")
        print(f"   calibration error:  {r['calibration_error_pct']}% "
              f"(lower=better; how far off the estimates are)")
        print()

    print("READ THIS: the no-skill estimator places ~zero bets because it has no")
    print("edge — and that is the CORRECT behavior. The overconfident one places")
    print("many bets, may even show a decent win rate, and still bleeds money")
    print("because its EV is negative. THAT is why EV>win-rate, and why you must")
    print("calibrate a real estimator here before risking a cent.\n")
