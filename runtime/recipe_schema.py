"""Explicit JSON Schema for the Goose recipe candidate.

Mirrors the contracts enforced by runtime.recipe_validation.validate and
runtime.learned_install.validate_plan. attach to the propose_recipe MCP tool
inputSchema to reject malformed tool output before it reaches the gate.
No product names, paths, or example hook code.
"""

RECIPE_SCHEMA = {
    "type": "object",
    "required": [
        "agent_identity_name",
        "match_features",
        "hook",
        "observation",
        "fallback",
        "evidence_refs",
    ],
    "properties": {
        "agent_identity_name": {"type": "string", "minLength": 1},
        "match_features": {
            "type": "object",
            "description": "Runtime/behavioural features used for fingerprint matching; currently uses runtime and optional evolves_prior_harness.",
            "properties": {
                "runtime": {"type": "string"},
                "evolves_prior_harness": {"type": "string"},
            },
        },
        "hook": {
            "type": "object",
            "required": [
                "method",
                "restart_required",
                "capabilities",
                "verification",
                "rollback",
                "limitations",
            ],
            "properties": {
                "method": {"type": "string", "minLength": 1},
                "restart_required": {
                    "anyOf": [{"type": "boolean"}, {"const": "unknown"}],
                },
                "workspace": {"type": "string"},
                "capabilities": {"type": "array"},
                "verification": {"type": "string", "minLength": 1},
                "rollback": {"type": "string", "minLength": 1},
                "limitations": {"type": "array"},
            },
        },
        "observation": {"type": "string", "minLength": 1},
        "fallback": {"type": "string", "minLength": 1},
        "evidence_refs": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "string",
                "pattern": "^ev-[0-9]+-[a-f0-9]{10}$",
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "install_plan": {
            "type": "object",
            "required": ["version", "files"],
            "properties": {
                "version": {"const": 1},
                "files": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 16,
                    "items": {
                        "type": "object",
                        "required": ["path", "content", "expected_sha256"],
                        "properties": {
                            "path": {
                                "type": "string",
                                "minLength": 1,
                            },
                            "content": {"type": "string"},
                            "expected_sha256": {
                                "anyOf": [
                                    {"type": "null"},
                                    {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                                ],
                            },
                        },
                    },
                },
            },
        },
        "investigation": {
            "type": "object",
            "description": "Evidence-backed identity and asset summary required by the gate.",
            "properties": {
                "identity_evidence": {"type": "object"},
                "identity": {"type": "object"},
                "assets": {"type": "object"},
            },
        },
    },
    "additionalProperties": True,
}

# Schema guides provider output; runtime validation remains authoritative.
_strings = {"type": "array", "items": {"type": "string"}}
_asset = {"type": "object", "required": ["status"], "properties": {
    "status": {"type": "string", "enum": ["collected", "empty", "failed", "unsupported", "unknown", "not_collected"]},
    "sources": _strings, "uncertainty": _strings, "summary": {"type": "string"}}}
RECIPE_SCHEMA["required"].extend(["confidence", "investigation"])
RECIPE_SCHEMA["properties"]["investigation"] = {"type": "object", "required": ["identity_evidence", "assets"], "properties": {
    "identity_evidence": {"type": "object", "required": ["sources"], "properties": {
        "sources": _strings, "uncertainty": _strings, "summary": {"type": "string"}}},
    "assets": {"type": "object", "required": ["model_gateway", "mcp", "skills", "rules"],
               "properties": {name: _asset for name in ["model_gateway", "mcp", "skills", "rules"]}}}}
for name in ("capabilities", "limitations"):
    RECIPE_SCHEMA["properties"]["hook"]["properties"][name] = _strings
