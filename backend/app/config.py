from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):

    APP_NAME: str = "Axiom Debug"
    ENVIRONMENT: str = "development"

    DATABASE_URL: str = "postgresql+asyncpg://axiom:password@localhost:5434/axiom_debug"

    # Validated at call time, not at startup — lets the app boot (health
    # checks, docs) without a key, and only /analyze needs one. Groq is the
    # LLM provider: OpenAI-compatible tool calling, no Anthropic account
    # required. gpt-oss-120b for the agent (strong tool use); the smaller
    # gpt-oss-20b for the cheap citation verifier, and as the indexer's
    # extraction fallback when Gemini is unavailable or unconfigured.
    GROQ_API_KEY: str = ""
    ANALYSIS_MODEL: str = "openai/gpt-oss-120b"
    VERIFIER_MODEL: str = "openai/gpt-oss-20b"
    AGENT_MAX_ITERATIONS: int = 8

    # Hard ceiling per LLM call. Without this the SDK default applies, and a
    # degraded upstream could stall a request for AGENT_MAX_ITERATIONS x that.
    GROQ_TIMEOUT_SECONDS: float = 60.0

    # Gemini is used only for the indexer's bulk extraction pass — thousands
    # of small classify+summarize calls, the workload that blew through
    # Groq's 8000 TPM free-tier budget on gpt-oss-20b. Gemini's free tier
    # has a much higher TPM ceiling for this same workload, and its
    # OpenAI-compatible endpoint means it's a drop-in AsyncOpenAI client, not
    # a second SDK. The interactive agent (analysis + citation verification)
    # stays on Groq — that path is low-volume and already working.
    GEMINI_API_KEY: str = ""
    EXTRACTION_MODEL: str = "gemini-3.5-flash-lite"
    GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    GEMINI_TIMEOUT_SECONDS: float = 60.0

    # Cosine-distance floor for retrieval. Measured against bge-small on real
    # error text: ~0.05 same failure reworded, ~0.24 same exception different
    # object, ~0.38 a different Python error, ~0.48 unrelated infrastructure,
    # ~0.67 nonsense. 0.45 admits genuinely-related failures and rejects the
    # rest, so an empty index or an unmatched query returns nothing rather
    # than the least-bad row.
    RETRIEVAL_MAX_DISTANCE: float = 0.45

    # A repeat of the same failure_signature within this window is served
    # from the last analysis instead of re-running the agent. CI failures
    # recur constantly before someone fixes them — this skips a full
    # investigation (and its cost) for every repeat. Not indefinite: an
    # old answer can go stale as the index improves.
    CACHE_TTL_DAYS: int = 7

    # Indexer only — the served app never reads this.
    GITHUB_TOKEN: str = ""

    EMBEDDING_MODEL: str = "BAAI/bge-small-en-v1.5"
    EMBEDDING_DIM: int = 384

    LOGFIRE_TOKEN: str = ""

    # Comma-separated origins allowed to call the API from a browser.
    # Defaults cover a local Vite/Next dev server; set explicitly in prod.
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    model_config = SettingsConfigDict(
        env_file=".env"
    )


settings = Settings()
