export const SUB2API_CARD_ASSIGNMENT_KEY = 'ldxp-sub2api-card-assignment-v1';

export const DEFAULT_SUB2API_CARD_ASSIGNMENT = Object.freeze({
  proxy_choice: 'json',
  group_ids: Object.freeze([]),
  codex_fingerprint_mode: 'off',
});

export function normalizeSub2ApiCardAssignment(value) {
  const source = value && typeof value === 'object' ? value : {};
  const rawProxy = String(source.proxy_choice || '').trim();
  const proxyChoice = rawProxy === 'json' || rawProxy === 'none' || /^proxy:\d+$/.test(rawProxy)
    ? rawProxy
    : DEFAULT_SUB2API_CARD_ASSIGNMENT.proxy_choice;
  const groupIds = Array.isArray(source.group_ids)
    ? [...new Set(source.group_ids.map(Number).filter(item => Number.isInteger(item) && item > 0))]
    : [];
  const mode = ['off', 'device', 'session', 'full'].includes(source.codex_fingerprint_mode)
    ? source.codex_fingerprint_mode
    : DEFAULT_SUB2API_CARD_ASSIGNMENT.codex_fingerprint_mode;
  return {proxy_choice: proxyChoice, group_ids: groupIds, codex_fingerprint_mode: mode};
}

export function readSub2ApiCardAssignment(storage) {
  try {
    return normalizeSub2ApiCardAssignment(JSON.parse(storage?.getItem(SUB2API_CARD_ASSIGNMENT_KEY) || 'null'));
  } catch {
    return DEFAULT_SUB2API_CARD_ASSIGNMENT;
  }
}
