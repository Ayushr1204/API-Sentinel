"""Parse and validate OpenAPI 3.0.x / 3.1.x documents."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import yaml
from openapi_spec_validator import OpenAPIV30SpecValidator, OpenAPIV31SpecValidator

from .config import settings
from .oas import RefError, iter_refs, operations, resolve_pointer

_VERSION_RE = re.compile(r"^3\.(0|1)\.\d+$")
MAX_ERRORS = 50


@dataclass
class ValidationIssue:
    message: str
    location: str = ""


@dataclass
class LoadedSpec:
    valid: bool
    filename: str
    openapi_version: str | None = None
    title: str | None = None
    api_version: str | None = None
    doc: dict | None = None
    errors: list[ValidationIssue] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "filename": self.filename,
            "openapi_version": self.openapi_version,
            "title": self.title,
            "api_version": self.api_version,
            "errors": [e.__dict__ for e in self.errors],
            "stats": self.stats,
        }


MAX_YAML_ALIASES = 100
MAX_NODES = 300_000
MAX_DEPTH = 64


def _check_shape(doc: Any) -> None:
    """Bound the *expanded* tree. YAML aliases share objects, so a tiny file can expand to billions of nodes
    ('billion laughs'); every later walk (ref scan, diff) would pay that cost. Iterative to avoid recursion."""
    stack = [(doc, 1)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES:
            raise ValueError(f"Document expands to more than {MAX_NODES} nodes; refusing to process it")
        if depth > MAX_DEPTH:
            raise ValueError(f"Document nests deeper than {MAX_DEPTH} levels; refusing to process it")
        if isinstance(node, dict):
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)


def _parse(text: str) -> Any:
    stripped = text.lstrip()
    try:
        if stripped.startswith("{"):
            try:
                doc = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON: {exc}") from exc
        else:
            try:
                aliases = sum(isinstance(ev, yaml.AliasEvent) for ev in yaml.parse(text, Loader=yaml.SafeLoader))
                if aliases > MAX_YAML_ALIASES:
                    raise ValueError(f"YAML uses {aliases} aliases (limit {MAX_YAML_ALIASES}); refusing to expand it")
                # safe_load only: no arbitrary Python object construction from uploads.
                doc = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise ValueError(f"Invalid YAML: {exc}") from exc
    except RecursionError:
        raise ValueError(f"Document nests too deeply to parse (limit {MAX_DEPTH} levels)") from None
    _check_shape(doc)
    return doc


def load_spec(raw: bytes | str, filename: str = "spec") -> LoadedSpec:
    result = LoadedSpec(valid=False, filename=filename)
    size = len(raw.encode() if isinstance(raw, str) else raw)
    if size == 0:
        result.errors.append(ValidationIssue("File is empty"))
        return result
    if size > settings.max_spec_bytes:
        result.errors.append(ValidationIssue(
            f"File is {size} bytes; the limit is {settings.max_spec_bytes} bytes"))
        return result
    try:
        text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    except UnicodeDecodeError:
        result.errors.append(ValidationIssue("File is not UTF-8 text"))
        return result
    try:
        doc = _parse(text)
    except ValueError as exc:
        result.errors.append(ValidationIssue(str(exc)))
        return result
    if not isinstance(doc, dict):
        result.errors.append(ValidationIssue("Top level of the document must be an object"))
        return result

    # Scope guard: only OpenAPI 3.0 / 3.1.
    if "swagger" in doc:
        result.errors.append(ValidationIssue(
            f"Swagger {doc.get('swagger')} is not supported. Convert to OpenAPI 3.0/3.1 first."))
        return result
    if "asyncapi" in doc:
        result.errors.append(ValidationIssue("AsyncAPI documents are out of scope"))
        return result
    version = str(doc.get("openapi", ""))
    result.openapi_version = version or None
    if not _VERSION_RE.match(version):
        result.errors.append(ValidationIssue(
            f"Unsupported or missing 'openapi' version '{version}'. Expected 3.0.x or 3.1.x.", "openapi"))
        return result
    info = doc.get("info") if isinstance(doc.get("info"), dict) else {}
    result.title = info.get("title")
    result.api_version = str(info.get("version")) if info.get("version") is not None else None

    # Reference check before meta-schema validation: reject remote refs (no network fetches
    # triggered by uploads) and report dangling refs with their location.
    for loc, ref in iter_refs(doc):
        if not ref.startswith("#"):
            result.errors.append(ValidationIssue(
                f"External $ref '{ref}' is not allowed; bundle the spec into one file", loc))
            continue
        try:
            resolve_pointer(doc, ref)
        except RefError as exc:
            result.errors.append(ValidationIssue(str(exc), loc))
    if result.errors:
        return result

    validator_cls = OpenAPIV31SpecValidator if version.startswith("3.1") else OpenAPIV30SpecValidator
    try:
        for err in validator_cls(doc).iter_errors():
            loc = "/".join(str(p) for p in err.path) if getattr(err, "path", None) else ""
            result.errors.append(ValidationIssue(err.message, loc))
            if len(result.errors) >= MAX_ERRORS:
                break
    except Exception as exc:  # validator internals can raise on exotic documents
        result.errors.append(ValidationIssue(f"Validator failed: {exc}"))

    if result.errors:
        return result

    try:
        ops = list(operations(doc))
    except RefError as exc:
        result.errors.append(ValidationIssue(str(exc)))
        return result
    result.valid = True
    result.doc = doc
    result.stats = {
        "paths": len(doc.get("paths") or {}),
        "operations": len(ops),
        "schemas": len(((doc.get("components") or {}).get("schemas")) or {}),
    }
    return result
