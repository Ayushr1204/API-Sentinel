"""Generate the Bookings API (v1 and v2) in Swagger 2.0, OpenAPI 3.0.3, 3.1.0 and 3.2.0.

Each format is written the way a person would write it in that format (x-nullable, nullable, type arrays,
boolean vs numeric exclusiveMinimum, basePath vs servers), describing exactly the same contract.
Usage: python tests/fixtures/gen_bookings.py tests/fixtures/bookings  -> {v1,v2}/{swagger20,oas30,oas31,oas32}.yaml
The bookings-* samples in backend/samples were copied from this output.
"""
import copy
import sys
from pathlib import Path

import yaml

STATUSES = ["pending", "confirmed", "cancelled"]


def contract(v2: bool) -> dict:
    """Format-neutral description of the API."""
    statuses = STATUSES + (["no_show"] if v2 else [])
    booking_props = {
        "id": {"type": "string"},
        "guest": {"type": "string", "maxLength": 80},
        "nights": {"type": "integer", "gt": 0},
        "status": {"type": "string", "enum": statuses},
        "checkIn": {"type": "string", "format": "date"},
        "notes": {"type": "string", "nullable": True},
    }
    if v2:
        booking_props["currency"] = {"type": "string", "pattern": "^[A-Z]{3}$"}
    new_props = {
        "guest": {"type": "string", "maxLength": 50 if v2 else 80},
        "nights": {"type": "integer", "gt": 0},
        "checkIn": {"type": "string", "format": "date"},
        "notes": {"type": "string", "nullable": True},
    }
    query = [
        {"name": "status", "in": "query", "schema": {"type": "string", "enum": statuses}},
        {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}},
        {"name": "tags", "in": "query", "array_csv": True, "schema": {"type": "array", "items": {"type": "string"}}},
    ]
    if v2:
        query.append({"name": "propertyId", "in": "query", "required": True, "schema": {"type": "string"}})
    return {"version": "2.0.0" if v2 else "1.0.0", "booking": booking_props, "new": new_props, "query": query,
            "delete": v2}


def schema_for(fmt: str, s: dict) -> dict:
    s = copy.deepcopy(s)
    if s.pop("nullable", False):
        if fmt == "swagger20":
            s["x-nullable"] = True
        elif fmt == "oas30":
            s["nullable"] = True
        else:
            s["type"] = [s["type"], "null"]
    if "gt" in s:
        bound = s.pop("gt")
        if fmt in ("swagger20", "oas30"):
            s["minimum"], s["exclusiveMinimum"] = bound, True
        else:
            s["exclusiveMinimum"] = bound
    return s


def obj(fmt: str, props: dict, required: list) -> dict:
    return {"type": "object", "required": required, "properties": {k: schema_for(fmt, v) for k, v in props.items()}}


def build(fmt: str, c: dict) -> dict:
    ref = "#/definitions/" if fmt == "swagger20" else "#/components/schemas/"
    booking = obj(fmt, c["booking"], ["id", "guest", "nights", "status"])
    new = obj(fmt, c["new"], ["guest", "nights", "checkIn"])
    b_ref, n_ref = {"$ref": ref + "Booking"}, {"$ref": ref + "NewBooking"}
    info = {"title": "Bookings API", "version": c["version"],
            "description": "Room bookings for the StayFlow property platform (sample)."}

    if fmt == "swagger20":
        def q(p):
            out = {"name": p["name"], "in": "query", "required": p.get("required", False)}
            out.update(p["schema"] if not p.get("array_csv") else
                       {"type": "array", "items": {"type": "string"}, "collectionFormat": "csv"})
            if not out["required"]:
                out.pop("required")
            return out
        paths = {
            "/bookings": {
                "get": {"operationId": "listBookings", "parameters": [q(p) for p in c["query"]],
                        "responses": {"200": {"description": "Bookings", "schema": {"type": "array", "items": b_ref},
                                              "headers": {"X-Total-Count": {"type": "integer"}}}}},
                "post": {"operationId": "createBooking",
                         "parameters": [{"name": "booking", "in": "body", "required": True, "schema": n_ref}],
                         "responses": {"201": {"description": "Created", "schema": b_ref},
                                       "400": {"description": "Invalid booking"}}}},
            "/bookings/{bookingId}": {
                "parameters": [{"name": "bookingId", "in": "path", "required": True, "type": "string"}],
                "get": {"operationId": "getBooking",
                        "responses": {"200": {"description": "Booking", "schema": b_ref},
                                      "404": {"description": "Not found"}}}},
        }
        if c["delete"]:
            paths["/bookings/{bookingId}"]["delete"] = {"operationId": "cancelBooking",
                                                       "responses": {"204": {"description": "Cancelled"}}}
        return {"swagger": "2.0", "info": info, "host": "api.stayflow.example", "basePath": "/v1",
                "schemes": ["https"], "consumes": ["application/json"], "produces": ["application/json"],
                "securityDefinitions": {"apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
                "security": [{"apiKey": []}], "paths": paths,
                "definitions": {"Booking": booking, "NewBooking": new}}

    def json_(schema):
        return {"application/json": {"schema": schema}}

    def q3(p):
        out = {"name": p["name"], "in": "query"}
        if p.get("required"):
            out["required"] = True
        if p.get("array_csv"):
            out.update({"style": "form", "explode": False})
        out["schema"] = schema_for(fmt, p["schema"])
        return out
    paths = {
        "/bookings": {
            "get": {"operationId": "listBookings", "parameters": [q3(p) for p in c["query"]],
                    "responses": {"200": {"description": "Bookings",
                                          "headers": {"X-Total-Count": {"schema": {"type": "integer"}}},
                                          "content": json_({"type": "array", "items": b_ref})}}},
            "post": {"operationId": "createBooking",
                     "requestBody": {"required": True, "content": json_(n_ref)},
                     "responses": {"201": {"description": "Created", "content": json_(b_ref)},
                                   "400": {"description": "Invalid booking"}}}},
        "/bookings/{bookingId}": {
            "parameters": [{"name": "bookingId", "in": "path", "required": True, "schema": {"type": "string"}}],
            "get": {"operationId": "getBooking",
                    "responses": {"200": {"description": "Booking", "content": json_(b_ref)},
                                  "404": {"description": "Not found"}}}},
    }
    if c["delete"]:
        paths["/bookings/{bookingId}"]["delete"] = {"operationId": "cancelBooking",
                                                   "responses": {"204": {"description": "Cancelled"}}}
    version = {"oas30": "3.0.3", "oas31": "3.1.0", "oas32": "3.2.0"}[fmt]
    return {"openapi": version, "info": info, "servers": [{"url": "https://api.stayflow.example/v1"}],
            "security": [{"apiKey": []}], "paths": paths,
            "components": {"schemas": {"Booking": booking, "NewBooking": new},
                           "securitySchemes": {"apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}}}


def main(out: Path) -> None:
    for tag, v2 in (("v1", False), ("v2", True)):
        for fmt in ("swagger20", "oas30", "oas31", "oas32"):
            path = out / tag / f"{fmt}.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(build(fmt, contract(v2)), sort_keys=False, allow_unicode=True),
                            encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
