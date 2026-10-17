"""Compatibility alias: the team is split into one module per agent.

  base.py          shared step / delegate / state helpers
  orchestrator.py  budget, round loop, recommendation
  scout.py         family scouts (recursive delegation) and the shortlist
  allocator.py     job selection
  critic.py        audits and sub-critics
  analyst.py       discovery write-ups
"""
from sieve.agents.orchestrator import run, run_sync  # noqa: F401
