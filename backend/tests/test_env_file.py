from app.config import load_env_file


def test_env_file_parsing_and_precedence(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text('# comment\nOLLAMA_BASE_URL="http://127.0.0.1:11434"\nexport OLLAMA_MODEL=qwen3.5:4b\nEMPTY=\nALREADY=file\n',
                 encoding="utf-8")
    for k in ("OLLAMA_BASE_URL", "OLLAMA_MODEL", "EMPTY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ALREADY", "shell")
    loaded = load_env_file(f)
    import os
    assert os.environ["OLLAMA_BASE_URL"] == "http://127.0.0.1:11434" and os.environ["OLLAMA_MODEL"] == "qwen3.5:4b"
    assert os.environ["ALREADY"] == "file" and "EMPTY" not in os.environ  # .env wins over the shell
    assert sorted(loaded) == ["ALREADY", "OLLAMA_BASE_URL", "OLLAMA_MODEL"]
