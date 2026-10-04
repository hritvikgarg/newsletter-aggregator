"""Newsletter aggregator pipeline.

Stages (see PIPELINE.md):
  M1 capture  -> raw .eml + `messages` rows        (this package: capture.py)
  M2 split    -> clean text + `items`               (todo)
  M3 enrich   -> per-item JSON via Groq/Qwen        (todo, never Claude)
  M4 cluster  -> `stories` across sources           (todo)
  M5 compose  -> our own digest issue               (todo)
"""

__version__ = "0.1.0"
