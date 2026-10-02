"""Run configuration (loaded from experiment YAML).

An experiment is *data*, never code (blueprint G: ``experiments/`` holds run configs only).
A run is reproducible from its config plus the baseline commit and the environment
fingerprint the evaluator records.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class LLMArmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str  # "anthropic" | "local" | "none"
    model: str
    templates: list[str] = Field(default_factory=lambda: ["optimize"])


class EngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "run"
    seed: int = 0
    generations: int = 20
    proposals_per_generation: int = 8
    rate_rps: float | None = None  # None = calibrate on the baseline

    # islands
    pool_capacity: int = 12
    migration_interval: int = 3
    stagnation_limit: int = 4
    budget_epsilon: float = 0.2
    regions: list[str] | None = None  # None = all target regions

    # operators / bandit
    llm_arms: list[LLMArmConfig] = Field(default_factory=list)
    code_operators: list[str] = Field(default_factory=lambda: ["py_rewrite", "gi_edit"])
    knob_operators: list[str] = Field(default_factory=lambda: ["knob_sample", "knob_perturb", "knob_reset"])
    redteam_island: bool = True
    bandit_prior_mean: float = 0.03

    # cascade
    surrogate_keep_fraction: float = 0.6
    run_l5_top_k: int = 4  # per generation, globally, the top-k L4 survivors go to L5
    promote: bool = True  # run L6 on new global-best elites

    # attribution
    shapley_every: int = 6
    shapley_max_genes: int = 10
    shapley_budget_subsets: int = 48
    splice_every: int = 8
    splice_pool: int = 11
    splice_top_k: int = 4

    # infra
    store_url: str = "sqlite:///runs/{name}/colloid.db"
    telemetry_path: str = "runs/{name}/events.jsonl"
    profile: bool = True
    profile_delays: list[float] = Field(default_factory=lambda: [0.0, 0.5, 1.0])
    aa_runs: int = 0  # A/A runs before the evolutionary loop (0 = skip)
    cost_usd_per_vcpu_hour: float = 0.0446
    cost_usd_per_gb_hour: float = 0.0056
    max_runtime_minutes: float | None = None

    def resolved(self, key: str) -> str:
        return getattr(self, key).replace("{name}", self.name)

    @staticmethod
    def load(path: str | Path) -> EngineConfig:
        return EngineConfig.model_validate(yaml.safe_load(Path(path).read_text()))

    def run_dir(self) -> Path:
        return Path("runs") / self.name
