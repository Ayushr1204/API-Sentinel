"""Any spec version to any spec version, in both directions.

The Bookings fixtures describe the same two contracts (v1, v2) written natively in Swagger 2.0 and OpenAPI
3.0 / 3.1 / 3.2 (see fixtures/gen_bookings.py). Format alone must never look like an API change, and the real
changes must be found identically whatever the formats on each side.
"""
import itertools
from pathlib import Path

import pytest
from openapi_spec_validator import OpenAPIV30SpecValidator

from app.convert import EXTRA_METHODS_KEY, SOURCE_KEY, swagger2_to_oas3, to_internal
from app.diff import diff_specs
from app.oas import operations
from app.spec_loader import load_spec

FIX = Path(__file__).parent / "fixtures" / "bookings"
FORMATS = ["swagger20", "oas30", "oas31", "oas32"]
LABELS = {"swagger20": "Swagger 2.0", "oas30": "OpenAPI 3.0.3", "oas31": "OpenAPI 3.1.0", "oas32": "OpenAPI 3.2.0"}


def load(tag, fmt):
    r = load_spec((FIX / tag / f"{fmt}.yaml").read_text(encoding="utf-8"), f"{tag}-{fmt}.yaml")
    assert r.valid, r.errors
    return r


def diff(base, cand):
    return [c.to_dict() for c in diff_specs(base, cand)]


def signature(changes):
    return sorted((c["rule"], c["severity"], c["location"]) for c in changes if c["rule"] != "SPEC_FORMAT_CHANGED")


@pytest.mark.parametrize("fmt", FORMATS)
def test_every_format_loads_and_is_labelled(fmt):
    r = load("v1", fmt)
    assert r.spec_format == LABELS[fmt]
    assert r.doc[SOURCE_KEY]["label"] == LABELS[fmt]
    assert r.doc["openapi"].startswith("3.1")


@pytest.mark.parametrize("tag", ["v1", "v2"])
@pytest.mark.parametrize("a,b", list(itertools.product(FORMATS, FORMATS)))
def test_same_contract_in_two_formats_has_no_api_changes(tag, a, b):
    changes = diff(load(tag, a).doc, load(tag, b).doc)
    assert signature(changes) == [], signature(changes)
    fmt_changes = [c for c in changes if c["rule"] == "SPEC_FORMAT_CHANGED"]
    assert len(fmt_changes) == (0 if a == b else 1)
    for c in fmt_changes:
        assert c["severity"] == "NON_BREAKING"


def test_real_changes_identical_across_all_format_pairs():
    reference = signature(diff(load("v1", "oas31").doc, load("v2", "oas31").doc))
    assert len(reference) >= 10
    for a, b in itertools.product(FORMATS, FORMATS):
        assert signature(diff(load("v1", a).doc, load("v2", b).doc)) == reference, (a, b)


def test_old_to_new_and_new_to_old_are_both_supported():
    """Upgrade and downgrade in either release direction: severity follows the API change, not the format."""
    for a, b in itertools.product(FORMATS, FORMATS):
        fwd = diff(load("v1", a).doc, load("v2", b).doc)   # release v2, format a -> b
        back = diff(load("v2", b).doc, load("v1", a).doc)  # roll back v2 -> v1, format b -> a
        assert any(c["severity"] == "BREAKING" for c in fwd)
        assert any(c["severity"] == "BREAKING" for c in back)
        if a != b:
            msg = next(c for c in fwd if c["rule"] == "SPEC_FORMAT_CHANGED")["message"]
            back_msg = next(c for c in back if c["rule"] == "SPEC_FORMAT_CHANGED")["message"]
            assert ("upgrade" in msg) != ("upgrade" in back_msg) or "patch" in msg


def test_converted_swagger_is_valid_openapi_30():
    import yaml
    for tag in ("v1", "v2"):
        doc = yaml.safe_load((FIX / tag / "swagger20.yaml").read_text(encoding="utf-8"))
        out = swagger2_to_oas3(doc)
        OpenAPIV30SpecValidator(out).validate()
        assert "definitions" not in out and "Booking" in out["components"]["schemas"]
        assert out["servers"][0]["url"] == "https://api.stayflow.example/v1"


def test_swagger_formdata_global_params_and_security():
    doc = {"swagger": "2.0", "info": {"title": "T", "version": "1"}, "basePath": "/api",
           "consumes": ["application/json"], "produces": ["application/json"],
           "parameters": {"Limit": {"name": "limit", "in": "query", "type": "integer", "maximum": 50}},
           "responses": {"NotFound": {"description": "missing"}},
           "securityDefinitions": {"basic": {"type": "basic"},
                                   "oauth": {"type": "oauth2", "flow": "accessCode", "authorizationUrl": "https://a/x",
                                             "tokenUrl": "https://a/t", "scopes": {"read": "r"}}},
           "paths": {"/files": {
               "get": {"parameters": [{"$ref": "#/parameters/Limit"},
                                      {"name": "ids", "in": "query", "type": "array", "items": {"type": "string"},
                                       "collectionFormat": "multi"}],
                       "responses": {"200": {"description": "ok", "schema": {"type": "array",
                                                                            "items": {"type": "string"}}},
                                     "404": {"$ref": "#/responses/NotFound"}}},
               "post": {"consumes": ["multipart/form-data"],
                        "parameters": [{"name": "file", "in": "formData", "type": "file", "required": True},
                                       {"name": "note", "in": "formData", "type": "string"}],
                        "responses": {"201": {"description": "created"}}}}}}
    out = swagger2_to_oas3(doc)
    OpenAPIV30SpecValidator(out).validate()
    get = out["paths"]["/files"]["get"]
    assert get["parameters"][0] == {"$ref": "#/components/parameters/Limit"}
    assert get["parameters"][1]["style"] == "form" and get["parameters"][1]["explode"] is True
    assert get["responses"]["404"] == {"$ref": "#/components/responses/NotFound"}
    body = out["paths"]["/files"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert body["properties"]["file"] == {"type": "string", "format": "binary"}
    assert body["required"] == ["file"]
    schemes = out["components"]["securitySchemes"]
    assert schemes["basic"] == {"type": "http", "scheme": "basic"}
    assert "authorizationCode" in schemes["oauth"]["flows"]


def test_oas30_nullable_and_boolean_exclusive_bounds_are_normalised():
    doc = {"openapi": "3.0.3", "info": {"title": "T", "version": "1"}, "paths": {}, "components": {"schemas": {
        "A": {"type": "object", "properties": {
            "n": {"type": "string", "nullable": True, "enum": ["a", "b"]},
            "k": {"type": "integer", "minimum": 0, "exclusiveMinimum": True},
            "m": {"type": "integer", "maximum": 9, "exclusiveMaximum": False}}}}}}
    props = to_internal(doc)["components"]["schemas"]["A"]["properties"]
    assert props["n"]["type"] == ["string", "null"] and None in props["n"]["enum"]
    assert props["k"] == {"type": "integer", "exclusiveMinimum": 0}
    assert props["m"] == {"type": "integer", "maximum": 9}


def test_oas32_additional_operations_and_query_method():
    doc = {"openapi": "3.2.0", "info": {"title": "T", "version": "1"}, "paths": {"/items": {
        "query": {"responses": {"200": {"description": "ok"}}},
        "additionalOperations": {"PURGE": {"responses": {"204": {"description": "gone"}}}}}}}
    r = load_spec(__import__("yaml").safe_dump(doc), "x.yaml")
    assert r.valid, r.errors
    ops = {(m.upper(), p) for p, m, _op, _item in operations(r.doc)}
    assert ("QUERY", "/items") in ops and ("PURGE", "/items") in ops
    assert "purge" in r.doc["paths"]["/items"][EXTRA_METHODS_KEY]
    removed = dict(doc, paths={"/items": {"query": doc["paths"]["/items"]["query"]}})
    changes = diff(r.doc, load_spec(__import__("yaml").safe_dump(removed), "y.yaml").doc)
    assert any(c["rule"] == "OPERATION_REMOVED" and "PURGE" in c["location"] for c in changes)
