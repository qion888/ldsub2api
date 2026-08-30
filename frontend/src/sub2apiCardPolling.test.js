import assert from 'node:assert/strict';
import test from 'node:test';

import {
  pollForReclaimDownloads,
  reclaimPollingFailureMessage,
  summarizeReclaimProgress,
} from './sub2apiCardPolling.js';

const payload = {
  task: {order_no: 'ORDER-1'},
  data: {accounts: [{name: 'Recovered'}]},
};

test('keeps polling when counters are settled but account JSON is not ready yet', async () => {
  let clock = 0;
  const responses = [
    {result: {queued: 0, already_running: 0, done: 1}, downloaded_payloads: []},
    {result: {queued: 0, already_running: 0, done: 1}, downloaded_payloads: [payload]},
  ];
  const snapshots = [];

  const result = await pollForReclaimDownloads({
    initialResponse: {result: {queued: 0, already_running: 0, done: 1}},
    requestProgress: async () => responses.shift(),
    onSnapshot: snapshot => snapshots.push(snapshot),
    intervalMs: 5000,
    timeoutMs: 60000,
    now: () => clock,
    sleep: async milliseconds => { clock += milliseconds; },
  });

  assert.equal(result.completed, true);
  assert.equal(result.timedOut, false);
  assert.equal(result.attempts, 2);
  assert.equal(result.elapsedMs, 10000);
  assert.deepEqual(result.downloads, [payload]);
  assert.equal(snapshots[0].completed, false);
});

test('stops at the one minute deadline when a done task still has no JSON', async () => {
  let clock = 0;
  const result = await pollForReclaimDownloads({
    initialResponse: {result: {queued: 0, already_running: 0, done: 1}},
    requestProgress: async () => ({result: {queued: 0, already_running: 0, done: 1}}),
    intervalMs: 5000,
    timeoutMs: 60000,
    now: () => clock,
    sleep: async milliseconds => { clock += milliseconds; },
  });

  assert.equal(result.completed, false);
  assert.equal(result.timedOut, true);
  assert.equal(result.elapsedMs, 60000);
  assert.equal(result.attempts, 12);
  assert.match(reclaimPollingFailureMessage(result), /等待可下载的账号 JSON 超过 1 分钟/);
});

test('does not treat a partial set of account JSON files as complete', async () => {
  let clock = 0;
  const result = await pollForReclaimDownloads({
    initialResponse: {result: {queued: 0, already_running: 0, done: 2}, downloaded_payloads: [payload]},
    requestProgress: async () => ({result: {queued: 0, already_running: 0, done: 2}, downloaded_payloads: [payload]}),
    intervalMs: 5000,
    timeoutMs: 60000,
    now: () => clock,
    sleep: async milliseconds => { clock += milliseconds; },
  });

  assert.equal(result.completed, false);
  assert.equal(result.timedOut, true);
  assert.match(reclaimPollingFailureMessage(result), /已下载 1\/2 个账号 JSON/);
});

test('keeps polling terminal-looking snapshots for the full minute', async () => {
  let clock = 0;
  let attempts = 0;
  const initialResponse = {result: {queued: 0, already_running: 0, done: 0, failed: 1, unreclaimable: 2}};
  const summary = summarizeReclaimProgress(initialResponse);
  const result = await pollForReclaimDownloads({
    initialResponse,
    requestProgress: async () => {
      attempts += 1;
      return initialResponse;
    },
    intervalMs: 5000,
    timeoutMs: 60000,
    now: () => clock,
    sleep: async milliseconds => { clock += milliseconds; },
  });

  assert.equal(summary.terminal, true);
  assert.equal(result.timedOut, true);
  assert.equal(result.elapsedMs, 60000);
  assert.equal(attempts, 12);
  assert.match(reclaimPollingFailureMessage(result), /持续轮询 1 分钟/);
  assert.match(reclaimPollingFailureMessage(result), /失败 1、不可找回 2/);
});

test('recovers JSON after an early terminal-looking snapshot', async () => {
  let clock = 0;
  const responses = [
    {result: {queued: 0, already_running: 0, done: 0, failed: 1}},
    {result: {queued: 0, already_running: 0, done: 1}, downloaded_payloads: [payload]},
  ];

  const result = await pollForReclaimDownloads({
    initialResponse: {result: {queued: 0, already_running: 0, done: 0, failed: 1}},
    requestProgress: async () => responses.shift(),
    intervalMs: 5000,
    timeoutMs: 60000,
    now: () => clock,
    sleep: async milliseconds => { clock += milliseconds; },
  });

  assert.equal(result.completed, true);
  assert.equal(result.timedOut, false);
  assert.equal(result.attempts, 2);
  assert.equal(result.elapsedMs, 10000);
  assert.deepEqual(result.downloads, [payload]);
});
