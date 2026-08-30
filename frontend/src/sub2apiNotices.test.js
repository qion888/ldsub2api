import assert from 'node:assert/strict';
import test from 'node:test';

import {buildSub2ApiImportNotice} from './sub2apiNotices.js';

test('builds separate import and fingerprint sections for a confirmed import', () => {
  const notice = buildSub2ApiImportNotice({
    ok: true,
    upstream_status: 200,
    result: {success: 1, failed: 0},
    import_verification: {confirmed: true, expected: 1, matched: 1, accepted: 1, failed: 0},
    fingerprint_verification: {
      mode: 'session', eligible: 1, matched: 1, verified: 1, repaired: 0, unresolved: 0,
    },
  });

  assert.equal(notice.type, 'success');
  assert.equal(notice.title, '账号导入完成');
  assert.deepEqual(notice.sections.map(section => section.title), ['账号导入', '指纹核验']);
  assert.deepEqual(notice.sections[0].metrics.map(item => item.value), ['1', '0', '1/1']);
  assert.deepEqual(notice.sections[1].metrics.map(item => item.value), ['1', '1', '1', '0', '0']);
});

test('uses a warning notice when imported accounts are not fully matched', () => {
  const notice = buildSub2ApiImportNotice({
    upstream_status: 200,
    result: {success: 2, failed: 0},
    import_verification: {confirmed: false, expected: 2, matched: 1, accepted: 2, failed: 0},
    fingerprint_verification: {
      mode: 'session', eligible: 2, matched: 1, verified: 1, repaired: 0, unresolved: 1,
    },
  });

  assert.equal(notice.type, 'warning');
  assert.match(notice.message, /1\/2/);
  assert.equal(notice.sections[1].metrics.at(-1).tone, 'negative');
});
