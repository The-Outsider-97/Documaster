"""A small JSON-Schema subset validator for tool arguments (type, enum, required, min/max, items, properties, additionalProperties)."""
from __future__ import annotations

from typing import Any

from ..errors import ValidationFailed

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def validate(value: Any, schema: dict[str, Any], path: str = "arguments") -> Any:
    t = schema.get("type")
    if t:
        py = _TYPES[t]
        if isinstance(value, bool) and t in ("integer", "number"):
            raise ValidationFailed(f"{path} must be {t}")
        if not isinstance(value, py):
            raise ValidationFailed(f"{path} must be {t}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationFailed(f"{path} must be one of {schema['enum']}")
    if t == "string":
        if len(value) > schema.get("maxLength", 20000):
            raise ValidationFailed(f"{path} is too long")
        if len(value) < schema.get("minLength", 0):
            raise ValidationFailed(f"{path} is too short")
    if t in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationFailed(f"{path} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationFailed(f"{path} must be at most {schema['maximum']}")
    if t == "array":
        if len(value) > schema.get("maxItems", 1000):
            raise ValidationFailed(f"{path} has too many items")
        if len(value) < schema.get("minItems", 0):
            raise ValidationFailed(f"{path} needs at least {schema['minItems']} item(s)")
        if "items" in schema:
            for i, item in enumerate(value):
                validate(item, schema["items"], f"{path}[{i}]")
    if t == "object":
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                raise ValidationFailed(f"{path}.{req} is required")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise ValidationFailed(f"{path} has unknown field(s): {', '.join(sorted(extra))}")
        for k, v in value.items():
            if k in props:
                validate(v, props[k], f"{path}.{k}")
    return value
