"""Builds the `inspect.Signature` FastAPI binds a mounted extension route's dispatch
callable to — path and query parameters only.

A pure function, `RouteRuntime dict -> inspect.Signature`: no FastAPI app instance, no
request handling, so it's directly unit-testable (assert the right `Path`/`Query`
parameters appear for a given route) without spinning up FastAPI at all. The request
body is deliberately never part of this signature: bodies are validated against a raw
JSON Schema, not a compiled Pydantic model, so it stays a `Request`-level concern, read
once and validated by extensions/body_validation.py — this module never binds a native
`Body(...)` parameter. The route's auth (`ABACPermissionCheck` / `X-Internal-Key`)
likewise stays wired the existing, already-tested way — as a `dependencies=[...]` entry
on `add_api_route`, not folded into this signature. (`extensions/runtime.py` separately
passes `openapi_extra` to `add_api_route` to document the body's JSON Schema in
/openapi.json purely for documentation — that never touches this signature either.)
"""

import inspect
import re
from typing import Any, Dict, Optional, Type

from fastapi import Path, Query, Request

_PATH_PARAM_RE = re.compile(r"{([^}]+)}")

_QUERY_PARAM_TYPES: Dict[str, Type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def build_signature(route: Dict[str, Any]) -> inspect.Signature:
    """Returns `request: Request` (the dispatcher only ever reads the raw `Request`
    itself; these bound values exist purely so FastAPI validates/coerces/documents them
    like it would a real route) plus one keyword-only parameter per `{name}` placeholder
    in `route['path']` and one per `route['query_params']` entry."""
    parameters = [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request)]
    used_names = {"request"}

    for name in _PATH_PARAM_RE.findall(route.get("path") or ""):
        if name in used_names:
            continue
        used_names.add(name)
        parameters.append(
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=str, default=Path(...))
        )

    for spec in route.get("query_params") or []:
        if spec["name"] in used_names:
            continue
        used_names.add(spec["name"])
        py_type = _QUERY_PARAM_TYPES.get(spec.get("type") or "string", str)
        required = spec.get("required", True)
        description = spec.get("description")
        if required:
            annotation: Any = py_type
            default = Query(..., description=description)
        else:
            annotation = Optional[py_type]
            default = Query(None, description=description)
        parameters.append(
            inspect.Parameter(spec["name"], inspect.Parameter.KEYWORD_ONLY, annotation=annotation, default=default)
        )

    return inspect.Signature(parameters)
