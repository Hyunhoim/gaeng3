from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from finance_agent_core.evaluation.models import EvaluationSuite


@dataclass(frozen=True)
class LoadedEvaluationSuite:
    suite: EvaluationSuite
    suite_sha256: str


def load_core_evaluation_suite(
    dataset: str = "overseas_etp",
) -> LoadedEvaluationSuite:
    resources = {
        "overseas_etp": "overseas_etp_core_50.json",
        "domestic_etp": "domestic_etp_core_50.json",
        "bond": "bond_core_50.json",
        "fund": "fund_core_50.json",
    }
    try:
        resource_name = resources[dataset]
    except KeyError as error:
        raise ValueError(f"no core evaluation suite for dataset: {dataset}") from error
    resource = files("finance_agent_core.evaluation.suites").joinpath(resource_name)
    raw = resource.read_bytes()
    payload: Any = json.loads(raw)
    return LoadedEvaluationSuite(
        suite=EvaluationSuite.model_validate(payload),
        suite_sha256=hashlib.sha256(raw).hexdigest(),
    )
