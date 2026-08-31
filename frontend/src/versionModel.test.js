import test from 'node:test';
import assert from 'node:assert/strict';

import {normalizeVersionInfo, versionBlockReason, versionStatusLabel} from './versionModel.js';


test('normalizes update metadata and safe GitHub links', () => {
  const info = normalizeVersionInfo({
    ok: true,
    status: 'update_available',
    current_version: '2.1.0',
    latest_version: '2.2.0',
    current_commit: 'a'.repeat(40),
    latest_commit: 'b'.repeat(40),
    branch: 'main',
    target_branch: 'main',
    repository_url: 'https://github.com/qion888/ldsub2api/',
    can_update: true,
    update_available: true,
    ahead_by: '3',
  });

  assert.equal(info.current_short_commit, 'aaaaaaaa');
  assert.equal(info.latest_short_commit, 'bbbbbbbb');
  assert.equal(info.repository_url, 'https://github.com/qion888/ldsub2api');
  assert.equal(info.ahead_by, 3);
  assert.equal(info.update_ready, true);
  assert.equal(versionStatusLabel(info), '发现新版本');
});

test('rejects unsafe links and explains update blockers in priority order', () => {
  const info = normalizeVersionInfo({
    repository_url: 'javascript:alert(1)',
    branch: 'codex/work',
    target_branch: 'main',
    worktree_clean: false,
    dirty_file_count: 2,
  });

  assert.equal(info.repository_url, '');
  assert.equal(versionBlockReason(info), '请切换到 main 分支后更新');
  assert.equal(versionBlockReason({...info, branch: 'main'}), '工作树有 2 项未提交改动');
});

test('preserves restart status after a completed update', () => {
  const info = normalizeVersionInfo({status: 'updated', updated: true, needs_restart: true});
  assert.equal(info.updated, true);
  assert.equal(info.needs_restart, true);
  assert.equal(versionStatusLabel(info), '等待重启');
});
