import test from 'node:test';
import assert from 'node:assert/strict';

import {normalizeBackupList, normalizeVersionInfo, versionBlockReason, versionStatusLabel} from './versionModel.js';


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
  assert.equal(info.remote_ahead_by, 3);
  assert.equal(info.local_ahead_by, 0);
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

test('identifies a GitHub download installation without treating it as a git checkout', () => {
  const info = normalizeVersionInfo({installation_mode: 'archive', branch: 'main', can_update: true});
  assert.equal(info.installation_mode, 'archive');
  assert.equal(info.branch, 'main');
  assert.equal(info.can_update, true);
});

test('normalizes database compatibility, backup history, and rollback metadata', () => {
  const info = normalizeVersionInfo({
    database: {
      path: 'C:/ldxp/backend/monitor.db',
      integrity: true,
      compatibility: 'sqlite-preserved',
      size_bytes: '2048',
      backup_count: '1',
      latest_backup: {id: 'latest-1', reason: 'before-update', size_bytes: 100},
    },
    backups: {
      ok: true,
      total: 1,
      items: [{id: 'backup-1', reason: 'manual', path: 'C:/ldxp/.runtime/version-backups/backup-1.sqlite3', sha256: 'a'.repeat(64), schema_version: 2}],
    },
    last_update: {backup_id: 'backup-1', previous_commit: 'a'.repeat(40), can_rollback: true},
  });

  assert.equal(info.database.compatibility, 'sqlite-preserved');
  assert.equal(info.database.path, 'C:/ldxp/backend/monitor.db');
  assert.equal(info.database.size_bytes, 2048);
  assert.equal(info.backups.total, 1);
  assert.equal(info.backups.items[0].id, 'backup-1');
  assert.equal(info.backups.items[0].path, 'C:/ldxp/.runtime/version-backups/backup-1.sqlite3');
  assert.equal(info.last_update.can_rollback, true);
  assert.equal(normalizeBackupList({items: [{id: 'x'}]}).latest.id, 'x');
});
