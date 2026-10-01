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

    # Funding sub-score points per approved programme. Every signal counted here is
    # one a reviewer kept, so the score rates how well the money matches the block's
    # recommendations rather than how many candidates the model emitted.
    points_strong_fit: float = 40.0           # fit >= relevance_high
    points_marginal: float = 5.0              # relevance_floor <= fit < relevance_high
    # relevance_floor doubles as the cut-off for a blanket 'approved': fits below it
    # are proposals nobody chose, so they are not written.
    relevance_high: float = 0.6
    relevance_floor: float = 0.3

    # DEPRECATED — the two-tier eligibility scoring these fed was removed when
    # eligibility stopped being determined deterministically. Left in place so an
    # existing .env carrying them still loads; nothing reads them.
    points_confirmed_eligible: float = 30.0
    points_relevant: float = 15.0

    # Each tier contributes at most this many signals. Without per-tier caps an
    # uncapped sum hits the 100 ceiling on almost any block — every block scored
    # 100 and the sub-score stopped discriminating at all.
    max_counted_per_tier: int = 2


settings = Settings()
