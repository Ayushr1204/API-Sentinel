import type { Decision, Severity } from './types'

export const SEVERITY_LABEL: Record<Severity, string> = {
  BREAKING: 'Breaking',
  POTENTIALLY_BREAKING: 'Potentially breaking',
  NON_BREAKING: 'Safe',
}

export const DECISION_COPY: Record<Decision, { word: string; line: string }> = {
  BLOCK: { word: 'Block', line: 'Existing clients will break. Tests confirmed at least one breaking change.' },
  HUMAN_REVIEW: { word: 'Review', line: 'Some changes may affect clients, and a person needs to judge their impact.' },
  PROCEED: { word: 'Proceed', line: 'No change is expected to affect existing clients.' },
}

const RULES: Record<string, string> = {
  PATH_REMOVED: 'Path removed',
  OPERATION_REMOVED: 'Operation removed',
  PATH_ADDED: 'Path added',
  OPERATION_ADDED: 'Operation added',
  OPERATION_DEPRECATED: 'Operation deprecated',
  OPERATION_ID_CHANGED: 'operationId renamed',
  PATH_PARAM_RENAMED: 'Path parameter renamed',
  SECURITY_ADDED: 'Authentication now required',
  SECURITY_REMOVED: 'Authentication no longer required',
  SECURITY_REQUIREMENT_CHANGED: 'Security requirements changed',
  SECURITY_SCHEME_CHANGED: 'Security scheme changed',
  SERVER_URL_REMOVED: 'Server URL removed',
  PARAM_REMOVED: 'Parameter removed',
  REQUIRED_PARAM_ADDED: 'Required parameter added',
  OPTIONAL_PARAM_ADDED: 'Optional parameter added',
  PARAM_BECAME_REQUIRED: 'Parameter now required',
  PARAM_BECAME_OPTIONAL: 'Parameter now optional',
  PARAM_SERIALIZATION_CHANGED: 'Parameter serialization changed',
  PARAM_DEPRECATED: 'Parameter deprecated',
  REQUEST_BODY_ADDED: 'Request body added',
  REQUEST_BODY_REMOVED: 'Request body removed',
  REQUEST_BODY_BECAME_REQUIRED: 'Request body now required',
  REQUEST_MEDIA_TYPE_REMOVED: 'Request media type dropped',
  REQUEST_MEDIA_TYPE_ADDED: 'Request media type added',
  SUCCESS_RESPONSE_REMOVED: 'Success response removed',
  RESPONSE_STATUS_REMOVED: 'Response status removed',
  RESPONSE_STATUS_ADDED: 'Response status added',
  RESPONSE_HEADER_REMOVED: 'Response header removed',
  RESPONSE_MEDIA_TYPE_REMOVED: 'Response media type dropped',
  RESPONSE_MEDIA_TYPE_ADDED: 'Response media type added',
  TYPE_CHANGED: 'Type changed',
  TYPE_WIDENED: 'Type widened',
  TYPE_NARROWED: 'Type narrowed',
  NULLABLE_ADDED: 'Can now be null',
  NULLABLE_REMOVED: 'Can no longer be null',
  ENUM_VALUE_REMOVED: 'Enum value removed',
  ENUM_VALUE_ADDED: 'Enum value added',
  ENUM_RESTRICTION_ADDED: 'Enum restriction added',
  ENUM_RESTRICTION_REMOVED: 'Enum restriction removed',
  CONSTRAINT_TIGHTENED: 'Constraint tightened',
  CONSTRAINT_LOOSENED: 'Constraint loosened',
  PATTERN_CHANGED: 'Pattern changed',
  FORMAT_CHANGED: 'Format changed',
  REQUEST_PROPERTY_REMOVED: 'Request field removed',
  RESPONSE_PROPERTY_REMOVED: 'Response field removed',
  REQUIRED_PROPERTY_ADDED: 'Required field added',
  PROPERTY_ADDED: 'Field added',
  PROPERTY_BECAME_REQUIRED: 'Field now required',
  PROPERTY_BECAME_OPTIONAL: 'Field now optional',
  COMPOSITION_CHANGED: 'oneOf/anyOf changed',
}

export const ruleLabel = (rule: string) => RULES[rule] ?? rule

/** Why a rule matters to an existing client. Written per direction-aware rule of the diff engine. */
const WHY: Record<string, string> = {
  PATH_REMOVED: 'Every existing call to this path will get 404 or 405.',
  OPERATION_REMOVED: 'Existing calls to this method will be rejected (405) or routed elsewhere.',
  OPERATION_ID_CHANGED: 'Generated SDKs name methods after the operationId, so client code calling the old method will not compile or link.',
  SECURITY_ADDED: 'Clients that call anonymously today will receive 401/403.',
  SECURITY_REQUIREMENT_CHANGED: 'Credentials clients send today no longer satisfy the operation.',
  SECURITY_SCHEME_CHANGED: 'Clients send credentials in the old place or format, so they will not be recognised.',
  SERVER_URL_REMOVED: 'Clients configured with this base URL may stop reaching the API.',
  PARAM_REMOVED: 'Clients still send it. The server may ignore it silently (changed behaviour) or reject it.',
  REQUIRED_PARAM_ADDED: 'Existing clients do not send this parameter, so their requests become invalid.',
  PARAM_BECAME_REQUIRED: 'Clients that omit it today will be rejected.',
  PARAM_SERIALIZATION_CHANGED: 'Arrays or objects are encoded differently on the wire, so the server may misread existing requests.',
  REQUEST_BODY_ADDED: 'Existing clients send no body.',
  REQUEST_BODY_BECAME_REQUIRED: 'Existing clients may send no body.',
  REQUEST_BODY_REMOVED: 'Data clients send may now be ignored.',
  REQUEST_MEDIA_TYPE_REMOVED: 'Clients sending this Content-Type will get 415.',
  SUCCESS_RESPONSE_REMOVED: 'Clients branch on this success status and will treat the new reply as an error or ignore it.',
  RESPONSE_STATUS_ADDED: 'A new success status may not be handled by clients that check for specific codes.',
  RESPONSE_HEADER_REMOVED: 'Clients that read this header (pagination, rate limits, request IDs) lose it.',
  RESPONSE_MEDIA_TYPE_REMOVED: 'Clients parse this media type and will fail on anything else.',
  TYPE_CHANGED: 'Values of the old type are rejected (requests) or arrive in a form clients cannot parse (responses).',
  NULLABLE_ADDED: 'Clients that dereference this value without a null check will crash.',
  NULLABLE_REMOVED: 'Clients that send null will be rejected.',
  ENUM_VALUE_REMOVED: 'Clients sending this value will be rejected.',
  ENUM_VALUE_ADDED: 'Clients with exhaustive switches or strict enums fail on the unknown value.',
  ENUM_RESTRICTION_ADDED: 'Free values clients send today may now be rejected.',
  ENUM_RESTRICTION_REMOVED: 'Any value may appear, including ones clients do not handle.',
  CONSTRAINT_TIGHTENED: 'Values valid today (e.g. at the old limit) will be rejected.',
  CONSTRAINT_LOOSENED: 'Responses may contain values outside the range clients were built for.',
  PATTERN_CHANGED: 'Some values valid today may no longer match the pattern.',
  FORMAT_CHANGED: 'Parsers keyed on the format (dates, UUIDs) may fail.',
  REQUEST_PROPERTY_REMOVED: 'Clients still send this field. It is ignored at best and rejected if the schema is closed.',
  RESPONSE_PROPERTY_REMOVED: 'Clients reading this field get nothing. A schema check cannot see this when the field was optional, so consumer evidence decides.',
  REQUIRED_PROPERTY_ADDED: 'Existing clients do not send this field, so their requests become invalid.',
  PROPERTY_BECAME_REQUIRED: 'Clients that omit the field will be rejected.',
  PROPERTY_BECAME_OPTIONAL: 'Responses may omit a field clients rely on.',
  COMPOSITION_CHANGED: 'The set of accepted shapes changed, so some existing messages may no longer match.',
}
export const ruleWhy = (rule: string, severity: string) =>
  WHY[rule] ?? (severity === 'NON_BREAKING' ? 'Additive or relaxing change: existing clients keep working.' : 'May change behaviour for existing clients.')

export type Category = 'Endpoints' | 'Security' | 'Parameters' | 'Request body' | 'Response body' | 'Responses'
export function categoryOf(c: { area: string | null; segments: string[] }): Category {
  if (c.area === 'security') return 'Security'
  if (c.area === 'parameter') return 'Parameters'
  if (c.area === 'request_body') return 'Request body'
  if (c.area === 'response') return c.segments.length ? 'Response body' : 'Responses'
  return 'Endpoints'
}
export const CATEGORY_ORDER: Category[] = ['Endpoints', 'Security', 'Parameters', 'Request body', 'Response body', 'Responses']

/** Test outcome labels. Failed = an incompatibility was observed; the others mean no compatibility verdict. */
export const STATUS_LABEL: Record<string, { text: string; tone: 'bad' | 'ok' | 'warn' | 'muted'; hint: string }> = {
  passed: { text: 'Passed', tone: 'ok', hint: 'Old clients stay compatible on this check.' },
  failed: { text: 'Failed', tone: 'bad', hint: 'An incompatibility was observed: this is evidence of a break.' },
  error: { text: 'Test error', tone: 'warn', hint: 'The test could not run. This is not a compatibility verdict.' },
  timeout: { text: 'Timed out', tone: 'warn', hint: 'The deployment did not answer in time. This is not a compatibility verdict.' },
  infra_error: { text: 'Infrastructure failure', tone: 'warn', hint: 'The deployment could not be reached. This is not a compatibility verdict.' },
  inconclusive: { text: 'Inconclusive', tone: 'muted', hint: 'The check ran but cannot decide this kind of change.' },
  skipped: { text: 'Skipped', tone: 'muted', hint: 'Not run.' },
}

export const VERDICT_LABEL: Record<string, string> = {
  confirms_prediction: 'Matches prediction',
  contradicts_prediction: 'Contradicts prediction',
  risk_confirmed: 'Risk observed',
  inconclusive: 'Inconclusive',
}

export const OUTCOME_LABEL: Record<string, { text: string; tone: 'bad' | 'warn' | 'ok' | 'muted' }> = {
  consumer_break_confirmed: { text: 'Breaks a consumer', tone: 'bad' },
  live_break_confirmed: { text: 'Breaks live', tone: 'bad' },
  affected_consumers_identified: { text: 'Consumers affected', tone: 'bad' },
  consumer_usage_found: { text: 'Used by consumers', tone: 'warn' },
  no_consumer_impact_claimed: { text: 'No consumer impact', tone: 'ok' },
  no_usage_found: { text: 'No usage found', tone: 'muted' },
  no_context: { text: 'No consumer files', tone: 'muted' },
  contradictory_evidence: { text: 'Model contradicted by rules', tone: 'warn' },
  unverified_model_claim: { text: 'Unverified model claim', tone: 'warn' },
}

export const FINDING_LABEL: Record<string, string> = {
  reads_field: 'reads the field',
  reads_without_null_check: 'reads it without a null check',
  reads_with_null_check: 'reads it with a null check',
  enumerates_known_values: 'handles only the known values',
  handles_new_value: 'already handles the new value',
  calls_sdk_method: 'calls the generated SDK method',
  reads_header: 'reads the header',
  sends_removed_value: 'sends the removed value',
  sends_field: 'sends the field',
  calls_operation: 'calls the operation',
  not_used: 'does not use it',
}

export const MODE_LABEL: Record<string, string> = { tool: 'Tool', rules: 'Rules', llm: 'LLM' }
