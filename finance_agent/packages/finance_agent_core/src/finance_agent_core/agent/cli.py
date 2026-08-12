from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.agent import FinanceAgent
from finance_agent_core.agent.providers import (
    BondMockProvider,
    DomesticMockProvider,
    LocalTestProvider,
    LocalTestSettings,
    MockProvider,
)
from finance_agent_core.answering import LocalGroundedAnswerProvider
from finance_agent_core.storage import connect_read_only, load_manifest

DEFAULT_QUESTION = (
    "미국 채권형 해외 ETF 중 현재 거래 가능한 상품에서 "
    "총보수 0.20% 이하인 상품을 AUM 순으로 5개 보여줘."
)
DOMESTIC_DEFAULT_QUESTION = (
    "미국 주식형 국내 ETF 중 판매 가능하고 거래정지가 아니며 연금 거래 가능한 "
    "상품을 1개월 수익률 순으로 5개 보여줘."
)
BOND_DEFAULT_QUESTION = "매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the verified ETP finance agent.")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("artifacts/normalized/overseas_etp.sqlite3"),
    )
    parser.add_argument("--provider", choices=("mock", "local_test"), default="mock")
    parser.add_argument(
        "--answer-provider",
        choices=("deterministic", "local_test"),
        default="deterministic",
    )
    parser.add_argument("--question")
    parser.add_argument("--request-id", default="etp-cli-001")
    parser.add_argument("--output", type=Path)
    return parser


def _provider(name: str, database: Path):
    if name == "local_test":
        settings = LocalTestSettings.from_environment()
        provider = LocalTestProvider(settings)
        provider.healthcheck()
        return provider
    with connect_read_only(database) as connection:
        dataset = load_manifest(connection).dataset
    if dataset == "bond":
        return BondMockProvider()
    return DomesticMockProvider() if dataset == "domestic_etp" else MockProvider()


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    with connect_read_only(arguments.database) as connection:
        dataset = load_manifest(connection).dataset
    default_questions = {
        "overseas_etp": DEFAULT_QUESTION,
        "domestic_etp": DOMESTIC_DEFAULT_QUESTION,
        "bond": BOND_DEFAULT_QUESTION,
    }
    question = arguments.question or default_questions[dataset]
    answer_provider = None
    if arguments.answer_provider == "local_test":
        answer_provider = LocalGroundedAnswerProvider(LocalTestSettings.from_environment())
        answer_provider.healthcheck()
    agent = FinanceAgent(
        database_path=arguments.database,
        provider=_provider(arguments.provider, arguments.database),
        answer_provider=answer_provider,
        allow_unapproved_database=True,
    )
    response, composition = agent.answer_with_composition(question, arguments.request_id)
    if composition is None:
        rendered = response.model_dump_json(indent=2)
    else:
        rendered = json.dumps(
            {
                "response": response.model_dump(mode="json"),
                "answer_composition": composition.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(f"{rendered}\n", encoding="utf-8")
    print(rendered)
    return 0
