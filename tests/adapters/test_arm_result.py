"""The transfer A/B scores a run on its own L6 passes, never on later ``colloid verify`` passes (ADR 0008)."""

from colloid.adapters.store.sql_store import open_store
from colloid.core.models import Evaluation, ObjectiveEstimate, Program, ProgramStatus, Stage, Verdict
from colloid.core.objectives import gain_percent
from colloid.services.ladder import arm_result

T0 = 1_000_000.0


def _l6(store, pid: str, at: float, log_ratio: float, verdict: Verdict = Verdict.PASS) -> None:
    est = ObjectiveEstimate(objective="cost", reference="baseline", reference_program_id="base-x", log_ratio=log_ratio,
                            ci_lo=log_ratio - 0.05, ci_hi=log_ratio + 0.05, p_value=0.001, n_candidate=10, n_reference=10)
    store.put_evaluation(Evaluation(id=f"ev-{pid}-{at}", program_id=pid, stage=Stage.L6, protocol_id="deep", verdict=verdict,
                                    objectives=(est,), created_at=at))


def test_post_run_verification_passes_do_not_count(tmp_path):
    store = open_store(f"sqlite:///{tmp_path / 'colloid.db'}")
    store.put_program(Program(id="base-x", baseline_id="base-x", gene_ids=(), island="baseline", generation=0,
                              status=ProgramStatus.EVALUATED, created_at=T0))
    for pid in ("in-run", "after-run"):
        store.put_program(Program(id=pid, baseline_id="base-x", gene_ids=(), island="composition", generation=1,
                                  status=ProgramStatus.PROMOTED, created_at=T0))
    _l6(store, "in-run", T0 + 1800, 0.20)  # promoted half an hour into a one-hour run
    _l6(store, "in-run", T0 + 900, 0.18, Verdict.FAIL)
    _l6(store, "after-run", T0 + 5 * 3600, 0.40)  # passed only when `colloid verify` re-ran L6 hours later
    store.kv_set("result", {"elapsed_min": 60.0})
    store.close()
    arm = arm_result(str(tmp_path))
    assert arm.verified == 1 and arm.hours_to_best == 0.5 and arm.hours_to_first == 0.5
    assert arm.best_gain_pct == round(gain_percent(0.20), 2)
