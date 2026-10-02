"""Mutation operators.

Every operator is a pure function from ``(parent genome, context, rng)`` to zero or more
:class:`~colloid.core.operators.base.Proposal` objects. Operators never evaluate anything;
they only propose. The LLM operator is split into a pure *request builder* and a pure
*response parser* so the core stays free of I/O - the engine performs the call through the
LLMProvider port in between.
"""
