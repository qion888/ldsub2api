export const SHOP_WAF_ACTIVE_STATUSES = new Set([
  'awaiting_verification',
  'ready',
  'action_required',
]);

export function shopWafChallengeId(result) {
  return String(result?.challenge_id || '').trim();
}

export function isShopWafActiveStatus(status) {
  return SHOP_WAF_ACTIVE_STATUSES.has(String(status || ''));
}

export function shouldPollShopWaf(status) {
  return status === 'awaiting_verification';
}

export function mergeShopWafState(current, result) {
  if (!result || typeof result !== 'object') {
    throw new TypeError('WAF verification response must be an object');
  }
  const status = String(result.status || '');
  const challengeId = shopWafChallengeId(result);
  if (isShopWafActiveStatus(status) && !challengeId) {
    const error = new Error('WAF 验证服务未返回 challenge_id，请重新打开验证');
    error.code = 'WAF_CHALLENGE_MISSING';
    throw error;
  }
  const terminal = ['idle', 'success', 'error'].includes(status);
  return {
    ...current,
    ...result,
    challenge_id: isShopWafActiveStatus(status)
      ? challengeId
      : terminal ? null : challengeId || current?.challenge_id || null,
    pending_shop_ids: Array.isArray(result.pending_shop_ids)
      ? result.pending_shop_ids
      : current?.pending_shop_ids || [],
    results: Array.isArray(result.results) ? result.results : current?.results || [],
    poll_error: '',
  };
}

export function mergeShopWafPollError(current, message) {
  return {
    ...current,
    poll_error: String(message || '验证状态检查暂时失败，请稍候重试'),
  };
}

export function trackVerificationRequest(registry, requestKey, operation) {
  const key = String(requestKey || '').trim();
  if (!(registry instanceof Map) || !key) throw new TypeError('A request registry and key are required');
  const existing = registry.get(key);
  if (existing) return existing;

  let promise;
  try {
    promise = Promise.resolve(operation());
  } catch (error) {
    promise = Promise.reject(error);
  }
  registry.set(key, promise);
  const release = () => {
    if (registry.get(key) === promise) registry.delete(key);
  };
  promise.then(release, release);
  return promise;
}
