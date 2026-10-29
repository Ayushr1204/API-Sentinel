import json

from app.spec_loader import load_spec
from conftest import make_spec


def test_valid_30_yaml():
    text = "openapi: 3.0.3\ninfo: {title: X, version: '1'}\npaths: {}\n"
    r = load_spec(text, "x.yaml")
    assert r.valid and r.openapi_version == "3.0.3" and r.title == "X"


def test_valid_31_json():
    r = load_spec(json.dumps(make_spec("3.1.0")), "x.json")
    assert r.valid, r.errors


def test_swagger2_rejected():
    r = load_spec(json.dumps({"swagger": "2.0", "info": {"title": "x", "version": "1"}, "paths": {}}))
    assert not r.valid and "Swagger" in r.errors[0].message


def test_asyncapi_rejected():
    r = load_spec(json.dumps({"asyncapi": "2.6.0"}))
    assert not r.valid and "AsyncAPI" in r.errors[0].message


def test_unsupported_version():
    r = load_spec(json.dumps(make_spec("3.2.0")))
    assert not r.valid and "3.0.x or 3.1.x" in r.errors[0].message


def test_remote_ref_rejected_without_fetching():
    doc = make_spec(paths={"/a": {"get": {"responses": {"200": {"$ref": "https://evil.example/x.yaml"}}}}})
    r = load_spec(json.dumps(doc))
    assert not r.valid and "External $ref" in r.errors[0].message


def test_dangling_ref_reported():
    doc = make_spec(paths={"/a": {"get": {"responses": {"200": {"description": "ok", "content": {
        "application/json": {"schema": {"$ref": "#/components/schemas/Missing"}}}}}}}})
    r = load_spec(json.dumps(doc))
    assert not r.valid and "does not resolve" in r.errors[0].message


def test_schema_violation_reported():
    r = load_spec(json.dumps({"openapi": "3.0.3", "info": {"title": "x"}, "paths": {}}))
    assert not r.valid and any("version" in e.message for e in r.errors)


def test_invalid_yaml_and_empty_and_non_object():
    assert "Invalid YAML" in load_spec("a: [unclosed").errors[0].message
    assert "empty" in load_spec(b"").errors[0].message
    assert "object" in load_spec("- 1\n- 2").errors[0].message


def test_size_limit():
    big = "openapi: 3.0.3\n" + "#" * (3 * 1024 * 1024)
    r = load_spec(big)
    assert not r.valid and "limit" in r.errors[0].message


def test_yaml_safe_load_blocks_python_tags():
    r = load_spec("!!python/object/apply:os.system ['echo hi']")
    assert not r.valid and "Invalid YAML" in r.errors[0].message
