// 与 docs/api/control-plane.openapi.yaml 对应的前端类型（只列控制台用到的字段）
export type Role = "platform_admin" | "asset_developer" | "data_owner" | "business_owner" | "company_admin" | "auditor";

export interface Me {
  id: string;
  login_name: string | null;
  display_name: string;
  home_org_id: string;
  effective_clearance: number;
  roles: { role: Role; scope_org_id: string }[];
}

export interface OrgUnit {
  id: string;
  type: "group" | "company" | "department" | "team";
  code: string;
  name: string;
  parent_id: string | null;
  company_id: string;
  default_clearance: number | null;
  status: "active" | "disabled";
}

export interface User {
  id: string;
  login_name: string | null;
  display_name: string;
  employee_no: string | null;
  home_org_id: string;
  login_enabled: boolean;
  status: string;
  effective_clearance: number;
  totp_provisioning_uri?: string | null;
}

export interface Consumer {
  id: string;
  client_id: string;
  company_id: string;
  clearance_cap: number;
  bind_policy: "strict" | "by_employee_no" | "app_only";
  status: string;
}

export interface Binding {
  id: string;
  consumer_id: string;
  external_subject: string;
  user_id: string;
  expires_at: string;
}

export interface Connection {
  id: string;
  company_id: string;
  system_key: string;
  env: string;
  base_url: string;
  scheme: "https" | "http";
  tls_mode: "system_ca" | "custom_ca" | "pinned" | "insecure" | null;
  pins: { kind: "cert" | "spki"; sha256: string }[];
  transport_exception_reason: string | null;
  transport_exception_expires_at: string | null;
  transport_approved_by: string[];
  tls_last_seen_fingerprint: string | null;
  status: "pending_approval" | "active" | "disabled";
}

export interface ConnectionTest {
  connection_id: string;
  resolved_ip: string | null;
  allowlisted: boolean | null;
  tls_version: string | null;
  chain: { subject: string; issuer: string; not_before: string; not_after: string; self_signed: boolean; sans: string[] }[];
  fingerprint_cert_sha256: string | null;
  fingerprint_spki_sha256: string | null;
  hostname_match: boolean | null;
  verdict: "pass" | "fail";
  error_code: string | null;
  detail: string | null;
}

export interface Asset {
  id: string;
  type: string;
  name: string;
  owner_org_id: string;
  classification: number;
}

export interface VersionSummary {
  id: string;
  semver: string;
  state: string;
  content_hash: string;
  created_at: string;
}

export interface TestCase {
  id: string;
  name: string;
  kind: "functional" | "permission";
  critical: boolean;
  args: Record<string, unknown>;
  principal: Record<string, unknown>;
  expect: Record<string, unknown>;
}

export interface VersionDetail extends VersionSummary {
  asset_id: string;
  spec: Record<string, unknown>;
  test_cases: TestCase[];
  acceptance: { template: Record<string, unknown>; signed_by: string | null; signed_at: string | null } | null;
}

export interface GateRecord {
  gate: string;
  status: "passed" | "failed" | "pending";
  evidence: Record<string, unknown>;
  created_at: string;
}

export interface Approval {
  id: string;
  kind: string;
  subject_id: string;
  subject: string;
  approver_role: string;
  required_approvals: number;
  due_at: string;
  payload: Record<string, unknown>;
}

export interface Ceiling {
  id: string;
  company_id: string;
  asset_ids: string[];
  max_classification: number;
  status: string;
}

export interface Grant {
  id: string;
  asset_id: string;
  grantee_type: string;
  grantee_id: string;
  env: string;
  expires_at: string;
  status: string;
}

export interface AuditRecord {
  shard_id: string;
  seq: number;
  occurred_at: string;
  company_id: string | null;
  kind: string;
  record: Record<string, unknown>;
  hash: string;
}
