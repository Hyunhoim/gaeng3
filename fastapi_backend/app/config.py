from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from finance_agent_core.contracts.queryplan import ProductFamily


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
