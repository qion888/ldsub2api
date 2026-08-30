import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildSub2ApiAutomationSaveNotice,
  buildSub2ApiImportNotice,
  sub2ApiHistoryDeleteErrorMessage,
} from './sub2apiNotices.js';

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

test('distinguishes unavailable single and batch history deletion routes', () => {
  const missingRoute = Object.assign(new Error('接口不存在'), {status: 404});

  assert.equal(
    sub2ApiHistoryDeleteErrorMessage(missingRoute),
    '单条删除接口尚未加载，请重启本项目服务后重试',
  );
  assert.equal(
    sub2ApiHistoryDeleteErrorMessage(missingRoute, true),
    '批量删除接口尚未加载，请重启本项目服务后重试',
  );
  assert.equal(
    sub2ApiHistoryDeleteErrorMessage(Object.assign(new Error('卡密导入记录不存在'), {status: 404})),
    '卡密导入记录不存在',
  );
});

test('reports successful automation save when fingerprint mode is passthrough', () => {
  assert.equal(
    buildSub2ApiAutomationSaveNotice({
      enabled: true,
      auto_import: true,
      codex_fingerprint_mode: 'off',
    }),
    '保存成功：定时找回与自动导入已启用，Codex 指纹：透传',
  );
});
