import { apiClient } from './client'
import type { ApiResponse } from './types'

/** QRAC2 account-cluster contract version accepted by the browser API. */
export const QRAC2_CONTRACT_VERSION = 1 as const
export const IDEMPOTENCY_HEADER = 'Idempotency-Key'
export const REVISION_HEADER = 'If-Match'

export type BrowserAccountStatus =
  | 'opening'
  | 'presenting'
  | 'refreshing'
  | 'verifying'
  | 'challenge'
  | 'unknown'
  | 'saving'
  | 'saved'
  | 'dormant'
  | 'expired'
  | 'closed'
  | 'error'

export type BrowserAuthEvidence = 'unknown' | 'valid' | 'invalid'
export type BrowserEvidenceSource = 'rule_verified' | 'manual_fallback'
export type BrowserLoginPurpose = 'login' | 'execution'

export interface BrowserExternalIdentity {
  provider: string
  subject: string
  display_name?: string | null
}

export interface BrowserAccountAuthEvidence {
  kind: BrowserAuthEvidence
  reason_code?: string | null
}

export interface BrowserAccount {
  id: string
  workspace_id: string
  site: string
  label: string
  node_id: string | null
  profile_id: string | null
  profile_version: number | null
  profile_manifest_id: string | null
  runtime_bundle_id: string | null
  runtime_bundle_version: string | null
  login_rule_id: string | null
  login_rule_version: string | null
  auth_required: boolean
  platform_identity: BrowserExternalIdentity | null
  auth_evidence: BrowserAuthEvidence | BrowserAccountAuthEvidence
  evidence_source: BrowserEvidenceSource | null
  evidence_observed_at: string | null
  manual_confirmed_by: string | null
  status: BrowserAccountStatus
  revision: number
  paused: boolean
  status_reason_code: string | null
  created_at: string
  updated_at: string
}

export interface BrowserAccountList {
  items: BrowserAccount[]
  next_cursor: string | null
}

export interface BrowserAccountCreateRequest {
  workspace_id: string
  site: string
  label: string
  node_id?: string
  runtime_bundle_id?: string
  login_rule_id?: string
  login_rule_version?: string
}

export interface BrowserLoginSessionCreateRequest {
  purpose: BrowserLoginPurpose
  execution_id?: string
  source_binding_revision_id?: string
  expected_revision: number
}

export interface BrowserLoginSession {
  id: string
  workspace_id: string
  account_id: string
  instance_id: string | null
  node_id: string | null
  node_boot_id: string | null
  lease_id: string | null
  epoch: number
  revision: number
  profile_id: string | null
  profile_version: number | null
  profile_state: 'new' | 'uncommitted' | 'committed'
  login_rule_id: string | null
  login_rule_version: string | null
  tab_id: string | null
  frame_id: string | null
  document_id: string | null
  origin: string | null
  view_generation: number
  purpose: BrowserLoginPurpose
  execution_id: string | null
  command_id: string | null
  status: BrowserAccountStatus
  expires_at: string | null
  closed_at: string | null
  created_at: string
  updated_at: string
}

export interface BrowserAccountOperationRequest {
  account_ref: {
    workspace_id: string
    account_id: string
    source_binding_revision_id?: string
  }
  expected_revision: number
  operation: 'auth_required' | 'suspend' | 'resume' | 'migrate'
  auth_required?: boolean
  target_node_id?: string
  isolation_evidence_ref?: string
}

export interface BrowserAccountOperationResponse {
  contract_version: typeof QRAC2_CONTRACT_VERSION
  http_status: 200
  account_ref: BrowserAccountOperationRequest['account_ref']
  operation: BrowserAccountOperationRequest['operation']
  revision: number
  status: BrowserAccountStatus
  auth_required: boolean
  paused: boolean
}

const accountPath = (workspaceId: string, accountId?: string) => {
  const base = `/workspaces/${encodeURIComponent(workspaceId)}/browser-accounts`
  return accountId ? `${base}/${encodeURIComponent(accountId)}` : base
}

const sessionPath = (workspaceId: string, accountId: string, sessionId?: string) => {
  const base = `${accountPath(workspaceId, accountId)}/login-sessions`
  return sessionId ? `${base}/${encodeURIComponent(sessionId)}` : base
}

const idempotencyHeaders = (key?: string) =>
  key ? { [IDEMPOTENCY_HEADER]: key } : undefined

export const listBrowserAccounts = (
  workspaceId: string,
  options: { limit?: number; cursor?: string } = {},
) =>
  apiClient
    .get<ApiResponse<BrowserAccountList>>(accountPath(workspaceId), {
      params: {
        limit: Math.min(Math.max(options.limit ?? 50, 1), 200),
        ...(options.cursor ? { cursor: options.cursor } : {}),
      },
    })
    .then((response) => response.data.data)

export const createBrowserAccount = (
  workspaceId: string,
  data: BrowserAccountCreateRequest,
  idempotencyKey?: string,
) =>
  apiClient
    .post<ApiResponse<BrowserAccount>>(accountPath(workspaceId), data, {
      headers: idempotencyHeaders(idempotencyKey),
    })
    .then((response) => response.data.data)

export const getBrowserAccount = (workspaceId: string, accountId: string) =>
  apiClient
    .get<ApiResponse<BrowserAccount>>(accountPath(workspaceId, accountId))
    .then((response) => response.data.data)

export const createBrowserLoginSession = (
  workspaceId: string,
  accountId: string,
  data: BrowserLoginSessionCreateRequest,
  idempotencyKey?: string,
) =>
  apiClient
    .post<ApiResponse<BrowserLoginSession>>(sessionPath(workspaceId, accountId), data, {
      headers: {
        ...(idempotencyHeaders(idempotencyKey) ?? {}),
        [REVISION_HEADER]: String(data.expected_revision),
      },
    })
    .then((response) => response.data.data)

export const getBrowserLoginSession = (
  workspaceId: string,
  accountId: string,
  sessionId: string,
) =>
  apiClient
    .get<ApiResponse<BrowserLoginSession>>(sessionPath(workspaceId, accountId, sessionId))
    .then((response) => response.data.data)

export const performBrowserSessionAction = (
  workspaceId: string,
  accountId: string,
  sessionId: string,
  action: 'view' | 'takeover' | 'confirm' | 'close',
  expectedSessionRevision: number,
  idempotencyKey?: string,
) =>
  apiClient
    .post<ApiResponse<BrowserLoginSession>>(`${sessionPath(workspaceId, accountId, sessionId)}/${action}`, undefined, {
      headers: {
        ...(idempotencyHeaders(idempotencyKey) ?? {}),
        [REVISION_HEADER]: String(expectedSessionRevision),
      },
    })
    .then((response) => response.data.data)

export const operateBrowserAccount = (
  workspaceId: string,
  accountId: string,
  data: BrowserAccountOperationRequest,
  idempotencyKey?: string,
) =>
  apiClient
    .post<ApiResponse<BrowserAccountOperationResponse>>(
      `${accountPath(workspaceId, accountId)}/${data.operation === 'auth_required' ? 'auth-required' : data.operation === 'migrate' ? 'migration' : data.operation}`,
      data,
      {
        headers: {
          ...(idempotencyHeaders(idempotencyKey) ?? {}),
          [REVISION_HEADER]: String(data.expected_revision),
        },
      },
    )
    .then((response) => response.data.data)
export interface PortalTicketIssueRequest {
  contract_version: typeof QRAC2_CONTRACT_VERSION
  first_entry: 'initial'
  account_ref: {
    workspace_id: string
    account_id: string
  }
  session_id: string
  expected_session_revision: number
  csrf_token: string
}

export interface PortalTicketIssued {
  contract_version: typeof QRAC2_CONTRACT_VERSION
  status: 'issued'
  account_ref: PortalTicketIssueRequest['account_ref']
  session_id: string
  session_revision: number
  ticket_id: string
  ticket: string
  csrf_token: string
  issued_at: string
  expires_at: string
  hard_expires_at: string
  http_status: 200
}

export interface PortalTicketRedeemRequest {
  contract_version: typeof QRAC2_CONTRACT_VERSION
  first_entry: 'initial' | 'reconnect'
  account_ref: PortalTicketIssueRequest['account_ref']
  session_id: string
  expected_session_revision: number
  ticket_id: string
  ticket: string
  csrf_token: string
}

export interface PortalTicketGrant {
  contract_version: typeof QRAC2_CONTRACT_VERSION
  status: 'granted'
  http_status: 200
  workspace_id: string
  account_id: string
  ticket_id: string
  session_revision: number
  session_id: string
  issued_at: string
  expires_at: string
  hard_expires_at: string
  cookie_name: string
  websocket_path: string
  cookie_http_only: true
  cookie_secure: true
  same_site: 'strict' | 'lax'
  origin_required: true
  csrf_bound: true
}

export const issueBrowserPortalTicket = (
  workspaceId: string,
  accountId: string,
  sessionId: string,
  expectedSessionRevision: number,
  csrfToken: string,
) =>
  apiClient
    .post<ApiResponse<PortalTicketIssued>>(
      `${sessionPath(workspaceId, accountId, sessionId)}/portal-ticket/issue`,
      {
        contract_version: QRAC2_CONTRACT_VERSION,
        first_entry: 'initial',
        account_ref: { workspace_id: workspaceId, account_id: accountId },
        session_id: sessionId,
        expected_session_revision: expectedSessionRevision,
        csrf_token: csrfToken,
      } satisfies PortalTicketIssueRequest,
      {
        headers: { [REVISION_HEADER]: String(expectedSessionRevision) },
        withCredentials: true,
      },
    )
    .then((response) => response.data.data)

export const redeemBrowserPortalTicket = (
  workspaceId: string,
  accountId: string,
  sessionId: string,
  data: Omit<PortalTicketRedeemRequest, 'account_ref' | 'session_id'>,
) =>
  apiClient
    .post<ApiResponse<PortalTicketGrant>>(
      `${sessionPath(workspaceId, accountId, sessionId)}/portal-ticket/redeem`,
      {
        ...data,
        account_ref: { workspace_id: workspaceId, account_id: accountId },
        session_id: sessionId,
      } satisfies PortalTicketRedeemRequest,
      {
        headers: { [REVISION_HEADER]: String(data.expected_session_revision) },
        withCredentials: true,
      },
    )
    .then((response) => response.data.data)
export interface BrowserWorkspaceMember {
  user_id: string
  subject: string
  email: string | null
  display_name: string | null
  disabled: boolean
  role: 'admin' | 'maintainer' | 'operator' | 'viewer'
  created_at: string
}

export const listBrowserWorkspaceMembers = (workspaceId: string) =>
  apiClient
    .get<ApiResponse<BrowserWorkspaceMember[]>>(`/workspaces/${encodeURIComponent(workspaceId)}/members`)
    .then((response) => response.data.data)
