"""CostModel adapter backed by a static price snapshot.

Colloid's headline metric is *dollars per unit of work at a fixed SLO* (blueprint
Recommendation 7). Every benchmark phase measures CPU-seconds consumed by the whole stack
(service + database process trees, from cgroup accounting) and the stack's memory footprint
(peak PSS); this adapter prices them.

Default snapshot: on-demand compute-optimised cloud capacity of the c7i class, expressed per
vCPU and per GB so it applies to any instance shape:

* ``usd_per_vcpu_hour``  = 0.0446  (c7i.large: $0.0893/h for 2 vCPU)
* ``usd_per_gb_hour``    = 0.0056  (memory share implied by r7i vs c7i pricing)

These are list prices snapshot for a region; change them in the experiment YAML to match
your own contract. The *ratio* between CPU and memory price is what shapes trade-offs
(e.g. "use 200 MB more shared_buffers to save 3% CPU").
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from colloid.ports import ResourceUsage


class StaticPriceCostModel:
    PORT_API = "1.0.0"

    def __init__(self, usd_per_vcpu_hour: float = 0.0446, usd_per_gb_hour: float = 0.0056, snapshot: str = "c7i-on-demand-2026") -> None:
        self.usd_per_cpu_second = usd_per_vcpu_hour / 3600.0
        self.usd_per_gb_second = usd_per_gb_hour / 3600.0
        self.snapshot = snapshot

    def price(self, usage: ResourceUsage) -> float:
        return usage.cpu_seconds * self.usd_per_cpu_second + usage.memory_gb_seconds * self.usd_per_gb_second

    def usd_per_million_requests(self, cpu_s_per_req: float, mem_gb: float, req_per_s: float) -> float:
        """Cost of serving 1M requests at ``req_per_s``: CPU actually burned plus the memory
        held for the time it takes to serve them."""
        seconds = 1e6 / max(req_per_s, 1e-9)
        return 1e6 * cpu_s_per_req * self.usd_per_cpu_second + mem_gb * seconds * self.usd_per_gb_second

    def describe(self) -> Mapping[str, Any]:
        return {
            "snapshot": self.snapshot,
            "usd_per_cpu_second": self.usd_per_cpu_second,
            "usd_per_gb_second": self.usd_per_gb_second,
        }
