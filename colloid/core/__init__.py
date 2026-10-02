"""Pure domain core of Colloid.

Nothing in this package performs I/O. Every function takes data in and returns data out,
which is what makes the search, attribution and selection logic unit-testable and
reproducible from a seed. Side effects (running benchmarks, calling LLMs, writing to the
program database) happen in adapters and services that call into the core.
"""
