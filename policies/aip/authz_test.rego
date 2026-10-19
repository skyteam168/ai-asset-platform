package aip.authz_test

import data.aip.authz

base := {
	"principal": {"identity_kind": "user", "clearance": 2, "roles": []},
	"facts": {"release_in_scope": true, "app_grant": true, "within_ceiling": true, "scope_check": {}},
	"resource": {"classification": 2, "risk": "read", "release_status": "active", "confirm_policy": "auto",
		"exposure": ["rest"]},
	"context": {"exposure": "rest"},
}

test_allow if authz.decision.result == "allow" with input as base

test_default_deny_on_empty_input if authz.decision.result == "deny" with input as {}

test_classification_deny if {
	d := authz.decision with input as object.union(base, {"principal": {"identity_kind": "user", "clearance": 1, "roles": []}})
	d.result == "deny"
	"CLASSIFICATION" in d.reasons
}

test_write_requires_confirmation if {
	r := object.union(base.resource, {"risk": "write", "confirm_policy": "user_confirm"})
	d := authz.decision with input as object.union(base, {"resource": r})
	d.result == "require_confirmation"
	d.kind == "user_confirm"
}
