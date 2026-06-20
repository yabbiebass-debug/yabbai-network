"""
YABBAI Revenue System v1.0 — the shared spine.

Three channels (products / agency / trading) plug into ONE spine:
  truth/        — what counts as real money (the Absolute Truth Protocol)
  gates/        — autonomous within bounds; human approval for anything irreversible
  memory/       — shared store every agent reads & writes
  orchestrator  — the OBSERVE→ORIENT→DECIDE→ACT→MEASURE→LEARN loop

INVARIANTS (enforced in code, tested):
  1. Income is bookable ONLY from a payment-processor source. LLM "signals" are
     ESTIMATE and are EXCLUDED from totals — never rolled into real money.
  2. Every irreversible/payout/publish/outbound action is HIGH class → queued for
     human approval. Nothing autonomous touches real money without a click.
  3. The trading executor requires a per-action human signature + hard caps before
     any real broadcast. The wallet Signer is an interface you own; keys are never
     hardcoded and no transaction is ever faked.
"""
__version__ = "1.0.0"
