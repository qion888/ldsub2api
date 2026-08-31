const STATUS_LABELS = {
  ready: '可检查更新',
  blocked: '暂不可更新',
  up_to_date: '已是最新',
  update_available: '发现新版本',
  local_ahead: '本地版本领先',
  diverged: '分支已分叉',
  comparison_unavailable: '版本关系待确认',
  updated: '等待重启',
};

function safeGithubUrl(value) {
  const url = String(value || '').trim();
  return /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/?$/.test(url) ? url.replace(/\/$/, '') : '';
}

function nonNegativeInteger(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.trunc(number)) : 0;
}

export function normalizeVersionInfo(value) {
  const source = value && typeof value === 'object' ? value : {};
  const currentCommit = String(source.current_commit || '').trim();
  const latestCommit = String(source.latest_commit || '').trim();
  const release = source.release && typeof source.release === 'object' ? source.release : null;
  const updateAvailable = Boolean(source.update_available);
  const canUpdate = Boolean(source.can_update);
  return {
    ok: Boolean(source.ok),
    status: String(source.status || 'ready'),
    message: String(source.message || '').trim(),
    current_version: String(source.current_version || 'dev').trim(),
    latest_version: String(source.latest_version || '').trim(),
    current_commit: currentCommit,
    current_short_commit: String(source.current_short_commit || currentCommit.slice(0, 8)).trim(),
    latest_commit: latestCommit,
    latest_short_commit: String(source.latest_short_commit || latestCommit.slice(0, 8)).trim(),
    branch: String(source.branch || '').trim(),
    target_branch: String(source.target_branch || 'main').trim(),
    repository_url: safeGithubUrl(source.repository_url),
    worktree_clean: source.worktree_clean !== false,
    dirty_file_count: nonNegativeInteger(source.dirty_file_count),
    repository_matches: source.repository_matches !== false,
    can_update: canUpdate,
    update_available: updateAvailable,
    update_ready: source.update_ready !== undefined
      ? Boolean(source.update_ready)
      : updateAvailable && canUpdate,
    updated: Boolean(source.updated),
    needs_restart: Boolean(source.needs_restart),
    ahead_by: nonNegativeInteger(source.ahead_by),
    behind_by: nonNegativeInteger(source.behind_by),
    checked_at: source.checked_at || null,
    release: release ? {
      tag: String(release.tag || '').trim(),
      name: String(release.name || '').trim(),
      published_at: release.published_at || null,
      url: safeGithubUrl(release.url),
    } : null,
  };
}

export function versionStatusLabel(value) {
  const info = normalizeVersionInfo(value);
  return STATUS_LABELS[info.status] || '版本状态未知';
}

export function versionBlockReason(value) {
  const info = normalizeVersionInfo(value);
  if (!info.repository_matches) return '当前 origin 与更新仓库不一致';
  if (info.branch !== info.target_branch) return `请切换到 ${info.target_branch} 分支后更新`;
  if (!info.worktree_clean) return `工作树有 ${info.dirty_file_count} 项未提交改动`;
  if (info.status === 'diverged') return '本地分支与 GitHub 主分支已分叉';
  if (info.status === 'local_ahead') return '本地版本领先于 GitHub 主分支';
  if (info.status === 'comparison_unavailable') return '暂时无法确认本地与 GitHub 的快进关系';
  return '';
}
