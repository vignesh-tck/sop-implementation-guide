from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str
    anthropic_api_key: str
    # Set to false on corporate networks with SSL inspection (self-signed CA in chain)
    anthropic_ssl_verify: bool = True
    app_env: str = "development"
    log_level: str = "INFO"

    # Feasibility score weights — adjustable without code changes
    weight_funding: float = 0.40
    weight_zoning: float = 0.35
    weight_policy: float = 0.25


settings = Settings()
