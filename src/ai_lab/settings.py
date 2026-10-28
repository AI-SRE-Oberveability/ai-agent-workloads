import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    api_key: str
    retrieval_url: str = "http://127.0.0.1:18081"
    allow_faults: bool = False
    otlp_endpoint: str | None = None
    version: str = "0.1.0-local"

    def __post_init__(self):
        if len(self.api_key) < 24:
            raise ValueError("LAB_API_KEY must contain at least 24 characters")

    @classmethod
    def from_env(cls):
        return cls(
            api_key=os.environ.get("LAB_API_KEY", ""),
            retrieval_url=os.environ.get("RETRIEVAL_URL", "http://127.0.0.1:18081"),
            allow_faults=os.environ.get("ALLOW_FAULTS") == "1",
            otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
            version=os.environ.get("LAB_VERSION", "0.1.0-local"),
        )
