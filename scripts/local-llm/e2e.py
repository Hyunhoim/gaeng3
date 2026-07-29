from __future__ import annotations

import argparse
from pathlib import Path

from finance_agent_core.agent import FinanceAgent
from finance_agent_core.agent.cli import DEFAULT_QUESTION
from finance_agent_core.agent.providers import (
    LocalTestProvider,
    LocalTestSettings,
    first_vertical_slice_plan,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run and verify the test-only local Qwen Agent vertical slice."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("artifacts/normalized/overseas_etp.sqlite3"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/e2e/local-qwen-response.json"),
    )
    return parser


EXPECTED_PRODUCT_IDS = [
    "NAS:BND.O",
    "AMX:AGG",
    "NYS:SGOV.K",
    "NAS:VCIT.O",
    "AMX:BIL",
]


def _semantic_payload(plan):
    payload = plan.model_dump(mode="json")
    payload.pop("question_id")
    payload.pop("projection")
    payload["constraints"] = sorted(
        payload["constraints"],
        key=lambda item: (item["field"], item["operator"]),
    )
    return payload


def _assert_expected_semantics(actual, expected) -> None:
    if _semantic_payload(actual) != _semantic_payload(expected):
        raise RuntimeError(
            "local model changed a constraint, ranking, intent, or limit"
        )
    missing_projection = set(expected.projection) - set(actual.projection)
    if missing_projection:
        raise RuntimeError(
            "local model omitted required projection fields: "
            f"{sorted(missing_projection)}"
        )


def main() -> int:
    arguments = build_parser().parse_args()
    settings = LocalTestSettings.from_environment()
    provider = LocalTestProvider(settings)
    health = provider.healthcheck()
    request_id = "local-qwen-e2e-001"
    response = FinanceAgent(arguments.database, provider).answer(
        DEFAULT_QUESTION,
        request_id,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        f"{response.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )

    expected = first_vertical_slice_plan(request_id)
    try:
        _assert_expected_semantics(response.query_plan, expected)
    except RuntimeError as error:
        raise RuntimeError(
            "local model returned a valid but semantically unexpected QueryPlan; "
            f"inspect {arguments.output}"
        ) from error
    if response.candidate_count != 440:
        raise RuntimeError(
            f"expected 440 verified candidates, got {response.candidate_count}"
        )
    product_ids = [product.product_id for product in response.products]
    if product_ids != EXPECTED_PRODUCT_IDS:
        raise RuntimeError(
            f"expected product IDs {EXPECTED_PRODUCT_IDS}, got {product_ids}"
        )

    print(
        {
            "health": health,
            "request_id": response.request_id,
            "candidate_count": response.candidate_count,
            "product_ids": product_ids,
            "output": str(arguments.output),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
