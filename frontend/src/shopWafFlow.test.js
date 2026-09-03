import test from 'node:test';
import assert from 'node:assert/strict';
import {
  isShopWafActiveStatus,
  mergeShopWafPollError,
  mergeShopWafState,
  shouldPollShopWaf,
  trackVerificationRequest,
} from './shopWafFlow.js';

test('requires a challenge id for every active verification state', () => {
  for (const status of ['awaiting_verification', 'ready', 'action_required']) {
    assert.throws(
      () => mergeShopWafState({challenge_id: 'old'}, {status}),
      error => error.code === 'WAF_CHALLENGE_MISSING',
    );
  }
});

test('action required preserves the new challenge and stops automatic polling', () => {
  const state = mergeShopWafState(
    {status: 'awaiting_verification', challenge_id: 'challenge-a', poll_error: 'old error'},
    {status: 'action_required', challenge_id: 'challenge-b', detail: '请重新打开验证'},
  );

  assert.equal(state.challenge_id, 'challenge-b');
  assert.equal(state.detail, '请重新打开验证');
  assert.equal(state.poll_error, '');
  assert.equal(isShopWafActiveStatus(state.status), true);
  assert.equal(shouldPollShopWaf(state.status), false);
});

test('a temporary polling error keeps the active challenge and polling state', () => {
  const current = {
    status: 'awaiting_verification',
    challenge_id: 'challenge-a',
    detail: '等待浏览器验证',
  };
  const state = mergeShopWafPollError(current, 'HTTP 503');

  assert.equal(state.status, 'awaiting_verification');
  assert.equal(state.challenge_id, 'challenge-a');
  assert.equal(state.detail, '等待浏览器验证');
  assert.equal(state.poll_error, 'HTTP 503');
  assert.equal(shouldPollShopWaf(state.status), true);
});

test('terminal state clears the challenge id', () => {
  const state = mergeShopWafState(
    {status: 'awaiting_verification', challenge_id: 'challenge-a'},
    {status: 'success', completed: 1},
  );
  assert.equal(state.challenge_id, null);
  assert.equal(isShopWafActiveStatus(state.status), false);
});

test('deduplicates only the same request key and runs a new challenge independently', async () => {
  const registry = new Map();
  let resolveA;
  let resolveB;
  let calls = 0;
  const firstA = trackVerificationRequest(registry, 'complete:challenge-a', () => {
    calls += 1;
    return new Promise(resolve => { resolveA = resolve; });
  });
  const secondA = trackVerificationRequest(registry, 'complete:challenge-a', () => {
    calls += 1;
    return Promise.resolve('duplicate');
  });
  const firstB = trackVerificationRequest(registry, 'complete:challenge-b', () => {
    calls += 1;
    return new Promise(resolve => { resolveB = resolve; });
  });

  assert.equal(firstA, secondA);
  assert.notEqual(firstA, firstB);
  assert.equal(calls, 2);
  resolveB('b');
  assert.equal(await firstB, 'b');
  assert.equal(registry.has('complete:challenge-b'), false);
  assert.equal(registry.has('complete:challenge-a'), true);
  resolveA('a');
  assert.equal(await firstA, 'a');
  assert.equal(registry.size, 0);
});
