export const DEFAULT_SHOP_BATCH_SYNC_INTERVAL_SECONDS = 3;
export const MIN_SHOP_BATCH_SYNC_INTERVAL_SECONDS = 1;
export const MAX_SHOP_BATCH_SYNC_INTERVAL_SECONDS = 60;

export function normalizeShopBatchSyncInterval(value, fallback = DEFAULT_SHOP_BATCH_SYNC_INTERVAL_SECONDS) {
  const fallbackNumber = Number(fallback);
  const normalizedFallback = Number.isInteger(fallbackNumber)
    ? Math.max(MIN_SHOP_BATCH_SYNC_INTERVAL_SECONDS, Math.min(MAX_SHOP_BATCH_SYNC_INTERVAL_SECONDS, fallbackNumber))
    : DEFAULT_SHOP_BATCH_SYNC_INTERVAL_SECONDS;
  const number = Number(value);
  if (!Number.isInteger(number)) return normalizedFallback;
  return Math.max(MIN_SHOP_BATCH_SYNC_INTERVAL_SECONDS, Math.min(MAX_SHOP_BATCH_SYNC_INTERVAL_SECONDS, number));
}

export function summarizeShopBatchSync(result = {}, expectedTotal = 0) {
  const rows = Array.isArray(result.results) ? result.results : [];
  const fallbackTotal = Math.max(0, Number(expectedTotal) || 0);
  const total = Math.max(0, Number(result.total ?? (rows.length || fallbackTotal)) || fallbackTotal);
  const succeeded = Math.max(0, Number(result.succeeded) || rows.filter(entry => entry?.ok).length);
  const failed = Math.max(0, Number(result.failed) || rows.filter(entry => entry && !entry.ok).length);
  const wafShopIds = Array.isArray(result.waf_shop_ids)
    ? [...new Set(result.waf_shop_ids.map(Number).filter(value => Number.isInteger(value) && value > 0))]
    : [];
  return {
    total,
    succeeded,
    failed,
    intervalSeconds: normalizeShopBatchSyncInterval(result.interval_seconds),
    wafShopIds,
  };
}

export function shopBatchSyncIntervalLabel(value) {
  return `${normalizeShopBatchSyncInterval(value)} 秒`;
}
