from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from finance_agent_core.contracts.queryplan import ProductFamily
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

FundExecutionPolicy = Literal["locked", "public_fund_v1_approved"]
AuditMode = Literal["disabled", "jsonl"]


class Settings(BaseSettings):
    """Environment-backed settings kept outside the HTTP response contract."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
        frozen=True,
    )

    app_name: str = Field(
        default="Finance Product Agent API",
        validation_alias="FINANCE_APP_NAME",
    )
    app_version: str = Field(
        default="0.1.0",
        validation_alias="FINANCE_APP_VERSION",
    )
    app_env: Literal["development", "test", "evaluation", "production"] = Field(
        default="development",
        validation_alias="APP_ENV",
    )
    answer_provider: Literal["deterministic", "local_test", "hyperclova"] = Field(
        default="deterministic",
        validation_alias="FINANCE_BACKEND_ANSWER_PROVIDER",
    )
    hcx_query_plan_enabled: bool = Field(
        default=False,
        validation_alias="FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED",
    )
    llm_mode: Literal["disabled", "local_test", "evaluation", "production"] = Field(
        default="disabled",
        validation_alias="FINANCE_AGENT_LLM_MODE",
    )
    llm_provider: Literal["disabled", "local_test", "hyperclova"] = Field(
        default="disabled",
        validation_alias="LLM_PROVIDER",
    )
    hcx_model: str | None = Field(
        default=None,
        validation_alias="HCX_MODEL",
    )
    hcx_timeout_seconds: float = Field(
        default=45.0,
        gt=0,
        le=300,
        validation_alias="HCX_TIMEOUT_SECONDS",
    )
    clovastudio_api_key_file: Path | None = Field(
        default=None,
        validation_alias="CLOVASTUDIO_API_KEY_FILE",
    )
    official_answer_timeout_seconds: float = Field(
        default=270.0,
        gt=0,
        lt=300,
        validation_alias="OFFICIAL_ANSWER_TIMEOUT_SECONDS",
    )
    official_answer_max_inflight: int = Field(
        default=2,
        ge=1,
        le=8,
        validation_alias="OFFICIAL_ANSWER_MAX_INFLIGHT",
    )
    web_concurrency: int = Field(
        default=1,
        ge=1,
        le=8,
        validation_alias="WEB_CONCURRENCY",
    )
    audit_mode: AuditMode = Field(
        default="disabled",
        validation_alias="FINANCE_AUDIT_MODE",
    )
    audit_file: Path | None = Field(
        default=None,
        validation_alias="FINANCE_AUDIT_FILE",
    )
    audit_queue_capacity: int = Field(
        default=2_048,
        ge=1,
        le=100_000,
        validation_alias="FINANCE_AUDIT_QUEUE_CAPACITY",
    )
    audit_shutdown_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=60,
        validation_alias="FINANCE_AUDIT_SHUTDOWN_TIMEOUT_SECONDS",
    )
    audit_fsync_each_event: bool = Field(
        default=True,
        validation_alias="FINANCE_AUDIT_FSYNC_EACH_EVENT",
    )
    fund_execution_policy: FundExecutionPolicy = Field(
        default="locked",
        validation_alias="FINANCE_BACKEND_FUND_EXECUTION_POLICY",
    )
    release_manifest_file: Path | None = Field(
        default=None,
        validation_alias="FINANCE_RELEASE_MANIFEST_FILE",
    )
    deployment_binding_file: Path | None = Field(
        default=None,
        validation_alias="FINANCE_DEPLOYMENT_BINDING_FILE",
    )
    deployment_binding_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        validation_alias="FINANCE_DEPLOYMENT_BINDING_SHA256",
    )
    source_commit: str | None = Field(
        default=None,
        pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$",
        validation_alias="FINANCE_SOURCE_COMMIT",
    )
    runtime_image_reference: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9._:/-]{2,255}@sha256:[0-9a-f]{64}$",
        validation_alias="FINANCE_RUNTIME_IMAGE_REFERENCE",
    )
    runtime_platform: Literal["linux/amd64", "linux/arm64"] = Field(
        default="linux/amd64",
        validation_alias="FINANCE_RUNTIME_PLATFORM",
    )
    dense_schema_linker_enabled: bool = Field(
        default=False,
        validation_alias="FINANCE_DENSE_SCHEMA_LINKER_ENABLED",
    )
    product_dense_enabled: bool = Field(
        default=False,
        validation_alias="FINANCE_PRODUCT_DENSE_ENABLED",
    )
    relation_retrieval_artifact_file: Path | None = Field(
        default=None,
        validation_alias="FINANCE_RELATION_RETRIEVAL_ARTIFACT_FILE",
    )
    relation_retrieval_artifact_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        validation_alias="FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256",
    )
    relation_retrieval_artifact_sha256_file: Path | None = Field(
        default=None,
        validation_alias="FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256_FILE",
    )
    relation_index_file: Path | None = Field(
        default=None,
        validation_alias="FINANCE_RELATION_INDEX_FILE",
    )
    overseas_etp_db: Path | None = Field(
        default=None,
        validation_alias="FINANCE_DB_OVERSEAS_ETP",
    )
    domestic_etp_db: Path | None = Field(
        default=None,
        validation_alias="FINANCE_DB_DOMESTIC_ETP",
    )
    bond_db: Path | None = Field(
        default=None,
        validation_alias="FINANCE_DB_BOND",
    )
    fund_db: Path | None = Field(
        default=None,
        validation_alias="FINANCE_DB_FUND",
    )

    @model_validator(mode="before")
    @classmethod
    def reject_inline_hcx_credential_without_loading_it(cls, values: Any) -> Any:
        """Reject the forbidden variable by name; never ingest its secret value."""

        explicit_names = (
            {str(name).casefold() for name in values} if isinstance(values, dict) else set()
        )
        if "CLOVASTUDIO_API_KEY" in os.environ or "clovastudio_api_key" in explicit_names:
            raise ValueError(
                "inline HyperCLOVA credential is forbidden; use CLOVASTUDIO_API_KEY_FILE"
            )
        return values

    @model_validator(mode="after")
    def require_development_for_local_provider(self) -> Settings:
        """Keep provider and capability opt-ins fail-closed."""

        if self.answer_provider == "local_test" and self.app_env != "development":
            raise ValueError(
                "FINANCE_BACKEND_ANSWER_PROVIDER=local_test is allowed only in development"
            )
        if self.uses_hyperclova:
            if self.app_env not in {"evaluation", "production"}:
                raise ValueError("HyperCLOVA X is allowed only in evaluation or production")
            if self.llm_mode != self.app_env or self.llm_provider != "hyperclova":
                raise ValueError(
                    "HyperCLOVA X requires FINANCE_AGENT_LLM_MODE to match APP_ENV "
                    "and LLM_PROVIDER=hyperclova"
                )
            if self.hcx_model != "HCX-007":
                raise ValueError("Structured Outputs currently require HCX_MODEL=HCX-007")
            if self.clovastudio_api_key_file is None:
                raise ValueError("HyperCLOVA X requires CLOVASTUDIO_API_KEY_FILE")
        if self.fund_execution_policy == "public_fund_v1_approved" and self.fund_db is None:
            raise ValueError("approved public fund execution requires FINANCE_DB_FUND")
        release_values = (
            self.release_manifest_file,
            self.deployment_binding_file,
            self.deployment_binding_sha256,
            self.source_commit,
            self.runtime_image_reference,
        )
        if any(value is not None for value in release_values) and not all(
            value is not None for value in release_values
        ):
            raise ValueError("Agent release configuration must be supplied as one complete set")
        if self.app_env in {"evaluation", "production"} and self.web_concurrency != 1:
            raise ValueError(
                "evaluation/production requires WEB_CONCURRENCY=1 "
                "until cross-process audit aggregation exists"
            )
        if self.app_env in {"evaluation", "production"} and (
            self.dense_schema_linker_enabled or self.product_dense_enabled
        ):
            raise ValueError("production Dense retrieval remains disabled in release schema v1")
        relation_artifacts = (
            self.relation_retrieval_artifact_file,
            self.relation_index_file,
        )
        relation_trust = (
            self.relation_retrieval_artifact_sha256,
            self.relation_retrieval_artifact_sha256_file,
        )
        if any(value is not None for value in (*relation_artifacts, *relation_trust)) and (
            not all(value is not None for value in relation_artifacts)
            or sum(value is not None for value in relation_trust) != 1
        ):
            raise ValueError(
                "relation retrieval requires both artifacts and exactly one SHA-256 trust source"
            )
        relation_paths = (
            self.relation_retrieval_artifact_file,
            self.relation_index_file,
            self.relation_retrieval_artifact_sha256_file,
        )
        if any(path is not None and not path.is_absolute() for path in relation_paths):
            raise ValueError("relation retrieval files must use absolute paths")
        if (
            self.app_env in {"evaluation", "production"}
            and self.relation_retrieval_artifact_sha256_file is not None
        ):
            raise ValueError(
                "evaluation/production requires the explicit relation artifact SHA-256"
            )
        if self.audit_mode == "disabled" and self.audit_file is not None:
            raise ValueError("disabled audit mode cannot configure FINANCE_AUDIT_FILE")
        if self.audit_mode == "jsonl" and (
            self.audit_file is None or not self.audit_file.is_absolute()
        ):
            raise ValueError("JSONL audit mode requires an absolute FINANCE_AUDIT_FILE")
        if (
            self.app_env in {"evaluation", "production"}
            and all(value is not None for value in release_values)
            and self.audit_mode != "jsonl"
        ):
            raise ValueError("evaluation/production requires the JSONL audit boundary")
        if (
            self.app_env in {"evaluation", "production"}
            and all(value is not None for value in release_values)
            and not self.audit_fsync_each_event
        ):
            raise ValueError("evaluation/production requires durable audit fsync")
        return self

    @property
    def has_release_configuration(self) -> bool:
        return all(
            value is not None
            for value in (
                self.release_manifest_file,
                self.deployment_binding_file,
                self.deployment_binding_sha256,
                self.source_commit,
                self.runtime_image_reference,
            )
        )

    @property
    def uses_hyperclova(self) -> bool:
        """Return whether any request path needs the official HCLX transport."""

        return self.answer_provider == "hyperclova" or self.hcx_query_plan_enabled

    @property
    def relation_retrieval_configured(self) -> bool:
        return (
            self.relation_retrieval_artifact_file is not None
            and self.relation_index_file is not None
            and (
                (self.relation_retrieval_artifact_sha256 is None)
                != (self.relation_retrieval_artifact_sha256_file is None)
            )
        )

    @property
    def capability_execution_overrides(self) -> set[ProductFamily]:
        """Return only the versioned product contracts explicitly approved for execution."""

        if self.fund_execution_policy == "public_fund_v1_approved":
            return {ProductFamily.FUND}
        return set()

    @property
    def database_paths(self) -> dict[ProductFamily, Path]:
        """Return only configured databases, keyed by the core domain enum."""

        candidates = {
            ProductFamily.OVERSEAS_ETP: self.overseas_etp_db,
            ProductFamily.DOMESTIC_ETP: self.domestic_etp_db,
            ProductFamily.BOND: self.bond_db,
            ProductFamily.FUND: self.fund_db,
        }
        return {family: path for family, path in candidates.items() if path is not None}
