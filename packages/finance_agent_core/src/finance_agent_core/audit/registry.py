from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


class InputDiscoveryError(ValueError):
    """Raised when a required workbook cannot be resolved unambiguously."""


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    prefix: str
    primary_key_fields: tuple[str, ...]
    validation_key_field: str
    validation_key_pattern: re.Pattern[str]
    metric_fields: tuple[str, ...]
    numeric_fields: frozenset[str]
    categorical_fields: frozenset[str]

    @property
    def data_pattern(self) -> str:
        return f"{self.prefix}_*datarows*.xlsx"

    @property
    def schema_pattern(self) -> str:
        return f"{self.prefix}_*schema*.xlsx"


DATASET_SPECS: tuple[DatasetSpec, ...] = (
    DatasetSpec(
        name="bond",
        prefix="PRBD01N001",
        primary_key_fields=("PD_NO",),
        validation_key_field="PD_NO",
        validation_key_pattern=re.compile(r"[A-Z0-9]{8,20}"),
        metric_fields=(
            "BUYABLE_QUANTITY",
            "BUY_YIELD",
            "AVG_ANNUAL_TAX_YIELD",
            "MAT_DT",
            "PD_STD_INFO_UPDATE",
        ),
        numeric_fields=frozenset({"BUYABLE_QUANTITY", "BUY_YIELD", "AVG_ANNUAL_TAX_YIELD"}),
        categorical_fields=frozenset(),
    ),
    DatasetSpec(
        name="domestic_etp",
        prefix="PREF01N001",
        primary_key_fields=("pd_itm_no",),
        validation_key_field="pd_itm_no",
        validation_key_pattern=re.compile(r"(?:KR|KRG)[A-Z0-9]{9,12}"),
        metric_fields=(
            "pd_grp_no",
            "pd_sale_yn",
            "pd_tr_yn",
            "cu_charge_rt",
            "du_chas_errt",
            "pd_dvid_yield",
        ),
        numeric_fields=frozenset({"cu_charge_rt", "du_chas_errt", "pd_dvid_yield"}),
        categorical_fields=frozenset({"pd_grp_no", "pd_sale_yn", "pd_tr_yn"}),
    ),
    DatasetSpec(
        name="overseas_etp",
        prefix="PREF02N001",
        primary_key_fields=("pd_exg_mkt_cd", "pd_itm_no"),
        validation_key_field="pd_itm_no",
        validation_key_pattern=re.compile(r".+"),
        metric_fields=(
            "pd_grp_no",
            "pd_sale_yn",
            "pd_tr_yn",
            "pd_isin_cd",
            "wu_inv_ast_type",
            "wu_inv_rgn",
            "cu_charge_rt",
            "du_er_1d",
            "du_last_aum",
        ),
        numeric_fields=frozenset({"cu_charge_rt", "du_er_1d", "du_last_aum"}),
        categorical_fields=frozenset(
            {
                "pd_grp_no",
                "pd_sale_yn",
                "pd_tr_yn",
                "wu_inv_ast_type",
                "wu_inv_rgn",
            }
        ),
    ),
    DatasetSpec(
        name="fund",
        prefix="PRFD01N001",
        primary_key_fields=("itm_no", "prfd_attr_cd"),
        validation_key_field="itm_no",
        validation_key_pattern=re.compile(r"KR[A-Z0-9]{10}"),
        metric_fields=(
            "itm_no",
            "prfd_attr_cd",
            "fd_nast_suma",
            "zrin_fd_ivst_risk_gcd",
            "fd_wk1_ern_r",
            "fd_mm1_ern_r",
            "fd_mm3_ern_r",
            "fd_mm6_ern_r",
            "fd_mm18_ern_r",
            "fd_yr1_ern_r",
            "fd_yr2_ern_r",
            "fd_yr3_ern_r",
            "fd_yr5_ern_r",
        ),
        numeric_fields=frozenset(
            {
                "fd_nast_suma",
                "fd_wk1_ern_r",
                "fd_mm1_ern_r",
                "fd_mm3_ern_r",
                "fd_mm6_ern_r",
                "fd_mm18_ern_r",
                "fd_yr1_ern_r",
                "fd_yr2_ern_r",
                "fd_yr3_ern_r",
                "fd_yr5_ern_r",
            }
        ),
        categorical_fields=frozenset({"prfd_attr_cd", "zrin_fd_ivst_risk_gcd"}),
    ),
)

DATASET_BY_NAME = {spec.name: spec for spec in DATASET_SPECS}


def discover_workbook(data_dir: Path, pattern: str) -> Path:
    candidates = sorted(path for path in data_dir.glob(pattern) if not path.name.startswith("~$"))
    if len(candidates) != 1:
        candidate_names = ", ".join(path.name for path in candidates) or "none"
        raise InputDiscoveryError(
            f"Expected exactly one workbook for {pattern!r}; found {candidate_names}"
        )
    return candidates[0]


def resolve_inputs(data_dir: Path, spec: DatasetSpec) -> tuple[Path, Path]:
    return (
        discover_workbook(data_dir, spec.data_pattern),
        discover_workbook(data_dir, spec.schema_pattern),
    )
