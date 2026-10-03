"""The judge for repairs in real repositories (SWE-bench, ADR 0011).

Search-time stages run inside the instance's container with the network off:

- L0: patch policy;
- L1: the patch applies and compiles;
- L2: the repository's existing tests that import the edited modules do not regress;
- L3: validated reproduction scripts.

The holdout is the official SWE-bench grader (``grader.py``), run in its own virtualenv on the
one submitted patch. ``tasks.jsonl`` holds what the search may see; ``gold.jsonl`` holds what
only the grader reads.
"""
