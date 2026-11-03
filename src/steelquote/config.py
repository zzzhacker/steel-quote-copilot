from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    odoo_url: str = "http://127.0.0.1:8069"
    odoo_db: str = "steel"
    odoo_api_key: str = ""
    er_dsn: str = "postgresql+psycopg://odoo:odoo@127.0.0.1:5432/er"
    er_mcp_url: str = "http://127.0.0.1:8765/mcp"
    anthropic_api_key: str = ""
    google_api_key: str = ""
    gemini_api_key: str = ""

    # model per graph node: the routing the evals are meant to justify
    model_extract: str = "gemini-3.5-flash-lite"
    model_disambiguate: str = "gemini-3.5-flash-lite"
    model_revise: str = "gemini-3.5-flash-lite"
    effort: str = "low"  # thinking depth for extraction-style calls


@lru_cache
def get_settings() -> Settings:
    return Settings()
