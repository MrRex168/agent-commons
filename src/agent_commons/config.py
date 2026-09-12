from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Agent Commons"
    environment: str = "development"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="AGENT_COMMONS_")


settings = Settings()
