import test from 'node:test';
import assert from 'node:assert/strict';

import {DEFAULT_SUB2API_SECTION, SUB2API_SECTIONS, normalizeSub2ApiSection} from './sub2apiNavigation.js';

test('defines the three Sub2API secondary navigation sections in product order', () => {
  assert.deepEqual(SUB2API_SECTIONS.map(section => section.value), ['cards', 'automation', 'accounts']);
  assert.equal(SUB2API_SECTIONS[0].label, '卡密核验与推送');
  assert.equal(SUB2API_SECTIONS[1].label, '定时找回与自动导入');
  assert.equal(SUB2API_SECTIONS[2].label, '账号列表');
});

test('normalizes unknown Sub2API sections to the card workflow', () => {
  assert.equal(DEFAULT_SUB2API_SECTION, 'cards');
  assert.equal(normalizeSub2ApiSection('accounts'), 'accounts');
  assert.equal(normalizeSub2ApiSection('unknown'), 'cards');
  assert.equal(normalizeSub2ApiSection(''), 'cards');
});
