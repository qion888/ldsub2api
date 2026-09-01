import test from 'node:test';
import assert from 'node:assert/strict';

import {DEFAULT_SUB2API_CARD_ASSIGNMENT, normalizeSub2ApiCardAssignment, readSub2ApiCardAssignment, SUB2API_CARD_ASSIGNMENT_KEY} from './sub2apiAssignment.js';

test('normalizes independent card import assignment settings', () => {
  assert.deepEqual(normalizeSub2ApiCardAssignment({
    proxy_choice: 'proxy:12',
    group_ids: ['3', 3, 0, -1, 'bad'],
    codex_fingerprint_mode: 'session',
  }), {proxy_choice: 'proxy:12', group_ids: [3], codex_fingerprint_mode: 'session'});
  assert.deepEqual(normalizeSub2ApiCardAssignment({proxy_choice: 'invalid', codex_fingerprint_mode: 'unknown'}), DEFAULT_SUB2API_CARD_ASSIGNMENT);
});

test('reads card assignment settings from a storage-like object', () => {
  const storage = {getItem: key => key === SUB2API_CARD_ASSIGNMENT_KEY ? '{"proxy_choice":"none","group_ids":[7]}' : null};
  assert.deepEqual(readSub2ApiCardAssignment(storage), {proxy_choice: 'none', group_ids: [7], codex_fingerprint_mode: 'off'});
  assert.deepEqual(readSub2ApiCardAssignment({getItem: () => '{bad json'}), DEFAULT_SUB2API_CARD_ASSIGNMENT);
});
