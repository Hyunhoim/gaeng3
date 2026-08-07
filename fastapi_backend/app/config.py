from __future__ import annotations

from pathlib import Path
from typing import Literal

from finance_agent_core.contracts.queryplan import ProductFamily
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed settings kept outside the HTTP response contract."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
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
    answer_provider: Literal["deterministic", "local_test"] = Field(
        default="deterministic",
        validation_alias="FINANCE_BACKEND_ANSWER_PROVIDER",
    )
    official_answer_timeout_seconds: float = Field(
        default=55.0,
        gt=0,
        lt=60,
        validation_alias="OFFICIAL_ANSWER_TIMEOUT_SECONDS",
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

    @model_validator(mode="after")
    def require_development_for_local_provider(self) -> Settings:
        """Keep the non-HCX provider outside evaluation and production."""

        if self.answer_provider == "local_test" and self.app_env != "development":
            raise ValueError(
                "FINANCE_BACKEND_ANSWER_PROVIDER=local_test is allowed only in development"
            )
        return self

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
