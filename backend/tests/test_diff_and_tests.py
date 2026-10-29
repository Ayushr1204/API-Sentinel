"""Rule-level tests: each builds a minimal base/candidate pair and checks the change AND the executed test."""
import pytest

from app.diff import BREAKING, NON_BREAKING, POTENTIAL, diff_specs
from app.recommend import recommend
from app.runner import run_tests
from app.testgen import generate_tests
from conftest import json_op, make_spec


def analyse(base, cand):
    changes = diff_specs(base, cand)
    tests = generate_tests(base, cand, changes)
    results = run_tests(base, cand, tests)
    return changes, tests, {r.test_id: r for r in results}, recommend(changes, tests, results)


def one(changes, rule):
    found = [c for c in changes if c.rule == rule]
    assert found, f"{rule} not in {[c.rule for c in changes]}"
    return found[0]


def result_for(change, tests, results):
    t = next(t for t in tests if change.id in t.change_ids)
    return t, results[t.id]


OBJ = {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}}


def test_identical_specs_proceed():
    base = make_spec(paths={"/a": {"get": json_op(OBJ)}})
    changes, tests, _, rec = analyse(base, base)
    assert changes == [] and rec["decision"] == "PROCEED"


def test_response_property_became_optional_confirmed():
    cand_schema = {**OBJ, "required": []}
    changes, tests, res, rec = analyse(make_spec(paths={"/a": {"get": json_op(OBJ)}}),
                                       make_spec(paths={"/a": {"get": json_op(cand_schema)}}))
    c = one(changes, "PROPERTY_BECAME_OPTIONAL")
    assert c.severity == BREAKING and c.direction == "response"
    t, r = result_for(c, tests, res)
    assert "id" not in t.payload and r.status == "failed" and r.verdict == "confirms_prediction"
    assert rec["decision"] == "BLOCK"


def test_request_required_property_added_confirmed():
    req_b = {"type": "object", "properties": {"a": {"type": "string"}}}
    req_c = {"type": "object", "required": ["b"], "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    changes, tests, res, rec = analyse(make_spec(paths={"/a": {"post": json_op(OBJ, request=req_b)}}),
                                       make_spec(paths={"/a": {"post": json_op(OBJ, request=req_c)}}))
    c = one(changes, "REQUIRED_PROPERTY_ADDED")
    t, r = result_for(c, tests, res)
    assert r.status == "failed" and "'b' is a required property" in r.errors[0]["message"]
    assert rec["decision"] == "BLOCK"


def test_optional_request_property_added_is_safe():
    req_b = {"type": "object", "properties": {"a": {"type": "string"}}}
    req_c = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    changes, tests, res, rec = analyse(make_spec(paths={"/a": {"post": json_op(OBJ, request=req_b)}}),
                                       make_spec(paths={"/a": {"post": json_op(OBJ, request=req_c)}}))
    c = one(changes, "PROPERTY_ADDED")
    assert c.severity == NON_BREAKING
    _, r = result_for(c, tests, res)
    assert r.status == "passed" and rec["decision"] == "PROCEED"


def test_request_enum_value_removed_uses_removed_value():
    b = {"type": "string", "enum": ["a", "b", "c"]}
    c = {"type": "string", "enum": ["a", "b"]}
    params = lambda s: [{"name": "mode", "in": "query", "required": True, "schema": s}]
    changes, tests, res, _ = analyse(make_spec(paths={"/x": {"get": json_op(OBJ, params=params(b))}}),
                                     make_spec(paths={"/x": {"get": json_op(OBJ, params=params(c))}}))
    ch = one(changes, "ENUM_VALUE_REMOVED")
    t, r = result_for(ch, tests, res)
    assert t.payload["params"]["query:mode"] == "c" and r.status == "failed"


def test_response_enum_value_added_is_potential_and_risk_confirmed():
    b = {"type": "object", "required": ["s"], "properties": {"s": {"type": "string", "enum": ["x"]}}}
    c = {"type": "object", "required": ["s"], "properties": {"s": {"type": "string", "enum": ["x", "y"]}}}
    changes, tests, res, rec = analyse(make_spec(paths={"/a": {"get": json_op(b)}}),
                                       make_spec(paths={"/a": {"get": json_op(c)}}))
    ch = one(changes, "ENUM_VALUE_ADDED")
    assert ch.severity == POTENTIAL
    t, r = result_for(ch, tests, res)
    assert t.payload == {"s": "y"} and r.verdict == "risk_confirmed"
    assert rec["decision"] == "HUMAN_REVIEW"


@pytest.mark.parametrize("kw,bv,cv,direction,severity,fails", [
    ("maxLength", 100, 50, "request", BREAKING, True),
    ("maxLength", 50, 100, "request", NON_BREAKING, False),
    ("minimum", 1, 5, "request", BREAKING, True),
    ("maximum", 100, 200, "response", POTENTIAL, True),
])
def test_constraint_boundaries(kw, bv, cv, direction, severity, fails):
    t = "string" if "Length" in kw else "integer"
    mk = lambda v: {"type": "object", "required": ["f"], "properties": {"f": {"type": t, kw: v}}}
    if direction == "request":
        base = make_spec(paths={"/a": {"post": json_op(OBJ, request=mk(bv))}})
        cand = make_spec(paths={"/a": {"post": json_op(OBJ, request=mk(cv))}})
    else:
        base = make_spec(paths={"/a": {"get": json_op(mk(bv))}})
        cand = make_spec(paths={"/a": {"get": json_op(mk(cv))}})
    changes, tests, res, _ = analyse(base, cand)
    ch = next(c for c in changes if c.rule.startswith("CONSTRAINT_"))
    assert ch.severity == severity
    _, r = result_for(ch, tests, res)
    assert (r.status == "failed") == fails


def test_type_change_and_widening():
    mk = lambda typ: {"type": "object", "required": ["n"], "properties": {"n": {"type": typ}}}
    changes, tests, res, _ = analyse(make_spec(paths={"/a": {"get": json_op(mk("integer"))}}),
                                     make_spec(paths={"/a": {"get": json_op(mk("string"))}}))
    ch = one(changes, "TYPE_CHANGED")
    assert ch.severity == BREAKING and result_for(ch, tests, res)[1].status == "failed"
    changes, *_ = analyse(make_spec(paths={"/a": {"post": json_op(OBJ, request=mk("integer"))}}),
                          make_spec(paths={"/a": {"post": json_op(OBJ, request=mk("number"))}}))
    assert one(changes, "TYPE_WIDENED").severity == NON_BREAKING


def test_oas31_null_type_added_in_response():
    mk = lambda typ: {"type": "object", "required": ["u"], "properties": {"u": {"type": typ}}}
    changes, tests, res, _ = analyse(make_spec("3.1.0", paths={"/a": {"get": json_op(mk("string"))}}),
                                     make_spec("3.1.0", paths={"/a": {"get": json_op(mk(["string", "null"]))}}))
    ch = one(changes, "NULLABLE_ADDED")
    t, r = result_for(ch, tests, res)
    assert ch.severity == POTENTIAL and t.payload == {"u": None} and r.status == "failed"


def test_oas30_nullable_keyword():
    mk = lambda n: {"type": "object", "required": ["u"], "properties": {"u": {"type": "string", "nullable": n}}}
    changes, *_ = analyse(make_spec(paths={"/a": {"get": json_op(mk(False))}}),
                          make_spec(paths={"/a": {"get": json_op(mk(True))}}))
    assert one(changes, "NULLABLE_ADDED")


def test_operation_removed_and_path_rename_not_flagged():
    base = make_spec(paths={"/p/{a}": {"parameters": [{"name": "a", "in": "path", "required": True,
                                                       "schema": {"type": "integer"}}],
                                       "get": json_op(OBJ), "delete": {"responses": {"204": {"description": "x"}}}}})
    cand = make_spec(paths={"/p/{b}": {"parameters": [{"name": "b", "in": "path", "required": True,
                                                       "schema": {"type": "integer"}}],
                                       "get": json_op(OBJ)}})
    changes, tests, res, rec = analyse(base, cand)
    rules = {c.rule for c in changes}
    assert "OPERATION_REMOVED" in rules and "PATH_PARAM_RENAMED" in rules
    assert "REQUIRED_PARAM_ADDED" not in rules and "PARAM_REMOVED" not in rules
    assert result_for(one(changes, "OPERATION_REMOVED"), tests, res)[1].status == "failed"


def test_security_added():
    base = make_spec(paths={"/a": {"get": json_op(OBJ)}})
    cand = make_spec(paths={"/a": {"get": {**json_op(OBJ), "security": [{"k": []}]}}})
    cand["components"] = {"securitySchemes": {"k": {"type": "apiKey", "in": "header", "name": "K"}}}
    changes, tests, res, rec = analyse(base, cand)
    ch = one(changes, "SECURITY_ADDED")
    assert result_for(ch, tests, res)[1].status == "failed" and rec["decision"] == "BLOCK"


def test_read_only_property_not_required_in_requests():
    pet = {"type": "object", "required": ["id", "name"],
           "properties": {"id": {"type": "integer", "readOnly": True}, "name": {"type": "string"},
                          "tag": {"type": "string"}}}
    pet_c = {**pet, "properties": {**pet["properties"], "tag": {"type": "string", "maxLength": 3}}}
    changes, tests, res, _ = analyse(make_spec(paths={"/a": {"post": json_op(OBJ, request=pet)}}),
                                     make_spec(paths={"/a": {"post": json_op(OBJ, request=pet_c)}}))
    ch = one(changes, "CONSTRAINT_TIGHTENED")
    t, r = result_for(ch, tests, res)
    assert "id" not in t.payload, "readOnly fields must not be sent by clients"
    assert r.status == "failed" and r.collateral == []


def test_recursive_schema_terminates():
    node = {"type": "object", "properties": {"child": {"$ref": "#/components/schemas/Node"},
                                             "v": {"type": "string"}}}
    node_c = {"type": "object", "properties": {"child": {"$ref": "#/components/schemas/Node"},
                                               "v": {"type": "integer"}}}
    ref = {"$ref": "#/components/schemas/Node"}
    changes, tests, res, _ = analyse(make_spec(paths={"/a": {"get": json_op(ref)}}, schemas={"Node": node}),
                                     make_spec(paths={"/a": {"get": json_op(ref)}}, schemas={"Node": node_c}))
    assert any(c.rule == "TYPE_CHANGED" and c.component == "Node" for c in changes)


def test_isolation_no_collateral_on_sample():
    from pathlib import Path
    from app.spec_loader import load_spec
    d = Path(__file__).resolve().parents[1] / "samples" / "petstore-breaking"
    b, c = load_spec((d / "base.yaml").read_bytes()), load_spec((d / "candidate.yaml").read_bytes())
    changes, tests, res, rec = analyse(b.doc, c.doc)
    assert all(not r.collateral for r in res.values())
    assert all(r.status != "error" for r in res.values())
    assert rec["decision"] == "BLOCK"
