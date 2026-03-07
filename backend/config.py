from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "CoPilot Platform"
    app_version: str = "0.1.0"
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)
    gpu_vram_threshold_gb: float = Field(
        default=4.0,
        description="Minimum GPU VRAM in GB required for performance mode",
    )
    audit_logs_dir: Path = Field(
        default=Path("logs/sessions"),
        description="Directory for JSONL audit session logs, relative to the backend root",
    )

    # Ollama / LLM settings
    ollama_url: str = Field(
        default="http://localhost:11434",
        description="Base URL for the Ollama server (e.g. http://localhost:11434)",
    )
    ollama_model: str = Field(
        default="qwen2.5:3b",
        description="Ollama model name used for LLM reasoning (e.g. qwen2.5:3b)",
    )

    model_config = {
        "env_prefix": "COPILOT_",
        "env_file": ".env",
        "extra": "ignore",
    }


settings = Settings()