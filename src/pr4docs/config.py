from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="PR4DOCS_", extra="ignore")

    model: str = "openai:gpt-4o-mini"
    storage: Path = Path("./storage")
    checkpoint_db: Path = Path("./pr4docs.sqlite")
    max_attempts: int = 3

    @property
    def uploads(self) -> Path:
        return self.storage / "uploads"

    @property
    def working(self) -> Path:
        return self.storage / "working"

    @property
    def output(self) -> Path:
        return self.storage / "output"

    def ensure_dirs(self) -> None:
        for path in (self.uploads, self.working, self.output):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
