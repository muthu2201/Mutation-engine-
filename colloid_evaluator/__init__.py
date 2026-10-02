"""colloid_evaluator: the judge, kept separate from the search that it judges.

The blueprint's central lesson is that every public failure of LLM-driven optimisation
(Sakana's CUDA engineer, the Darwin Gödel Machine removing its own hallucination markers,
inflated KernelBench numbers) was an *evaluator* failure. This package is therefore built
as an adversarial security boundary (blueprint D1-D3):

* It owns everything a candidate could exploit: request generators (with hidden, fresh
  seeds per evaluation), the holdout workload the search never sees, reference outputs,
  float tolerances, the timing harness and the policy rules.
* It never imports the search machinery (enforced by import-linter), so nothing in the
  proposal side can change how proposals are judged.
* Candidates run in a sandbox with no network, as an unprivileged user; reference outputs
  are computed at evaluation time by the baseline running beside them, and are never on
  disk where a candidate could read them.
* A canary suite of known reward hacks must be rejected 100% before any run starts.
"""

__version__ = "0.1.0"
