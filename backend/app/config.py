"""Runtime settings, read from environment variables (prefix SENTINEL_)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent
ENV_FILE = BACKEND_DIR / ".env"


def load_env_file(path: Path = ENV_FILE) -> list[str]:
    """Read KEY=VALUE lines from backend/.env (git-ignored) into the environment.

    The file WINS over variables already set in the shell or in Windows: the project's .env is the documented
    place for settings, so a stale machine-wide variable must not silently override it. Returns the names loaded."""
    loaded: list[str] = []
    if not path.is_file():
        return loaded
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value:
            os.environ[key] = value
            loaded.append(key)
    return loaded


load_env_file()


def _env(name: str, default: str) -> str:
    return os.environ.get(f"SENTINEL_{name}", default)


@dataclass(frozen=True)
class Settings:
    max_spec_bytes: int = int(_env("MAX_SPEC_BYTES", str(2 * 1024 * 1024)))
    max_context_bytes: int = int(_env("MAX_CONTEXT_BYTES", str(1024 * 1024)))
    samples_dir: Path = BACKEND_DIR / "samples"
    frontend_dist: Path = PROJECT_DIR / "frontend" / "dist"
    db_path: str = _env("DB_PATH", str(BACKEND_DIR / "data" / "sentinel.db"))
    cors_origins: list[str] = field(
        default_factory=lambda: _env("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(","))


settings = Settings()
