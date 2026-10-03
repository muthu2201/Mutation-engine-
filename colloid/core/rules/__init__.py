"""CRL, the Colloid Rule Language: optimisation patterns learned from verified evidence.

* ``sqlfacts`` - language-neutral facts about SQL statements (equality filters, sort orders)
* ``crl``      - the grammar (seeds), parser, validator, canonical printer and rule identity
* ``match``    - applying rules to a stack's queries: index proposals (candidates, never verdicts)
"""

from colloid.core.rules.crl import CRLError, Evidence, Rule, parse, render, render_file
from colloid.core.rules.match import IndexProposal, covers, proposals
from colloid.core.rules.sqlfacts import QueryFacts, SortKey, index_columns, primary_keys, query_facts

__all__ = ["CRLError", "Evidence", "IndexProposal", "QueryFacts", "Rule", "SortKey", "covers", "index_columns", "parse", "primary_keys",
           "proposals", "query_facts", "render", "render_file"]
