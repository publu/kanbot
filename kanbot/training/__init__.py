"""Self-improvement for workflow distillation (Part 2).

Closed loop: distill -> evaluate against real outcomes -> keep what's proven as
few-shot exemplars -> re-distill better. No model fine-tuning; the optimizable
"program" is the meta-prompt + the exemplar library, scored by execution-grounded
evals run on the user's own agents.
"""
