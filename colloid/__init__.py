"""Colloid: an AI-driven, cross-layer mutation engine for software stacks.

The package is organised as a hexagon (ports and adapters):

* ``colloid.core``      - the pure domain: Stack Atlas, genomes, search, attribution.
                          It performs no I/O and imports no SDKs (enforced by import-linter).
* ``colloid.ports``     - typing.Protocol contracts the core talks through.
* ``colloid.adapters``  - concrete implementations (LLMs, sandbox, stores, the StackZero target).
* ``colloid.services``  - the engine loop, CLI and dashboard that wire everything together.

The evaluator lives in the sibling package ``colloid_evaluator`` and is deliberately
independent of the search machinery: it is the security boundary that decides whether a
candidate is correct and faster, so the code that *proposes* mutations must not be able to
influence how they are *judged*.
"""

__version__ = "0.1.0"
