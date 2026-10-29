import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def make_spec(version="3.0.3", paths=None, schemas=None, **extra):
    doc = {"openapi": version, "info": {"title": "T", "version": "1"}, "paths": paths or {}}
    if schemas:
        doc["components"] = {"schemas": schemas}
    doc.update(extra)
    return copy.deepcopy(doc)


def json_op(schema, status="200", request=None, params=None):
    op = {"responses": {status: {"description": "ok", "content": {"application/json": {"schema": schema}}}}}
    if request is not None:
        op["requestBody"] = {"required": True, "content": {"application/json": {"schema": request}}}
    if params is not None:
        op["parameters"] = params
    return op


@pytest.fixture
def spec():
    return make_spec


LLM_VARS = ("SENTINEL_LLM_PROVIDER", "LLM_PROVIDER", "SENTINEL_LLM_MODEL", "SENTINEL_LLM_TEMPERATURE", "OLLAMA_BASE_URL",
            "OLLAMA_MODEL", "OLLAMA_NUM_CTX", "OLLAMA_TIMEOUT", "OLLAMA_THINK", "OLLAMA_KEEP_ALIVE")


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """Tests never call a real model and never see the developer's backend/.env (provider, model, Ollama settings)."""
    for name in LLM_VARS:
        monkeypatch.delenv(name, raising=False)
    from app.agent import llm
    monkeypatch.setattr(llm, "LAST", {"check": None, "selftest": None, "investigation": None})
    monkeypatch.setenv("SENTINEL_LLM", "off")


@pytest.fixture(autouse=True)
def isolated_store():
    """Every test gets its own in-memory database; nothing touches backend/data/."""
    from app import main
    from app.store import Store
    store = Store(":memory:")
    main.set_store(store)
    yield store
    store.close()
