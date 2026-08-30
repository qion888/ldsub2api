import assert from 'node:assert/strict';
import test from 'node:test';

import {
  normalizeSub2ApiRecoveryResult,
  recoveryResultForProgress,
} from './sub2apiRecoveryModel.js';

test('separates permanent failures from retryable failures', () => {
  const model = normalizeSub2ApiRecoveryResult({
    ok: true,
    outcome: 'partial',
    reclaim_summary: {failed: 1, unreclaimable: 1, active: 0},
    reclaim_failures: [
      {card_code: 'CARD-403', status: 'failed', permanent: true, provider_status: 403, reason: '账号已停用'},
      {card_code: 'CARD-500', status: 'failed', retryable: true, provider_status: 500, reason: '上游暂时不可用'},
    ],
    retryable_card_codes: ['CARD-500'],
    retry_available: true,
  });

  assert.equal(model.retryAvailable, true);
  assert.deepEqual(model.retryCodes, ['CARD-500']);
  assert.equal(model.failures.find(item => item.card_code === 'CARD-403').retryable, false);
  assert.equal(model.failures.find(item => item.card_code === 'CARD-500').retryable, true);
});

test('permanent markers win when an upstream item also says retryable', () => {
  const model = normalizeSub2ApiRecoveryResult({
    outcome: 'partial',
    reclaim_summary: {unreclaimable: 1, failed: 0},
    reclaim_failures: [{
      card_code: 'CARD-403',
      status: 'failed',
      permanent: true,
      retryable: true,
      provider_status: 403,
      reason: 'account_deactivated',
    }],
    retryable_card_codes: ['CARD-403'],
    retry_available: true,
  });

  assert.equal(model.failures[0].retryable, false);
  assert.equal(model.retryAvailable, false);
  assert.deepEqual(model.retryCodes, []);
});

test('does not duplicate a permanent task when aggregate counters repeat it', () => {
  const model = normalizeSub2ApiRecoveryResult({
    ok: true,
    outcome: 'unrecoverable',
    reclaim_summary: {failed: 1, unreclaimable: 1},
    reclaim_failures: [{
      card_code: 'CARD-403',
      status: 'failed',
      permanent: true,
      provider_status: 403,
      error_code: 'account_deactivated',
    }],
  });

  assert.equal(model.failures.length, 1);
  assert.equal(model.failures[0].failure_bucket, 'unreclaimable');
  assert.equal(model.retryAvailable, false);
});

test('does not offer retry while any task is still active', () => {
  const model = normalizeSub2ApiRecoveryResult({
    outcome: 'pending',
    reclaim_summary: {active: 1, queued: 1, failed: 1},
    reclaim_failures: [{card_code: 'CARD-500', status: 'failed', retryable: true}],
    retryable_card_codes: ['CARD-500'],
  });

  assert.equal(model.retryAvailable, false);
});

test('keeps automatic import status independent from recovery outcome', () => {
  const model = normalizeSub2ApiRecoveryResult({
    ok: true,
    outcome: 'recovered',
    recovery_message: '找回完成',
    reclaim_summary: {done: 2, downloaded: 0},
    import_status: 'disabled',
    imported: false,
  });

  assert.equal(model.outcome, 'recovered');
  assert.equal(model.importStatus, 'disabled');
  assert.equal(model.importMeta.label, '自动导入已关闭');
});

test('merges complete progress responses without dropping scan context', () => {
  const merged = recoveryResultForProgress(
    {scanned_accounts: 12, accounts_401: 2, reclaim_card_codes: ['CARD-1', 'CARD-2']},
    {
      ok: true,
      outcome: 'partial',
      reclaim_summary: {done: 1, failed: 1},
      reclaim_failures: [{card_code: 'CARD-2', retryable: true, reason: '稍后重试'}],
      retryable_card_codes: ['CARD-2'],
    },
  );

  assert.equal(merged.scanned_accounts, 12);
  assert.deepEqual(merged.reclaim_card_codes, ['CARD-1', 'CARD-2']);
  assert.equal(merged.result.outcome, 'partial');
  assert.equal(merged.reclaim_failures[0].card_code, 'CARD-2');
});

test('normalizes legacy top-level redeem tasks and exposes retry codes', () => {
  const model = normalizeSub2ApiRecoveryResult({
    ok: true,
    requested_cards: 1,
    all_tasks: [{
      card_code: 'CARD-500',
      status: 'failed',
      provider_status: 500,
      message: 'upstream timeout',
    }],
  });

  assert.equal(model.outcome, 'failed');
  assert.deepEqual(model.retryCodes, ['CARD-500']);
  assert.equal(model.retryAvailable, true);
  assert.equal(model.failures[0].reason, 'upstream timeout');
});

test('creates visible failure detail when provider returns aggregate counters only', () => {
  const model = normalizeSub2ApiRecoveryResult({
    ok: true,
    outcome: 'partial',
    reclaim_summary: {failed: 1, unreclaimable: 1},
    error: 'provider rejected one task',
  });

  assert.equal(model.failures.length, 2);
  assert.equal(model.failures.some(item => item.category === 'unrecoverable'), true);
  assert.equal(model.failures.some(item => item.category === 'retryable'), true);
  assert.equal(model.retryAvailable, false);
});

test('uses downloaded payloads when an older response reports zero downloads', () => {
  const model = normalizeSub2ApiRecoveryResult({
    ok: true,
    result: {done: 1},
    downloaded: 0,
    downloaded_payloads: [{task: {order_no: 'ORDER-1'}, data: {accounts: [{id: 1}]}}],
  });

  assert.equal(model.outcome, 'recovered');
  assert.equal(model.summary.downloaded, 1);
  assert.equal(model.updated, 1);
});

test('counts download gaps as retryable and only queues the missing order', () => {
  const model = normalizeSub2ApiRecoveryResult({
    ok: true,
    reclaim_summary: {done: 2},
    all_tasks: [
      {card_code: 'CARD-1', status: 'done', order_no: 'ORDER-1', download_token: 'TOKEN-1'},
      {card_code: 'CARD-2', status: 'done', order_no: 'ORDER-2', download_token: 'TOKEN-2'},
    ],
    downloaded_payloads: [
      {task: {order_no: 'ORDER-1'}, data: {accounts: []}},
    ],
  });

  assert.equal(model.summary.done, 2);
  assert.equal(model.summary.downloaded, 1);
  assert.equal(model.summary.download_failed, 1);
  assert.equal(model.summary.failed, 1);
  assert.deepEqual(model.retryCodes, ['CARD-2']);
});

test('does not fabricate download failures when automatic import is disabled', () => {
  const model = normalizeSub2ApiRecoveryResult({
    ok: true,
    import_status: 'disabled',
    reclaim_summary: {done: 2},
    all_tasks: [
      {card_code: 'CARD-1', status: 'done', order_no: 'ORDER-1', download_token: 'TOKEN-1'},
      {card_code: 'CARD-2', status: 'done', order_no: 'ORDER-2', download_token: 'TOKEN-2'},
    ],
    downloaded_payloads: [],
  });

  assert.equal(model.downloadsRequested, false);
  assert.equal(model.summary.download_failed, 0);
  assert.equal(model.summary.failed, 0);
  assert.deepEqual(model.retryCodes, []);
});

test('keeps missing card-code accounts visible beside provider failures', () => {
  const model = normalizeSub2ApiRecoveryResult({
    ok: true,
    reclaim_summary: {failed: 1, unreclaimable: 1},
    reclaim_failures: [{card_code: 'CARD-500', status: 'failed', retryable: true}],
    missing_card_code_accounts: [{id: 42, name: '账号 42'}],
  });

  assert.equal(model.summary.unreclaimable, 1);
  assert.equal(model.failures.some(item => item.name === '账号 42'), true);
  assert.equal(model.failures.some(item => item.card_code === 'CARD-500' && item.retryable), true);
});

test('filters explicit permanent card codes from retry candidates', () => {
  const model = normalizeSub2ApiRecoveryResult({
    ok: true,
    reclaim_summary: {failed: 2},
    permanent_card_codes: ['CARD-403'],
    retryable_card_codes: ['CARD-403', 'CARD-500'],
  });

  assert.deepEqual(model.retryCodes, ['CARD-500']);
  assert.equal(model.failures.some(item => item.card_code === 'CARD-403'), false);
});

test('uses the latest progress counters when a prior response had terminal work', () => {
  const merged = recoveryResultForProgress(
    {reclaim_summary: {done: 3, failed: 1, unreclaimable: 2}},
    {reclaim_summary: {done: 1, failed: 0, unreclaimable: 0}},
  );

  assert.equal(merged.reclaim_summary.done, 1);
  assert.equal(merged.reclaim_summary.failed, 0);
  assert.equal(merged.reclaim_summary.unreclaimable, 0);
});

test('prefers normalized outer counters over stale nested provider counters', () => {
  const model = normalizeSub2ApiRecoveryResult({
    ok: true,
    reclaim_summary: {done: 1, failed: 0},
    result: {reclaim_summary: {done: 9, failed: 4}},
  });

  assert.equal(model.summary.done, 1);
  assert.equal(model.summary.failed, 0);
});
