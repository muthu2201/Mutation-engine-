"""StackZero "shop": the reference API service Colloid optimises.

It is written the way a first version of a real service usually is - correct, readable,
and not tuned: per-row queries inside loops, aggregation in Python, linear membership
tests, a C ranking library compiled with ordinary flags, and default runtime and database
settings. Every layer therefore has realistic headroom, which is what a reference target
for a cross-layer optimiser needs.
"""
