from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Agent Commons"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://agent_commons:agent_commons@localhost:5432/agent_commons"
    api_url: str = "http://127.0.0.1:8000"
    api_key: str | None = None
    a2a_default_agent: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_prefix="AGENT_COMMONS_")


settings = Settings()
