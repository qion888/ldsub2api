export const EMPTY_SHOP_WAF_STATE = Object.freeze({
  status: 'idle',
  browser: null,
  browser_mode: null,
  proxy_mode: null,
  challenge_id: null,
  completed: 0,
  total: 0,
  current_shop_id: null,
  pending_shop_ids: [],
  results: [],
  detail: '',
});

const VALID_STATUSES = new Set(['idle', 'awaiting_verification', 'ready', 'success', 'browser_closed', 'retry_exhausted', 'error']);

export function normalizeShopWafState(value, current = EMPTY_SHOP_WAF_STATE) {
  const input = value && typeof value === 'object' ? value : {};
  const status = VALID_STATUSES.has(input.status) ? input.status : current.status || 'idle';
  const terminal = ['idle', 'success', 'browser_closed', 'retry_exhausted', 'error'].includes(status);
  return {
    ...current,
    ...input,
    status,
    browser: input.browser ?? current.browser ?? null,
    browser_mode: input.browser_mode ?? current.browser_mode ?? null,
    proxy_mode: input.proxy_mode ?? current.proxy_mode ?? null,
    challenge_id: terminal ? null : String(input.challenge_id || current.challenge_id || '').trim() || null,
    current_shop_id: terminal
      ? null
      : Number.isInteger(Number(input.current_shop_id)) && Number(input.current_shop_id) > 0
        ? Number(input.current_shop_id)
        : current.current_shop_id ?? null,
    completed: Math.max(0, Number(input.completed ?? current.completed) || 0),
    total: Math.max(0, Number(input.total ?? current.total) || 0),
    pending_shop_ids: Array.isArray(input.pending_shop_ids)
      ? input.pending_shop_ids.map(Number).filter(Number.isInteger).filter(id => id > 0)
      : [...(current.pending_shop_ids || [])],
    results: Array.isArray(input.results) ? input.results : [...(current.results || [])],
    detail: String(input.detail || ''),
  };
}

export function shopWafSessionActive(state) {
  return state?.status === 'awaiting_verification' || state?.status === 'ready';
}

export function shopWafShouldPoll(state) {
  return state?.status === 'awaiting_verification' && Boolean(state?.challenge_id);
}

export function shopWafShouldComplete(state) {
  return state?.status === 'ready' && Boolean(state?.challenge_id);
}

export function shopWafResponseMatches(expectedChallengeId, current, response) {
  const expected = String(expectedChallengeId || '').trim();
  const currentChallenge = String(current?.challenge_id || '').trim();
  const responseChallenge = String(response?.challenge_id || '').trim();
  return Boolean(expected) && currentChallenge === expected && (!responseChallenge || responseChallenge === expected);
}

export function shopWafProxyLabel(mode) {
  if (mode === 'system') return '跟随系统 VPN';
  if (mode === 'direct') return '直连';
  if (['http', 'https', 'socks'].includes(mode)) return '指定代理';
  return '代理状态未知';
}
