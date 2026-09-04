from __future__ import annotations

from pathlib import Path

from finance_agent_core.agent.grounded_planning import (
    GroundedConstraintProposal,
    GroundedPlanProposal,
)
from finance_agent_core.contracts.queryplan import (
    ConstraintOperator,
    Intent,
    ProductFamily,
)
from finance_agent_core.domain import DatabaseManifest, NormalizedOverseasEtpRecord
from finance_agent_core.evaluation.grounded_plan_audit import GroundedPlanAuditRunner
from finance_agent_core.evaluation.metamorphic import (
    MutationAxis,
    MutationBatch,
    MutationCandidate,
    MutationValidation,
)


class _ExactIdentityProvider:
    @property
    def provider_name(self) -> str:
        return "fake_grounded"

    @property
    def model_name(self) -> str:
        return "fake-grounded-v1"

    def generate_grounded_plan(
        self,
        question: str,
        question_id: str,
        product_family_hint: ProductFamily | None = None,
    ) -> GroundedPlanProposal:
        assert product_family_hint is ProductFamily.OVERSEAS_ETP
        return GroundedPlanProposal(
            schema_version="1.0",
            question_id=question_id,
            intent=Intent.SEARCH,
            product_family=ProductFamily.OVERSEAS_ETP,
            family_evidence_span="해외 ETF",
            constraints=[
                GroundedConstraintProposal(
                    field="ticker",
                    operator=ConstraintOperator.EQ,
                    value="B",
                    evidence_span="티커 B2",
                ),
                GroundedConstraintProposal(
                    field="product_type",
                    operator=ConstraintOperator.EQ,
                    value="ETF",
                    evidence_span="ETF",
                ),
            ],
            ranking=[],
            limit=1,
            limit_evidence_span="",
            comparison_fields=[],
            group_by=[],
            aggregations=[],
            ambiguities=[],
            unsupported_conditions=[],
        )


def _batch() -> MutationBatch:
    candidate = MutationCandidate(
        id="semantic-roundtrip-v1-001-semantic_formal",
        source_case_id="official-mock-v1-001",
        axis=MutationAxis.SEMANTIC_FORMAL,
        coverage_family=ProductFamily.OVERSEAS_ETP,
        source_question="해외 ETF 종목코드 B2의 상세 정보 조회",
        question="티커 B2인 해외 ETF 상품 상세 정보 조회",
        hard_literals=["B2", "ETF"],
        validation=MutationValidation(checks={"synthetic": True}, violations=[], passed=True),
    )
    return MutationBatch(
        batch_id="semantic-roundtrip-v1-fake",
        generated_at_utc="2026-08-08T00:00:00+00:00",
        protocol_id="semantic-roundtrip-v1",
        protocol_sha256="a" * 64,
        source_suite_id="official-mock-v1-30",
        source_suite_sha256="b" * 64,
        generator="expected",
        model=None,
        requested_count=1,
        generated_count=1,
        accepted_count=1,
        rejected_count=0,
        candidates=[candidate],
        interpretation_limits=["unit fixture"],
    )


def test_grounded_plan_audit_preserves_proposal_and_gate_decision(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    report = GroundedPlanAuditRunner(
        batch=_batch(),
        database_paths={ProductFamily.OVERSEAS_ETP: path},
        providers={ProductFamily.OVERSEAS_ETP: _ExactIdentityProvider()},
        database_sha256_by_family={ProductFamily.OVERSEAS_ETP.value: "c" * 64},
    ).run()

    assert report.summary.total == 1
    assert report.summary.provider_valid == 1
    assert report.summary.gate_accepted == 1
    assert report.cases[0].proposal is not None
    assert report.cases[0].gated_plan is not None
    assert report.cases[0].gated_plan.constraints[-1].field == "product_id"
    assert report.cases[0].gated_plan.constraints[-1].value == "AMX:B2"
