import test from 'node:test';
import assert from 'node:assert/strict';

import {
  EMPTY_SHOP_WAF_STATE,
  normalizeShopWafState,
  shopWafSessionActive,
  shopWafShouldComplete,
  shopWafShouldPoll,
  shopWafProxyLabel,
  shopWafResponseMatches,
} from './shopWafModel.js';

test('normalizes a recoverable storefront WAF session', () => {
  const state = normalizeShopWafState({
    status: 'awaiting_verification',
    challenge_id: 'challenge-1',
    current_shop_id: '4',
    pending_shop_ids: [4, '5', 'bad'],
    browser_mode: 'native_attach',
    proxy_mode: 'system',
  });

  assert.equal(state.current_shop_id, 4);
  assert.deepEqual(state.pending_shop_ids, [4, 5]);
  assert.equal(shopWafSessionActive(state), true);
  assert.equal(shopWafShouldPoll(state), true);
  assert.equal(shopWafShouldComplete(state), false);
});

test('only a ready challenge can trigger one completion request', () => {
  const ready = normalizeShopWafState({status: 'ready', challenge_id: 'challenge-2'});
  const stale = normalizeShopWafState({status: 'ready'});

  assert.equal(shopWafShouldComplete(ready), true);
  assert.equal(shopWafShouldComplete(stale), false);
  assert.equal(shopWafShouldPoll(ready), false);
});

test('terminal status clears stale challenge and current shop', () => {
  const pending = normalizeShopWafState({
    status: 'awaiting_verification',
    challenge_id: 'challenge-3',
    current_shop_id: 8,
  });
  const completed = normalizeShopWafState({status: 'success'}, pending);

  assert.equal(completed.challenge_id, null);
  assert.equal(completed.current_shop_id, null);
  assert.equal(shopWafSessionActive(completed), false);
  assert.deepEqual(normalizeShopWafState(null), EMPTY_SHOP_WAF_STATE);
});

test('retry exhaustion stops polling and allows a fresh browser session', () => {
  const exhausted = normalizeShopWafState({
    status: 'retry_exhausted',
    challenge_id: 'old-challenge',
    current_shop_id: 8,
    detail: 'start a new browser session',
  });

  assert.equal(exhausted.challenge_id, null);
  assert.equal(exhausted.current_shop_id, null);
  assert.equal(shopWafSessionActive(exhausted), false);
  assert.equal(shopWafShouldPoll(exhausted), false);
});

test('rejects a poll response after the active challenge changes', () => {
  const expected = 'challenge-old';

  assert.equal(shopWafResponseMatches(expected, {challenge_id: expected}, {challenge_id: expected}), true);
  assert.equal(shopWafResponseMatches(expected, {challenge_id: expected}, {status: 'idle'}), true);
  assert.equal(shopWafResponseMatches(expected, {challenge_id: 'challenge-new'}, {challenge_id: expected}), false);
  assert.equal(shopWafResponseMatches(expected, {challenge_id: expected}, {challenge_id: 'challenge-new'}), false);
});

test('describes only known proxy modes as configured', () => {
  assert.equal(shopWafProxyLabel('system'), '跟随系统 VPN');
  assert.equal(shopWafProxyLabel('direct'), '直连');
  assert.equal(shopWafProxyLabel('socks'), '指定代理');
  assert.equal(shopWafProxyLabel(null), '代理状态未知');
});
