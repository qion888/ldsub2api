function count(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : fallback;
}

function metric(label, value, tone = '') {
  return {label, value: String(value), tone};
}

export function buildSub2ApiImportNotice(result) {
  const upstream = result?.result && typeof result.result === 'object' ? result.result : {};
  const verification = result?.import_verification;
  const fingerprint = result?.fingerprint_verification;
  const expected = count(verification?.expected);
  const accepted = count(
    upstream.success ?? upstream.account_created ?? verification?.accepted,
    expected,
  );
  const failed = count(upstream.failed ?? upstream.account_failed ?? verification?.failed);
  const matched = count(verification?.matched);
  const unresolved = count(fingerprint?.unresolved);
  const fingerprintError = String(fingerprint?.error || '').trim();
  const confirmed = Boolean(verification?.confirmed);
  const type = failed || fingerprintError ? 'error' : (!confirmed || unresolved ? 'warning' : 'success');

  const sections = [{
    title: '账号导入',
    badge: `HTTP ${result?.upstream_status ?? '--'}`,
    metrics: [
      metric('成功', accepted, 'positive'),
      metric('失败', failed, failed ? 'negative' : ''),
      metric('新增核验', verification ? `${matched}/${expected}` : '待刷新', confirmed ? 'positive' : 'warning'),
    ],
  }];

  if (fingerprint) {
    const repaired = count(fingerprint.repaired);
    const verified = count(fingerprint.verified);
    sections.push({
      title: '指纹核验',
      badge: String(fingerprint.mode || 'off'),
      metrics: [
        metric('符合', count(fingerprint.eligible)),
        metric('已核对', count(fingerprint.matched)),
        metric('直接生效', Math.max(0, verified - repaired), 'positive'),
        metric('补写', repaired, repaired ? 'warning' : ''),
        metric('未匹配', unresolved, unresolved ? 'negative' : ''),
      ],
      detail: fingerprintError ? `核对失败：${fingerprintError}` : '',
    });
  }

  return {
    type,
    title: confirmed ? '账号导入完成' : '账号导入需要确认',
    message: confirmed
      ? `已确认 ${matched} 个新增账号，导入与指纹结果已分别核对。`
      : verification
        ? `接口已返回，但仅确认 ${matched}/${expected} 个新增账号，请检查账号列表。`
        : '接口已返回，但没有账号核验结果，请刷新账号列表确认。',
    sections,
    duration: type === 'success' ? 9000 : 12000,
  };
}

export function sub2ApiHistoryDeleteErrorMessage(error, batch = false) {
  const detail = String(error?.message || '').trim();
  if (Number(error?.status) === 404 && /接口不存在/.test(detail)) {
    return batch
      ? '批量删除接口尚未加载，请重启本项目服务后重试'
      : '单条删除接口尚未加载，请重启本项目服务后重试';
  }
  return detail || (batch ? '批量删除导入记录失败' : '删除导入记录失败');
}
