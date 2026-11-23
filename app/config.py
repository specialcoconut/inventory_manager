"""Application settings loaded from environment variables / `.env`."""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Grocy -----------------------------------------------------------------
    grocy_url: str = "http://grocy:80"
    grocy_api_key: str = ""
    # Location used when adding stock (falls back to the product's default location).
    grocy_default_location_id: Optional[int] = None
    # Fallback expiry offset when the product has no default best-before days.
    grocy_default_best_before_days: int = 7
    # Quantity unit for newly created products (falls back to the first unit in Grocy).
    grocy_default_quantity_unit_id: Optional[int] = None
    grocy_timeout_seconds: float = 10.0

    # --- Categories ------------------------------------------------------------
    category_delimiter: str = "/"

    # --- Printer ---------------------------------------------------------------
    printer_model: str = "QL-800"
    printer_backend: str = "linux_kernel"
    printer_identifier: str = "file:///dev/usb/lp0"
    printer_label_size: str = "62"
    printer_auto_cut: bool = True
    printer_red: bool = False

    # --- Labels / scanning -----------------------------------------------------
    barcode_prefix: str = "grcy:s:"

    # --- External barcode lookup -------------------------------------------------
    # Used to pre-fill product names for unknown barcodes. Empty string disables it.
    external_lookup_url: str = "https://world.openfoodfacts.org"
    external_lookup_timeout_seconds: float = 5.0

    # --- UX --------------------------------------------------------------------
    consume_countdown_seconds: int = Field(5, ge=1, le=60)

    # --- Server ----------------------------------------------------------------
    app_port: int = 8080

    @field_validator("grocy_url")
    @classmethod
    def _strip_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value.endswith("/api"):
            value = value[: -len("/api")]
        return value

    @field_validator("grocy_default_location_id", "grocy_default_quantity_unit_id", mode="before")
    @classmethod
    def _empty_to_none(cls, value):
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("category_delimiter")
    @classmethod
    def _non_empty_delimiter(cls, value: str) -> str:
        if not value:
            raise ValueError("CATEGORY_DELIMITER must not be empty")
        return value

    @property
    def grocy_api_base(self) -> str:
        return f"{self.grocy_url}/api"


@lru_cache
def get_settings() -> Settings:
    return Settings()
