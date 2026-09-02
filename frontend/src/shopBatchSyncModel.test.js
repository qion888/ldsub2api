import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DEFAULT_SHOP_BATCH_SYNC_INTERVAL_SECONDS,
  MAX_SHOP_BATCH_SYNC_INTERVAL_SECONDS,
  MIN_SHOP_BATCH_SYNC_INTERVAL_SECONDS,
  normalizeShopBatchSyncInterval,
  shopBatchSyncIntervalLabel,
  summarizeShopBatchSync,
} from './shopBatchSyncModel.js';

test('normalizes the persisted shop batch interval into the supported range', () => {
  assert.equal(normalizeShopBatchSyncInterval(undefined), DEFAULT_SHOP_BATCH_SYNC_INTERVAL_SECONDS);
  assert.equal(normalizeShopBatchSyncInterval('8'), 8);
  assert.equal(normalizeShopBatchSyncInterval(0), MIN_SHOP_BATCH_SYNC_INTERVAL_SECONDS);
  assert.equal(normalizeShopBatchSyncInterval(99), MAX_SHOP_BATCH_SYNC_INTERVAL_SECONDS);
  assert.equal(normalizeShopBatchSyncInterval(2.5), DEFAULT_SHOP_BATCH_SYNC_INTERVAL_SECONDS);
  assert.equal(normalizeShopBatchSyncInterval('bad', 9), 9);
});

test('summarizes explicit and legacy batch responses without duplicate waf shops', () => {
  assert.deepEqual(summarizeShopBatchSync({
    interval_seconds: 6,
    total: 3,
    succeeded: 1,
    failed: 2,
    results: [{id: 1, ok: true}, {id: 2, ok: false}, {id: 3, ok: false}],
    waf_shop_ids: [2, '2', 3, 'bad'],
  }), {
    total: 3,
    succeeded: 1,
    failed: 2,
    intervalSeconds: 6,
    wafShopIds: [2, 3],
  });

  assert.deepEqual(summarizeShopBatchSync({results: [{ok: true}, {ok: false}]}, 8), {
    total: 2,
    succeeded: 1,
    failed: 1,
    intervalSeconds: DEFAULT_SHOP_BATCH_SYNC_INTERVAL_SECONDS,
    wafShopIds: [],
  });
  assert.equal(shopBatchSyncIntervalLabel(12), '12 秒');
});
