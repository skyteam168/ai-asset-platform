# 调用授权：五方交集，默认拒绝（architecture.md 4）。
#   allow = Identity ∩ Grant ∩ ReleaseScope ∩ Classification ∩ RiskPolicy
# 输入结构见 docs/specs/policy-input.schema.json；事实（facts）由数据面 PrincipalResolver 预先计算。
#
# 设计：只有当所有检查“正向成立”时才可能放行（checks_ok）；字段缺失或类型不对时检查不成立，结果为 deny。
# reasons 只用于解释拒绝原因，不参与放行判断。
package aip.authz

available_status := {"canary", "active", "deprecated"}

# ---------- 正向检查 ----------

release_ok if input.resource.release_status in available_status

scope_release_ok if input.facts.release_in_scope == true

grant_ok if {
	input.facts.app_grant == true
	input.facts.within_ceiling == true
}

user_grant_ok if input.principal.identity_kind == "app"

user_grant_ok if {
	input.principal.identity_kind == "user"
	object.get(input.facts, "user_grant_required", false) == false
}

user_grant_ok if {
	input.principal.identity_kind == "user"
	input.facts.user_grant == true
}

identity_ok if input.principal.identity_kind == "user"

identity_ok if {
	input.principal.identity_kind == "app"
	input.resource.risk == "read"
}

identity_ok if {
	input.principal.identity_kind == "app"
	input.resource.app_identity_allowed == true
}

classification_ok if {
	is_number(input.principal.clearance)
	is_number(input.resource.classification)
	input.principal.clearance >= input.resource.classification
}

data_scope_ok if {
	is_object(input.facts.scope_check)
	every _, v in input.facts.scope_check {
		v.in_scope == true
	}
}

roles_ok if {
	every r in object.get(input.resource, "required_scopes", []) {
		r in input.principal.roles
	}
}

exposure_ok if input.context.exposure == "internal"

exposure_ok if {
	input.context.exposure in object.get(input.resource, "exposure", [])
	not mcp_danger
}

mcp_danger if {
	input.context.exposure == "mcp"
	input.resource.risk == "danger"
}

checks_ok if {
	release_ok
	scope_release_ok
	grant_ok
	user_grant_ok
	identity_ok
	classification_ok
	data_scope_ok
	roles_ok
	exposure_ok
}

# ---------- 拒绝原因（仅解释） ----------

reasons contains "RELEASE_NOT_AVAILABLE" if not release_ok

reasons contains "OUT_OF_RELEASE_SCOPE" if not scope_release_ok

app_grant_true if input.facts.app_grant == true

within_ceiling_true if input.facts.within_ceiling == true

scope_check_is_object if is_object(input.facts.scope_check)

reasons contains "NO_GRANT" if not app_grant_true

reasons contains "EXCEEDS_CEILING" if {
	app_grant_true
	not within_ceiling_true
}

reasons contains "USER_NOT_GRANTED" if not user_grant_ok

reasons contains "APP_IDENTITY_NOT_ALLOWED" if not identity_ok

reasons contains "CLASSIFICATION" if not classification_ok

reasons contains sprintf("OUT_OF_SCOPE:%s", [k]) if {
	scope_check_is_object
	some k, v in input.facts.scope_check
	not v.in_scope == true
}

reasons contains "SCOPE_CHECK_MISSING" if not scope_check_is_object

reasons contains "MISSING_ROLE" if not roles_ok

reasons contains "EXPOSURE_NOT_ALLOWED" if not exposure_ok

# ---------- 风险策略：write / danger 按 confirm_policy 触发确认或审批 ----------

confirmation_kind := "user_confirm" if input.resource.confirm_policy == "user_confirm"

confirmation_kind := "approval" if input.resource.confirm_policy == "approval"

# ---------- 决策 ----------

default decision := {"result": "deny", "reasons": ["DEFAULT_DENY"]}

decision := {"result": "deny", "reasons": sort(reasons)} if {
	not checks_ok
	count(reasons) > 0
}

decision := {"result": "allow", "reasons": []} if {
	checks_ok
	input.resource.confirm_policy == "auto"
}

decision := {"result": "require_confirmation", "kind": confirmation_kind, "reasons": []} if {
	checks_ok
	confirmation_kind
}
