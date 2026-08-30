import React, {useEffect, useState} from 'react';
import {
  Activity,
  AlertCircle,
  ArrowUpRight,
  Check,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Clipboard,
  Database,
  Download,
  FileUp,
  Gauge,
  History,
  KeyRound,
  Layers3,
  Network,
  RefreshCw,
  Save,
  Search,
  Send,
  Settings2,
  ShieldCheck,
  TimerReset,
  Trash2,
  TriangleAlert,
  Upload,
  Wifi,
  X,
} from 'lucide-react';

function compactTime(value) {
  if (!value) return '尚未抓取';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date);
}

function intervalLabel(value) {
  const seconds = Number(value || 0);
  if (seconds < 60) return `${seconds}秒`;
  if (seconds % 60 === 0) return `${seconds / 60}分钟`;
  return `${seconds}秒`;
}

function IconButton({label, children, tone = '', ...props}) {
  return <button className={`icon-button ${tone}`} aria-label={label} title={label} {...props}>{children}</button>;
}

function Tooltip({label, children}) {
  if (!label) return children;
  const child = React.Children.only(children);
  return React.cloneElement(child, {
    className: [child.props.className, 'tooltip-anchor'].filter(Boolean).join(' '),
    title: undefined,
    'data-tooltip': label,
    'data-tooltip-placement': 'top',
  });
}

function formatSub2ApiNumber(value, maximumFractionDigits = 2) {
  if (value === null || value === undefined || value === '') return '--';
  const number = Number(value);
  return Number.isFinite(number)
    ? number.toLocaleString('zh-CN', {maximumFractionDigits})
    : String(value);
}

function formatSub2ApiCompactNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '--';
  return new Intl.NumberFormat('zh-CN', {notation: 'compact', maximumFractionDigits: 1}).format(number);
}

function formatSub2ApiDateTime(value) {
  if (!value) return '未记录';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
}

function formatSub2ApiResetCountdown(value, remainingSeconds) {
  const hasRemainingSeconds = remainingSeconds !== null && remainingSeconds !== undefined && remainingSeconds !== '';
  const providedSeconds = hasRemainingSeconds ? Number(remainingSeconds) : NaN;
  const resetTimestamp = value ? new Date(value).getTime() : NaN;
  const seconds = Number.isFinite(providedSeconds) && providedSeconds >= 0
    ? providedSeconds
    : Number.isFinite(resetTimestamp) ? Math.max(0, (resetTimestamp - Date.now()) / 1000) : NaN;
  if (!Number.isFinite(seconds)) return '未提供重置时间';
  if (seconds <= 0) return '即将重置';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `剩余 ${days} 天 ${hours} 小时`;
  if (hours > 0) return `剩余 ${hours} 小时 ${minutes} 分钟`;
  if (minutes > 0) return `剩余 ${minutes} 分钟`;
  return `剩余 ${Math.max(1, Math.ceil(seconds))} 秒`;
}

function sub2ApiFutureTime(value, now = Date.now()) {
  if (!value) return false;
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) && timestamp > now;
}

function sub2ApiQuotaExceeded(account) {
  const quotas = [
    ['总额度', account.quota_used, account.quota_limit],
    ['日额度', account.quota_daily_used, account.quota_daily_limit],
    ['周额度', account.quota_weekly_used, account.quota_weekly_limit],
  ];
  return quotas.find(([, used, limit]) => Number(limit) > 0 && Number(used) >= Number(limit));
}

function sub2ApiAccountState(account) {
  if (sub2ApiFutureTime(account.rate_limit_reset_at)) {
    return {key: 'limited', label: '限流中', tone: 'warning', detail: `429 · ${formatSub2ApiDateTime(account.rate_limit_reset_at)} 恢复`};
  }
  if (sub2ApiFutureTime(account.overload_until)) {
    return {key: 'overloaded', label: '上游过载', tone: 'danger', detail: `529 · ${formatSub2ApiDateTime(account.overload_until)} 后重试`};
  }
  if (sub2ApiFutureTime(account.temp_unschedulable_until)) {
    const reason = account.temp_unschedulable_reason ? `${String(account.temp_unschedulable_reason).slice(0, 80)} · ` : '';
    return {key: 'temporary', label: '暂不可用', tone: 'warning', detail: `${reason}${formatSub2ApiDateTime(account.temp_unschedulable_until)} 恢复`};
  }
  if (account.status === 'error' || account.error_message) {
    return {key: 'error', label: '异常', tone: 'danger', detail: String(account.error_message || 'Sub2API 标记账号异常').slice(0, 120)};
  }
  const exceeded = sub2ApiQuotaExceeded(account);
  if (exceeded) {
    return {key: 'quota', label: '额度耗尽', tone: 'warning', detail: `${exceeded[0]}已达到上限`};
  }
  if (account.schedulable === false) {
    return {key: 'paused', label: '暂停调度', tone: 'muted', detail: 'Sub2API 当前不会为该账号分配请求'};
  }
  if (account.status === 'active') {
    return {key: 'active', label: '正常', tone: 'success', detail: '状态正常，可参与调度'};
  }
  if (account.status === 'inactive') {
    return {key: 'inactive', label: '已停用', tone: 'muted', detail: '账号已在 Sub2API 停用'};
  }
  return {key: 'unknown', label: account.status || '未知', tone: 'muted', detail: '等待 Sub2API 返回明确状态'};
}

function Sub2ApiProgress({label, used, limit, resetsAt, testId}) {
  const numericUsed = Number(used);
  const numericLimit = Number(limit);
  const ratio = Number.isFinite(numericLimit) && numericLimit > 0 && Number.isFinite(numericUsed)
    ? (numericUsed / numericLimit) * 100
    : null;
  const rawPercent = ratio;
  const hasPercent = Number.isFinite(rawPercent);
  const displayPercent = hasPercent ? Math.max(0, rawPercent) : 0;
  const meterPercent = Math.min(displayPercent, 100);
  const tone = displayPercent >= 100 ? 'danger' : displayPercent >= 80 ? 'warning' : 'normal';
  const exactValue = Number.isFinite(numericLimit) && numericLimit > 0
    ? `${formatSub2ApiNumber(numericUsed || 0)} / ${formatSub2ApiNumber(numericLimit)}`
    : hasPercent ? `${formatSub2ApiNumber(displayPercent, 1)}%` : '未设置';
  return <div className={`account-progress ${tone}`} data-testid={testId}>
    <div className="account-progress-head"><span>{label}</span><strong>{exactValue}</strong></div>
    <div className="account-progress-track" role="progressbar" aria-label={`${label}使用率`} aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round(meterPercent)} aria-valuetext={hasPercent ? `${displayPercent.toFixed(1)}%` : '未设置'}><span style={{width: `${meterPercent}%`}}/></div>
    <div className="account-progress-meta"><span>{hasPercent ? `${displayPercent.toFixed(1)}%` : '--'}</span><small>{resetsAt ? `${compactTime(resetsAt)} 重置` : '无重置时间'}</small></div>
  </div>;
}

function Sub2ApiQuotaStack({account}) {
  return <div className="account-progress-stack">
    <Sub2ApiProgress label="总额度" used={account.quota_used} limit={account.quota_limit} testId={`quota-total-${account.id}`}/>
    <Sub2ApiProgress label="日额度" used={account.quota_daily_used} limit={account.quota_daily_limit} resetsAt={account.quota_daily_reset_at} testId={`quota-daily-${account.id}`}/>
    <Sub2ApiProgress label="周额度" used={account.quota_weekly_used} limit={account.quota_weekly_limit} resetsAt={account.quota_weekly_reset_at} testId={`quota-weekly-${account.id}`}/>
  </div>;
}

function Sub2ApiUsageWindow({label, window, tone, testId}) {
  const stats = window?.window_stats && typeof window.window_stats === 'object' ? window.window_stats : {};
  const numericUtilization = Number(window?.utilization ?? window?.used_percent);
  const numericUsed = Number(window?.used_requests ?? window?.used);
  const numericLimit = Number(window?.limit_requests ?? window?.limit);
  const ratio = Number.isFinite(numericLimit) && numericLimit > 0 && Number.isFinite(numericUsed)
    ? (numericUsed / numericLimit) * 100
    : null;
  const rawPercent = Number.isFinite(numericUtilization) ? numericUtilization : ratio;
  const hasPercent = Number.isFinite(rawPercent);
  const displayPercent = hasPercent ? Math.max(0, rawPercent) : 0;
  const meterPercent = Math.min(displayPercent, 100);
  const state = displayPercent >= 100 ? 'danger' : displayPercent >= 80 ? 'warning' : 'normal';
  const hasNumber = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  const requests = hasNumber(stats.requests) ? stats.requests : hasNumber(window?.used_requests) ? window.used_requests : null;
  const requestValue = hasNumber(requests)
    ? hasNumber(window?.limit_requests) && Number(window.limit_requests) > 0
      ? `${formatSub2ApiCompactNumber(requests)} / ${formatSub2ApiCompactNumber(window.limit_requests)}`
      : formatSub2ApiCompactNumber(requests)
    : null;
  const detailItems = [
    requestValue && {label: '请求', value: requestValue},
    hasNumber(stats.tokens) && {label: 'Token', value: formatSub2ApiCompactNumber(stats.tokens)},
    hasNumber(stats.cost) && {label: '账号费用', value: `$${Number(stats.cost).toFixed(2)}`},
    hasNumber(stats.user_cost) && {label: '用户费用', value: `$${Number(stats.user_cost).toFixed(2)}`},
  ].filter(Boolean);
  const resetsAt = window?.resets_at ?? window?.reset_at;
  const hasWindowData = hasPercent || detailItems.length > 0 || Boolean(resetsAt);
  const resetLabel = formatSub2ApiResetCountdown(resetsAt, window?.remaining_seconds);
  const exactResetLabel = resetsAt ? `准确重置时间：${formatSub2ApiDateTime(resetsAt)}` : resetLabel;
  return <section className={`account-usage-window ${tone} ${state}`} data-testid={testId}>
    <div className="account-usage-window-head">
      <span><Clock3 size={15}/><strong>{label}</strong></span>
      <strong className="account-usage-percent">{hasPercent ? `${displayPercent.toFixed(1)}%` : '--'}</strong>
    </div>
    {hasWindowData ? <>
      <div
        className="account-usage-track"
        role="progressbar"
        aria-label={`${label}使用率`}
        aria-valuemin="0"
        aria-valuemax="100"
        aria-valuenow={hasPercent ? Math.round(meterPercent) : undefined}
        aria-valuetext={hasPercent ? `${displayPercent.toFixed(1)}%` : '暂无使用率'}
      ><span style={{width: `${meterPercent}%`}}/></div>
      <div className="account-usage-footnote">
        <Tooltip label={exactResetLabel}><span className="account-usage-reset"><TimerReset size={12}/>{resetLabel}</span></Tooltip>
        {hasNumber(stats.standard_cost) && <Tooltip label="标准费用不含账号倍率，用于和账号费用、用户结算费用核对"><span className="account-usage-standard">标准成本 ${Number(stats.standard_cost).toFixed(2)}</span></Tooltip>}
      </div>
      {detailItems.length > 0
        ? <dl className="account-usage-details">{detailItems.map(item => <div key={item.label}><dt>{item.label}</dt><dd>{item.value}</dd></div>)}</dl>
        : <div className="account-usage-window-empty">暂无请求与费用明细</div>}
    </> : <div className="account-usage-window-empty">暂未返回此窗口数据</div>}
  </section>;
}

function Sub2ApiUsageStack({account, usage, usageError}) {
  const accountUsage = usage[String(account.id)] || usage[account.id] || {};
  const windowFor = key => ({...(account?.[key] || {}), ...(accountUsage?.[key] || {})});
  const fiveHour = windowFor('five_hour');
  const sevenDay = windowFor('seven_day');
  const sourceLabels = {active: '主动查询', live: '主动查询', passive: '被动采样'};
  const sourceLabel = sourceLabels[String(accountUsage.source || '').toLowerCase()] || (Object.keys(accountUsage).length ? '上游数据' : '账号快照');
  const updatedAt = accountUsage.updated_at || account.updated_at;
  return <div className="account-usage-stack upstream">
    <div className="account-usage-stack-meta">
      <span><Activity size={13}/>{sourceLabel}</span>
      {updatedAt && <Tooltip label={`数据时间：${formatSub2ApiDateTime(updatedAt)}`}><small>更新 {compactTime(updatedAt)}</small></Tooltip>}
    </div>
    <div className="account-usage-windows">
      <Sub2ApiUsageWindow label="5 小时窗口" window={fiveHour} tone="five-hour" testId={`usage-5h-${account.id}`}/>
      <Sub2ApiUsageWindow label="7 天窗口" window={sevenDay} tone="seven-day" testId={`usage-7d-${account.id}`}/>
    </div>
    {usageError && <small className="account-usage-error"><AlertCircle size={13}/>{usageError}</small>}
  </div>;
}

function Sub2ApiAccountsPanel({data, filters, onFiltersChange, busy, error, actions, tested, onRefresh, onTest, onDelete, onCopyName, onPage}) {
  const items = Array.isArray(data?.items) ? data.items : [];
  const usage = data?.usage && typeof data.usage === 'object' ? data.usage : {};
  const usageErrors = data?.usage_errors && typeof data.usage_errors === 'object' ? data.usage_errors : {};
  return (
    <section className="account-management-panel">
      <div className="account-management-head">
        <div><span className="detail-kicker">SUB2API ACCOUNTS</span><h3>账号列表</h3><p>账号费用、上游窗口与实时调度状态</p></div>
        <div className="account-management-actions"><label className="account-search"><Search size={15}/><input value={filters.search} onChange={event => onFiltersChange({search: event.target.value})} placeholder="搜索账号名称"/></label><select value={filters.status} onChange={event => onFiltersChange({status: event.target.value})}><option value="">全部状态</option><option value="active">正常</option><option value="inactive">停用</option><option value="error">异常</option></select><select value={filters.platform} onChange={event => onFiltersChange({platform: event.target.value})}><option value="">全部平台</option><option value="openai">OpenAI</option><option value="anthropic">Anthropic</option><option value="google">Google</option></select><IconButton label="刷新账号列表" onClick={() => onRefresh({page: data?.page || 1})} disabled={busy}><RefreshCw size={16} className={busy ? 'spin' : ''}/></IconButton></div>
      </div>
      <div className="account-table-wrap">
        <table className="account-table">
          <colgroup>
            <col className="account-col-identity"/>
            <col className="account-col-platform"/>
            <col className="account-col-state"/>
            <col className="account-col-quota"/>
            <col className="account-col-usage"/>
            <col className="account-col-routing"/>
            <col className="account-col-actions"/>
          </colgroup>
          <thead><tr><th scope="col">账号信息</th><th scope="col">平台计费</th><th scope="col">调度状态</th><th scope="col">本地额度</th><th scope="col">上游用量</th><th scope="col">路由归属</th><th scope="col">操作</th></tr></thead>
          <tbody>
          {items.length ? items.map(account => {
            const id = Number(account.id);
            const test = tested[id];
            const state = sub2ApiAccountState(account);
            const accountGroups = Array.isArray(account.groups) ? account.groups.map(group => group.name).filter(Boolean).join('、') : '';
            const accountProxy = account.proxy?.name || (account.proxy_id ? `代理 #${account.proxy_id}` : '无代理');
            const multiplier = Number(account.rate_multiplier ?? 1);
            const concurrency = `${formatSub2ApiNumber(account.current_concurrency ?? 0, 0)} / ${formatSub2ApiNumber(account.concurrency ?? 0, 0)}`;
            const usageError = usageErrors[String(account.id)] || usageErrors[account.id] || usageErrors._;
            const accountName = account.name || `账号 ${account.id}`;
            return <tr key={account.id} data-account-state={state.key}>
              <td className="account-identity-cell" data-label="账号信息">
                <div className="account-identity">
                  <div className="account-name-line"><strong title={accountName}>{accountName}</strong><IconButton label={`复制账号名称：${accountName}`} onClick={() => onCopyName(account)}><Clipboard size={13}/></IconButton></div>
                  <div className="account-identity-meta"><span>#{account.id}</span><span>创建 {formatSub2ApiDateTime(account.created_at)}</span></div>
                  <small>{account.last_used_at ? `最近使用 ${compactTime(account.last_used_at)}` : `更新于 ${compactTime(account.updated_at)}`}</small>
                </div>
              </td>
              <td className="account-platform-cell" data-label="平台计费">
                <div className="account-platform-block">
                  <div className="account-platform-name"><strong>{account.platform || '--'}</strong><span>{account.type || '--'}</span></div>
                  <Tooltip label="账号费用 = 标准费用 × 账号倍率"><span className="account-billing-rate">{Number.isFinite(multiplier) && multiplier === 0 ? '免费计费' : `账号倍率 ×${Number.isFinite(multiplier) ? multiplier.toFixed(2) : '--'}`}</span></Tooltip>
                  <div className="account-concurrency"><span>并发</span><strong>{concurrency}</strong></div>
                </div>
              </td>
              <td className="account-state-cell" data-label="调度状态"><div className="account-state-block"><span className={`account-status ${state.tone}`}>{state.label}</span><small className="account-status-detail" title={state.detail}>{state.detail}</small></div></td>
              <td className="account-quota-cell" data-label="本地额度"><Sub2ApiQuotaStack account={account}/></td>
              <td className="account-usage-cell" data-label="上游用量"><Sub2ApiUsageStack account={account} usage={usage} usageError={usageError}/></td>
              <td className="account-routing-cell" data-label="路由归属"><div className="account-routing"><strong>{accountProxy}</strong><small>{accountGroups || (Array.isArray(account.group_ids) ? `${account.group_ids.length} 个分组` : '未分组')}</small></div></td>
              <td className="account-action-cell" data-label="操作"><div className="account-action-stack"><div className="account-row-actions"><IconButton label={test ? '重新测试账号' : '测试账号'} onClick={() => onTest(account)} disabled={actions[id] === 'test'} tone={test?.ok ? 'success' : ''}>{actions[id] === 'test' ? <RefreshCw size={15} className="spin"/> : test?.ok ? <Check size={15}/> : <Activity size={15}/>}</IconButton><IconButton label="删除账号" tone="danger" onClick={() => onDelete(account)} disabled={actions[id] === 'delete'}>{actions[id] === 'delete' ? <RefreshCw size={15} className="spin"/> : <Trash2 size={15}/>}</IconButton></div>{test && <small className={`account-test-result ${test.ok ? 'ok' : 'bad'}`}>{test.ok ? `测试通过 · ${compactTime(test.tested_at)}` : (test.message || '测试失败')}</small>}</div></td>
            </tr>;
          }) : <tr className="account-empty-row"><td colSpan="7"><div className="account-table-empty">{busy ? <RefreshCw size={20} className="spin"/> : <Database size={20}/>}<span>{busy ? '正在加载账号列表' : error || '暂无匹配账号'}</span></div></td></tr>}
          </tbody>
        </table>
      </div>
      <div className="account-pagination"><span>共 {data?.total ?? 0} 个账号 · 第 {data?.page || 1} / {data?.pages || 1} 页</span><div><IconButton label="上一页" onClick={() => onPage(Math.max(1, (data?.page || 1) - 1))} disabled={busy || (data?.page || 1) <= 1}><ChevronLeft size={16}/></IconButton><IconButton label="下一页" onClick={() => onPage(Math.min(data?.pages || 1, (data?.page || 1) + 1))} disabled={busy || (data?.page || 1) >= (data?.pages || 1)}><ChevronRight size={16}/></IconButton></div></div>
    </section>
  );
}

function CardImportHistoryPanel({data, filter, onFilter, page, pageSize, onPage, onPageSize, busy, actions, onRefresh, onRetry, onDelete, onCopyCode}) {
  const items = Array.isArray(data?.items) ? data.items : [];
  const [selectedIds, setSelectedIds] = useState([]);
  const summary = data?.summary || {total: 0, success: 0, failed: 0, pending: 0, successful_accounts: 0, failed_accounts: 0};
  const filters = [
    {value: 'all', label: '全部', count: summary.total},
    {value: 'success', label: '成功', count: summary.success},
    {value: 'failed', label: '失败', count: summary.failed},
    {value: 'pending', label: '待推送', count: summary.pending},
  ];
  const statusMeta = {
    success: {label: '推送成功', icon: Check},
    failed: {label: '推送失败', icon: TriangleAlert},
    pending: {label: '等待推送', icon: Clock3},
    running: {label: '正在处理', icon: RefreshCw},
  };
  const pages = Math.max(1, Number(data?.pages || 1));
  const currentPage = Math.min(pages, Math.max(1, Number(data?.page || page || 1)));
  const total = Number(data?.total || 0);
  const pageStart = total ? ((currentPage - 1) * Number(data?.page_size || pageSize || 10)) + 1 : 0;
  const pageEnd = Math.min(total, currentPage * Number(data?.page_size || pageSize || 10));
  const pageIds = items.map(record => Number(record.id)).filter(Number.isInteger);
  const allSelected = pageIds.length > 0 && pageIds.every(recordId => selectedIds.includes(recordId));
  const batchDeleting = actions?.batchDelete === 'delete';
  useEffect(() => {
    setSelectedIds(current => current.filter(recordId => pageIds.includes(recordId)));
  }, [data]);
  const toggleRecord = recordId => setSelectedIds(current => current.includes(recordId) ? current.filter(value => value !== recordId) : [...current, recordId]);
  const togglePage = () => setSelectedIds(allSelected ? [] : pageIds);
  const deleteRecords = async records => {
    const deleted = await onDelete(records);
    if (deleted) setSelectedIds([]);
  };
  const recordDetail = record => {
    const accountIds = Array.isArray(record.details?.new_account_ids) ? record.details.new_account_ids : [];
    const missing = Array.isArray(record.details?.missing_accounts) ? record.details.missing_accounts : [];
    if (record.status === 'success' && accountIds.length) return `新增账号 #${accountIds.slice(0, 6).join('、#')}`;
    if (record.status === 'failed' && missing.length) return `未确认：${missing.slice(0, 3).map(item => item.name || item.platform || '账号').join('、')}`;
    return record.message || '暂无补充信息';
  };

  return (
    <div className="card-import-history" data-testid="card-import-history">
      <div className="card-history-head">
        <div><span className="detail-kicker">IMPORT AUDIT</span><h3><History size={17}/>导入记录</h3><p>保留最近 500 次核验、卡密与推送结果</p></div>
        <button className="icon-button" type="button" onClick={onRefresh} disabled={busy} title="刷新导入记录" aria-label="刷新导入记录"><RefreshCw size={15} className={busy ? 'spin' : ''}/></button>
      </div>
      <div className="card-history-summary">
        <div><span>记录总数</span><strong>{summary.total ?? 0}</strong><small>最近 500 次</small></div>
        <div><span>推送成功</span><strong className="positive">{summary.success ?? 0}</strong><small>{summary.successful_accounts ?? 0} 个账号</small></div>
        <div><span>推送失败</span><strong className={summary.failed ? 'negative' : ''}>{summary.failed ?? 0}</strong><small>{summary.failed_accounts ?? 0} 个账号</small></div>
        <div><span>处理中 / 待推送</span><strong>{summary.pending ?? 0}</strong><small>可继续手动推送</small></div>
      </div>
      <div className="card-history-toolbar">
        <div className="card-history-filters" role="tablist" aria-label="导入记录状态筛选">
          {filters.map(item => <button type="button" role="tab" aria-selected={filter === item.value} className={filter === item.value ? 'active' : ''} onClick={() => onFilter(item.value)} key={item.value}>{item.label}<span>{item.count ?? 0}</span></button>)}
        </div>
        <div className="card-history-toolbar-actions"><span>{selectedIds.length ? `已选 ${selectedIds.length} 条` : total ? `当前 ${pageStart}-${pageEnd} / ${total} 条` : '当前没有记录'}</span>{selectedIds.length > 0 && <button className="icon-button" type="button" onClick={() => setSelectedIds([])} disabled={busy || batchDeleting} title="取消选择" aria-label="取消选择"><X size={15}/></button>}<button className="button danger-button" type="button" onClick={() => deleteRecords(items.filter(record => selectedIds.includes(Number(record.id))))} disabled={busy || batchDeleting || !selectedIds.length}>{batchDeleting ? <RefreshCw size={14} className="spin"/> : <Trash2 size={14}/>}<span>{batchDeleting ? '正在删除' : '批量删除'}</span></button></div>
      </div>
      <div className={`card-history-table-wrap ${busy ? 'is-loading' : ''}`} role="region" tabIndex={0} aria-label="可横向滚动的卡密导入记录" aria-busy={busy}>
        <div className="card-history-table" role="table" aria-label="卡密导入记录">
          <div className="card-history-row head" role="row"><span role="columnheader" className="card-history-select"><input type="checkbox" checked={allSelected} onChange={togglePage} disabled={busy || !pageIds.length} aria-label="选择当前页全部导入记录" title="选择当前页全部导入记录"/></span><span role="columnheader">状态</span><span role="columnheader">时间 / 模式</span><span role="columnheader">卡密</span><span role="columnheader">下载 / 账号</span><span role="columnheader">推送结果</span><span role="columnheader">详情</span><span role="columnheader">操作</span></div>
          {items.length ? items.map(record => {
            const meta = statusMeta[record.status] || statusMeta.running;
            const StatusIcon = meta.icon;
            const cardCodes = Array.isArray(record.card_codes) ? record.card_codes : [];
            const retryVisible = ['failed', 'pending'].includes(record.status);
            const retryBusy = actions?.[record.id] === 'retry';
            const deleteBusy = actions?.[record.id] === 'delete';
            return <div className={`card-history-row ${record.status} ${selectedIds.includes(Number(record.id)) ? 'selected' : ''}`} role="row" key={record.id}>
              <span role="cell" className="card-history-select"><input type="checkbox" checked={selectedIds.includes(Number(record.id))} onChange={() => toggleRecord(Number(record.id))} disabled={busy || retryBusy || deleteBusy || batchDeleting} aria-label={`选择导入记录 #${record.id}`}/></span>
              <span role="cell" className={`card-history-status ${record.status}`}><StatusIcon size={14} className={record.status === 'running' ? 'spin' : ''}/><strong>{meta.label}</strong></span>
              <span role="cell"><strong>{compactTime(record.completed_at || record.updated_at || record.started_at)}</strong><small>{record.mode === 'auto' ? '自动推送' : '手动推送'} · #{record.id}</small></span>
              <span role="cell" className="card-history-codes">{cardCodes.length ? <span className="card-code-list">{cardCodes.map(code => <button type="button" onClick={() => onCopyCode(code)} title={`复制卡密 ${code}`} aria-label={`复制卡密 ${code}`} key={code}><code>{code}</code><Clipboard size={12}/></button>)}</span> : <small>旧记录未保存卡密</small>}</span>
              <span role="cell"><strong>{record.downloaded_files} 个文件 · {record.account_count} 个账号</strong><small>已核验 {record.verified_count} / {record.card_count} 张卡密</small></span>
              <span role="cell"><strong><em className="positive">{record.success_count}</em> 成功 · <em className={record.failed_count ? 'negative' : ''}>{record.failed_count}</em> 失败</strong><small>待推送 {Math.max(0, Number(record.account_count || 0) - Number(record.success_count || 0))}</small></span>
              <span role="cell" title={recordDetail(record)}><strong>{record.message || meta.label}</strong><small>{recordDetail(record)}</small></span>
              <span role="cell" className="card-history-actions">{retryVisible && <button className="button secondary" type="button" onClick={() => onRetry(record)} disabled={busy || retryBusy || deleteBusy || batchDeleting || !record.retryable} title={record.retryable ? '使用原导入参数重新推送' : '该记录尚未保存可重推的账号数据'}>{retryBusy ? <RefreshCw size={13} className="spin"/> : <Send size={13}/>}<span>{retryBusy ? '推送中' : '重新推送'}</span></button>}<button className="icon-button danger-icon" type="button" onClick={() => deleteRecords(record)} disabled={busy || retryBusy || deleteBusy || batchDeleting} title={`删除导入记录 #${record.id}`} aria-label={`删除导入记录 #${record.id}`}>{deleteBusy ? <RefreshCw size={14} className="spin"/> : <Trash2 size={14}/>}</button></span>
            </div>;
          }) : <div className="card-history-empty">{busy ? <RefreshCw size={20} className="spin"/> : <History size={20}/>}<span>{busy ? '正在读取导入记录' : '当前筛选下暂无导入记录'}</span></div>}
        </div>
      </div>
      <div className="card-history-pagination"><label><span>每页</span><select value={pageSize} onChange={event => onPageSize(Number(event.target.value))} disabled={busy}>{[10, 20, 50].map(value => <option value={value} key={value}>{value} 条</option>)}</select></label><span>第 {currentPage} / {pages} 页</span><div><button className="icon-button" type="button" onClick={() => onPage(Math.max(1, currentPage - 1))} disabled={busy || currentPage <= 1} title="上一页" aria-label="上一页"><ChevronLeft size={16}/></button><button className="icon-button" type="button" onClick={() => onPage(Math.min(pages, currentPage + 1))} disabled={busy || currentPage >= pages} title="下一页" aria-label="下一页"><ChevronRight size={16}/></button></div></div>
    </div>
  );
}

function Sub2ApiCardImportPanel({redeemConfig, setRedeemConfig, onSaveRedeem, codes, onCodes, mode, onMode, busy, importing, flow, payload, canAutoPush, onRun, onPush, historyData, historyFilter, onHistoryFilter, historyPage, historyPageSize, onHistoryPage, onHistoryPageSize, historyBusy, historyActions, onRefreshHistory, onRetryHistory, onDeleteHistory, onCopyCode}) {
  const stageLabel = {
    idle: '等待输入',
    verify: '正在核验',
    reclaim: '正在提交',
    poll: '正在轮询',
    download: '正在下载',
    stage: '正在整理',
    waiting: '等待后续查询',
    ready: '等待推送',
    push: '正在推送',
    done: '推送完成',
    error: '处理异常',
  }[flow.stage] || '等待输入';
  const codesCount = [...new Set(codes.split(/[\s,，]+/).map(value => value.trim()).filter(Boolean))].length;
  const health = flow.health || {};
  const pushConfirmed = Boolean(flow.pushResult?.import_verification?.confirmed || flow.pushed);
  const isActive = busy || importing;
  const configuredRedeemUrl = /^https?:\/\//i.test(redeemConfig.base_url || '') ? redeemConfig.base_url : 'https://30d.team';

  return (
    <section className="card-import-console" data-testid="sub2api-card-import">
      <div className="card-import-head">
        <div><span className="detail-kicker">CARD DIRECT IMPORT</span><h2>卡密核验与推送</h2><p>独立于账号 JSON 导入和 401 定时监控</p></div>
        <a href={configuredRedeemUrl} target="_blank" rel="noreferrer"><span>{configuredRedeemUrl}</span><ArrowUpRight size={14}/></a>
      </div>
      <div className="card-import-body">
        <div className="card-import-inputs">
          <label><span>核验下载服务</span><div className="card-service-field"><input value={redeemConfig.base_url} onChange={event => setRedeemConfig({...redeemConfig, base_url: event.target.value})} placeholder="https://30d.team"/><button className="icon-button" type="button" onClick={onSaveRedeem} title="保存核验下载服务地址" aria-label="保存核验下载服务地址"><Save size={15}/></button></div></label>
          <label className="code-field"><span>卡密列表 <small>{codesCount}/100</small></span><textarea data-testid="sub2api-card-codes" value={codes} onChange={event => onCodes(event.target.value)} placeholder="每行输入一个卡密" rows={5}/></label>
        </div>
        <div className="card-import-control">
          <div className="card-mode-head"><span>完成后操作</span><div className="card-mode-segment" role="group" aria-label="卡密推送模式">
            <button type="button" className={mode === 'manual' ? 'active' : ''} onClick={() => onMode('manual')} data-testid="card-mode-manual"><Download size={14}/>手动推送</button>
            <button type="button" className={mode === 'auto' ? 'active' : ''} onClick={() => onMode('auto')} data-testid="card-mode-auto"><Send size={14}/>自动推送</button>
          </div></div>
          <div className="card-flow-steps" role="list" aria-label="卡密直导进度">
            <div role="listitem" className={flow.health ? 'done' : flow.stage === 'verify' ? 'active' : ''}><span>1</span><strong>核验卡密</strong><small>{flow.health ? `${health.total ?? codesCount} 个已核验` : '30d.team'}</small></div>
            <div role="listitem" className={flow.stage !== 'poll' && flow.downloaded ? 'done' : ['reclaim', 'poll', 'download', 'stage', 'waiting'].includes(flow.stage) ? 'active' : ''}><span>2</span><strong>找回下载</strong><small>{flow.stage === 'poll' ? `${flow.downloaded || 0} 文件 · ${flow.pollElapsedSeconds || 0}s / 60s` : flow.downloaded ? `${flow.downloaded} 个文件` : flow.stage === 'waiting' ? '可稍后重试' : '账号 JSON'}</small></div>
            <div role="listitem" className={pushConfirmed ? 'done' : flow.stage === 'push' ? 'active' : ''}><span>3</span><strong>{mode === 'auto' ? '自动推送' : '手动推送'}</strong><small>{pushConfirmed ? 'Sub2API 已核验' : mode === 'auto' ? '下载后执行' : '确认后执行'}</small></div>
          </div>
          <div className="card-flow-summary">
            <div><span>当前状态</span><strong className={flow.stage === 'error' ? 'negative' : ''}>{stageLabel}</strong></div>
            <div><span>核验总数</span><strong>{health.total ?? '--'}</strong></div>
            <div><span>轮询等待</span><strong>{flow.pollElapsedSeconds == null ? '--' : `${flow.pollElapsedSeconds}s / ${flow.pollTimeoutSeconds || 60}s`}</strong></div>
            <div><span>账号数量</span><strong>{flow.accounts ?? '--'}</strong></div>
          </div>
          {mode === 'auto' && !canAutoPush && <div className="card-flow-notice"><AlertCircle size={14}/><span>自动推送需要先保存 Sub2API 管理员密钥</span></div>}
          {flow.stage === 'poll' && <div className="card-flow-notice"><RefreshCw size={14} className="spin"/><span>每 5 秒查询一次；即使队列已清空，也会继续等待账号 JSON，最长 1 分钟</span></div>}
          {flow.notice && <div className="card-flow-notice"><Clock3 size={14}/><span>{flow.notice}</span></div>}
          {flow.error && <div className="card-flow-notice error"><TriangleAlert size={14}/><span>{flow.error}</span></div>}
          <div className="card-import-actions">
            <button className="button primary" type="button" onClick={onRun} disabled={isActive || !codesCount || (mode === 'auto' && !canAutoPush)} data-testid="run-card-import">{busy ? <RefreshCw size={15} className="spin"/> : mode === 'auto' ? <Send size={15}/> : <Download size={15}/>} {busy ? stageLabel : mode === 'auto' ? '核验、下载并推送' : '核验并下载'}</button>
            {flow.accounts && payload && !pushConfirmed && <button className="button secondary" type="button" onClick={onPush} disabled={isActive}><Upload size={15}/>手动推送当前 JSON</button>}
          </div>
        </div>
      </div>
      <CardImportHistoryPanel data={historyData} filter={historyFilter} onFilter={onHistoryFilter} page={historyPage} pageSize={historyPageSize} onPage={onHistoryPage} onPageSize={onHistoryPageSize} busy={historyBusy} actions={historyActions} onRefresh={onRefreshHistory} onRetry={onRetryHistory} onDelete={onDeleteHistory} onCopyCode={onCopyCode}/>
    </section>
  );
}

export default function Sub2ApiView({config, setConfig, adminKey, setAdminKey, redeemConfig, setRedeemConfig, onSaveRedeem, cardCodes, onCardCodes, cardMode, onCardMode, cardBusy, cardFlow, onRunCardImport, onPushCards, cardHistory, cardHistoryFilter, onCardHistoryFilter, cardHistoryPage, cardHistoryPageSize, onCardHistoryPage, onCardHistoryPageSize, cardHistoryBusy, cardHistoryActions, onRefreshCardHistory, onRetryCardHistory, onDeleteCardHistory, onCopyCardCode, fileName, payload, result, busy, optionsBusy, options, proxyChoice, groupIds, codexFingerprintMode, onCodexFingerprintMode, reclaimBusy, reclaimResult, onReclaim401, automation, automationState, automationBusy, onAutomationChange, onSaveAutomation, onRunAutomation, onSave, onTest, onLoadOptions, onProxyChoice, onToggleGroup, onFile, onFiles, onImport, accountsData, accountFilters, onAccountFiltersChange, accountBusy, accountError, accountActions, testedAccounts, onLoadAccounts, onTestAccount, onDeleteAccount, onCopyAccountName}) {
  const [dragging, setDragging] = useState(false);
  const accountCount = Array.isArray(payload?.accounts) ? payload.accounts.length : 0;
  const jsonProxyCount = Array.isArray(payload?.proxies) ? payload.proxies.length : 0;
  const monitor = options.monitor || {};
  const selectedProxy = options.proxies.find(proxy => `proxy:${proxy.id}` === proxyChoice);
  const selectedGroups = options.groups.filter(group => groupIds.includes(group.id));
  const activeGroups = options.groups.filter(group => !group.status || group.status === 'active');
  const automationReady = Boolean(config.admin_key_set && proxyChoice.startsWith('proxy:') && groupIds.length && codexFingerprintMode !== 'off' && automation.auto_import);
  const automationResult = automationState?.last_result;
  const runHistory = Array.isArray(automationState?.run_history) ? [...automationState.run_history].reverse() : [];
  const recentErrors = Array.isArray(monitor.recent_errors) ? monitor.recent_errors : [];
  const platforms = Array.isArray(monitor.platforms) ? monitor.platforms : [];
  const nextRun = automation.enabled && automationState?.last_run
    ? new Date(new Date(automationState.last_run).getTime() + (Number(automation.interval_seconds || 0) * 1000))
    : null;
  const fingerprintModes = [
    {value: 'off', label: '透传'},
    {value: 'device', label: '设备'},
    {value: 'session', label: '设备 + 会话'},
    {value: 'full', label: '完全'},
  ];
  const readiness = [
    {label: '管理员密钥', ready: Boolean(config.admin_key_set)},
    {label: '固定代理', ready: Boolean(selectedProxy)},
    {label: '导入分组', ready: groupIds.length > 0},
    {label: '指纹策略', ready: codexFingerprintMode !== 'off'},
  ];
  const proxyLabel = proxyChoice === 'json' ? `JSON 自带（${jsonProxyCount}）` : selectedProxy?.name || '不绑定代理';
  const fingerprintLabel = fingerprintModes.find(mode => mode.value === codexFingerprintMode)?.label || '透传';
  const maxPlatformCount = Math.max(1, ...platforms.map(item => Number(item.count || 0)));
  const selectActiveGroups = () => activeGroups.forEach(group => {
    if (!groupIds.includes(group.id)) onToggleGroup(group.id);
  });
  const clearGroups = () => groupIds.forEach(onToggleGroup);

  return (
    <section className="tool-view sub2api-view">
      <div className="sub2api-service-strip">
        <div className={`sub2api-service-state ${config.admin_key_set ? 'connected' : ''}`}><Wifi size={17}/><span><strong>{config.admin_key_set ? 'Sub2API 已配置' : 'Sub2API 待配置'}</strong><small>{config.base_url || '未设置服务地址'}</small></span></div>
        <div><span>账号</span><strong>{monitor.total_accounts ?? '--'}</strong></div>
        <div><span>代理</span><strong>{options.proxy_count ?? 0}</strong></div>
        <div><span>分组</span><strong>{options.group_count ?? 0}</strong></div>
        <div><span>监控刷新</span><strong>{monitor.fetched_at ? compactTime(monitor.fetched_at) : '尚未同步'}</strong></div>
        <button className="icon-button" title="刷新 Sub2API 状态" aria-label="刷新 Sub2API 状态" onClick={onLoadOptions} disabled={optionsBusy || !config.admin_key_set}><RefreshCw size={16} className={optionsBusy ? 'spin' : ''}/></button>
      </div>

      <div className="tool-grid sub2api-primary-grid">
        <div className="tool-panel sub2api-config-panel">
          <div className="section-heading"><div><span className="detail-kicker">SUB2API ADMIN</span><h2>连接配置</h2><p>x-api-key · 后端代理</p></div><Settings2 size={21}/></div>
          <div className="sub2api-config-fields">
            <label><span>服务地址</span><input value={config.base_url} onChange={event => setConfig({...config, base_url: event.target.value})} placeholder="http://127.0.0.1:8080"/></label>
            <label><span>管理员密钥 {config.admin_key_set && <small>{config.admin_key_mask}</small>}</span><input type="password" value={adminKey} onChange={event => setAdminKey(event.target.value)} placeholder={config.admin_key_set ? '留空保留现有密钥' : '输入管理员密钥'} autoComplete="off"/></label>
          </div>
          <div className="tool-actions compact-actions"><button className="button primary" onClick={onSave}><Save size={15}/>保存</button><button className="button secondary" onClick={onTest} disabled={busy}><Activity size={15}/>测试</button><button className="button secondary" onClick={onReclaim401} disabled={reclaimBusy || busy}><KeyRound size={15}/>{reclaimBusy ? '扫描中' : '扫描并找回 401'}</button></div>
          {result && !result.mode && <div className={`connection-result ${result.ok ? 'ok' : 'bad'}`}>{result.ok ? `连接成功${result.account_count == null ? '' : ` · ${result.account_count} 个账号`}` : (result.detail || `上游 HTTP ${result.upstream_status}`)}</div>}
          {reclaimResult && <div className={`connection-result ${reclaimResult.ok ? 'ok' : 'bad'}`}>扫描 {reclaimResult.scanned_accounts} · 401 {reclaimResult.accounts_401} · 提交 {reclaimResult.card_code_count} · 跳过 {reclaimResult.skipped_non_401}</div>}
          <div className="sub2api-capability-grid"><div><span className={config.admin_key_set ? 'ready' : ''}/><small>管理认证</small><strong>{config.admin_key_set ? '已保存' : '待配置'}</strong></div><div><span className={options.loaded ? 'ready' : ''}/><small>账号读取</small><strong>{options.loaded ? '正常' : '待同步'}</strong></div><div><span className={options.proxy_service_available ? 'ready' : ''}/><small>代理资源</small><strong>{options.proxy_service_available ? `${options.proxy_count} 个` : '待同步'}</strong></div><div><span className={automationState ? 'ready' : ''}/><small>自动化状态</small><strong>{automationState ? '已连接' : '待同步'}</strong></div></div>
        </div>
        <div className="tool-panel sub2api-import-panel">
          <div className="section-heading"><div><span className="detail-kicker">ACCOUNT JSON</span><h2>账号导入</h2><p>sub2api-data / sub2api-bundle</p></div><FileUp size={21}/></div>
          <label className={`file-drop sub2api-file-drop ${dragging ? 'dragging' : ''}`} onDragEnter={event => { event.preventDefault(); setDragging(true); }} onDragOver={event => event.preventDefault()} onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget)) setDragging(false); }} onDrop={event => { event.preventDefault(); setDragging(false); onFiles(event.dataTransfer.files); }}><input type="file" accept="application/json,.json" multiple onChange={onFile}/><FileUp size={22}/><strong>{fileName || '选择或拖入账号 JSON'}</strong><small>{accountCount ? `${accountCount} 个账号 · ${jsonProxyCount} 个代理` : '支持多文件合并'}</small></label>
          <div className="import-strategy-summary">
            <div><Network size={15}/><span>代理<strong>{proxyLabel}</strong></span></div>
            <div><Layers3 size={15}/><span>分组<strong>{selectedGroups.length ? `${selectedGroups.length} 个已选` : '未分组'}</strong></span></div>
            <div><ShieldCheck size={15}/><span>指纹<strong>{fingerprintLabel}</strong></span></div>
          </div>
          <button className="button primary import-button" onClick={onImport} disabled={!payload || busy}><Upload size={15}/>{busy ? '正在导入' : accountCount ? `导入 ${accountCount} 个账号` : '导入账号'}</button>
        </div>
      </div>

      <Sub2ApiCardImportPanel
        redeemConfig={redeemConfig} setRedeemConfig={setRedeemConfig} onSaveRedeem={onSaveRedeem}
        codes={cardCodes} onCodes={onCardCodes} mode={cardMode} onMode={onCardMode}
        busy={cardBusy} importing={busy} flow={cardFlow} payload={payload} canAutoPush={Boolean(config.admin_key_set)}
        onRun={onRunCardImport} onPush={onPushCards}
        historyData={cardHistory} historyFilter={cardHistoryFilter} onHistoryFilter={onCardHistoryFilter}
        historyPage={cardHistoryPage} historyPageSize={cardHistoryPageSize} onHistoryPage={onCardHistoryPage} onHistoryPageSize={onCardHistoryPageSize}
        historyBusy={cardHistoryBusy} historyActions={cardHistoryActions} onRefreshHistory={onRefreshCardHistory} onRetryHistory={onRetryCardHistory} onDeleteHistory={onDeleteCardHistory} onCopyCode={onCopyCardCode}
      />

      <div className="automation-console">
        <div className="automation-head">
          <div><span className="detail-kicker">401 AUTOMATION CONTROL</span><h2>定时找回与自动导入</h2><p>账号健康、找回队列与导入策略</p></div>
          <div className="automation-head-actions"><span className={`automation-readiness ${automationReady ? 'ready' : ''}`}><ShieldCheck size={15}/>{automationReady ? '自动化就绪' : `${readiness.filter(item => !item.ready).length} 项待配置`}</span><button className="button secondary" onClick={onRunAutomation} disabled={automationBusy || !automation.enabled}><RefreshCw size={15} className={automationBusy ? 'spin' : ''}/>立即检查</button><button className="button primary" onClick={onSaveAutomation} disabled={automationBusy}><Save size={15}/>{automationBusy ? '处理中' : '保存策略'}</button></div>
        </div>

        <div className="automation-metrics">
          <div><span>账号总量</span><strong>{monitor.total_accounts ?? '--'}</strong><small>{platforms.length} 个平台</small></div>
          <div><span>可调度</span><strong className="positive">{monitor.schedulable_accounts ?? '--'}</strong><small>{monitor.unschedulable_accounts ?? 0} 个不可调度</small></div>
          <div><span>账号异常</span><strong className={monitor.error_accounts ? 'negative' : ''}>{monitor.error_accounts ?? '--'}</strong><small>{monitor.expiring_accounts ?? 0} 个 7 日内到期</small></div>
          <div><span>限流 / 过载</span><strong className={monitor.rate_limited_accounts ? 'warning' : ''}>{monitor.rate_limited_accounts ?? '--'}</strong><small>含临时不可调度</small></div>
          <div><span>代理异常</span><strong className={monitor.unhealthy_proxies ? 'negative' : ''}>{monitor.unhealthy_proxies ?? '--'}</strong><small>{monitor.active_proxies ?? 0} 个活跃</small></div>
          <div><span>找回队列</span><strong>{automationState?.pending_card_codes?.length ?? 0}</strong><small>{automationResult?.downloaded ?? 0} 个最近下载</small></div>
        </div>

        <div className="automation-workspace">
          <section className="automation-setting-card">
            <div className="automation-card-head"><TimerReset size={17}/><span><strong>运行计划</strong><small>{automation.enabled ? '自动执行中' : '自动执行已关闭'}</small></span></div>
            <div className="automation-toggle-row"><span><strong>401 定时监控</strong><small>明确授权错误进入找回队列</small></span><button className={`switch ${automation.enabled ? 'on' : ''}`} role="switch" aria-checked={automation.enabled} title={automation.enabled ? '关闭自动监控' : '开启自动监控'} disabled={!automation.enabled && !automationReady} onClick={() => onAutomationChange(current => ({...current, enabled: !current.enabled}))}><span/></button></div>
            <label className="automation-check-row"><input type="checkbox" checked={automation.auto_import} onChange={event => onAutomationChange(current => ({...current, auto_import: event.target.checked}))}/><span><strong>找回后自动导入</strong><small>应用右侧固定分配策略</small></span></label>
            <label className="automation-interval"><span>检查间隔</span><div><input type="number" min="10" max="86400" value={automation.interval_seconds} onChange={event => onAutomationChange(current => ({...current, interval_seconds: Math.max(10, Math.min(86400, Number(event.target.value) || 10))}))} inputMode="numeric"/><span>秒</span></div></label>
            <div className="interval-presets">{[60, 300, 900, 3600].map(seconds => <button className={Number(automation.interval_seconds) === seconds ? 'active' : ''} key={seconds} onClick={() => onAutomationChange(current => ({...current, interval_seconds: seconds}))}>{intervalLabel(seconds)}</button>)}</div>
            <div className="automation-readiness-grid">{readiness.map(item => <span className={item.ready ? 'ready' : ''} key={item.label}>{item.ready ? <Check size={13}/> : <AlertCircle size={13}/>} {item.label}</span>)}</div>
          </section>

          <section className="automation-setting-card assignment-card">
            <div className="automation-card-head"><Network size={17}/><span><strong>代理、分组与指纹</strong><small>手动导入与自动导入共用</small></span><button className="icon-button" title="刷新代理和分组" aria-label="刷新代理和分组" onClick={onLoadOptions} disabled={optionsBusy}><RefreshCw size={15} className={optionsBusy ? 'spin' : ''}/></button></div>
            <label><span>固定代理</span><select value={proxyChoice} onChange={event => onProxyChoice(event.target.value)}><option value="json">JSON 自带代理（仅手动导入）</option><option value="none">不绑定代理</option>{options.proxies.map(proxy => <option value={`proxy:${proxy.id}`} key={proxy.id}>{proxy.name} · {proxy.status || 'unknown'}{proxy.latency_ms == null ? '' : ` · ${proxy.latency_ms}ms`}</option>)}</select></label>
            {selectedProxy && <div className="selected-proxy-meta"><span>{selectedProxy.protocol}://{selectedProxy.host}:{selectedProxy.port}</span><span>{selectedProxy.account_count} 个账号</span><span>{selectedProxy.quality_grade || selectedProxy.country_code || '未评级'}</span></div>}
            <div className="fingerprint-field"><span>Codex 指纹收敛</span><div className="fingerprint-segment" data-testid="codex-fingerprint-mode">{fingerprintModes.map(mode => <button className={codexFingerprintMode === mode.value ? 'active' : ''} key={mode.value} onClick={() => onCodexFingerprintMode(mode.value)}>{mode.label}</button>)}</div></div>
            <div className="group-picker automation-group-picker"><div className="group-picker-head"><span>导入分组 <strong>{groupIds.length}</strong></span><div><button onClick={selectActiveGroups}>全选活跃</button><button onClick={clearGroups} disabled={!groupIds.length}>清空</button></div></div>{options.groups.length ? <div>{options.groups.map(group => <label className={group.status && group.status !== 'active' ? 'inactive' : ''} key={group.id}><input type="checkbox" checked={groupIds.includes(group.id)} onChange={() => onToggleGroup(group.id)}/><span><strong>{group.name}</strong><small>{group.platform || '通用'} · {group.account_count} 个账号 · {group.status || 'active'}</small></span></label>)}</div> : <p>{options.loaded ? '当前没有可用分组' : '连接后加载分组'}</p>}</div>
          </section>

          <section className="automation-setting-card runtime-card">
            <div className="automation-card-head"><Gauge size={17}/><span><strong>运行状态</strong><small>{automationState?.last_error ? '最近执行异常' : automationResult ? '最近执行完成' : '等待首次执行'}</small></span></div>
            <div className={`runtime-state ${automationState?.last_error ? 'bad' : automationResult ? 'ok' : ''}`}><span className="runtime-dot"/><strong>{automationState?.last_error ? '执行失败' : automation.enabled ? '监控运行中' : '监控已暂停'}</strong></div>
            <dl className="runtime-details"><div><dt>最近运行</dt><dd>{automationState?.last_run ? compactTime(automationState.last_run) : '--'}</dd></div><div><dt>下次运行</dt><dd>{nextRun && !Number.isNaN(nextRun.getTime()) ? compactTime(nextRun) : '--'}</dd></div><div><dt>扫描账号</dt><dd>{automationResult?.scanned_accounts ?? 0}</dd></div><div><dt>发现 401</dt><dd>{automationResult?.accounts_401 ?? 0}</dd></div><div><dt>完成下载</dt><dd>{automationResult?.downloaded ?? 0}</dd></div><div><dt>自动导入</dt><dd>{automationResult?.imported ? '已完成' : '无'}</dd></div></dl>
            {automationState?.last_error && <div className="runtime-error"><TriangleAlert size={14}/><span>{automationState.last_error}</span></div>}
            <div className="platform-health"><span>平台分布</span>{platforms.length ? platforms.slice(0, 5).map(item => <div key={item.platform}><strong>{item.platform}</strong><span><i style={{width: `${Math.max(5, (Number(item.count || 0) / maxPlatformCount) * 100)}%`}}/></span><em>{item.count}{item.errors ? ` / ${item.errors} 异常` : ''}</em></div>) : <small>暂无账号数据</small>}</div>
          </section>
        </div>

        <div className="automation-activity-grid">
          <section className="automation-activity-panel">
            <div className="activity-panel-head"><div><span className="detail-kicker">RECENT RUNS</span><h3>运行记录</h3></div><span>{runHistory.length} 条</span></div>
            <div className="automation-run-list">{runHistory.length ? runHistory.slice(0, 6).map((entry, index) => <div className={entry.status === 'error' ? 'bad' : ''} key={`${entry.run_at}-${index}`}><span>{compactTime(entry.run_at)}</span><strong>{entry.status === 'error' ? '失败' : entry.imported ? '找回并导入' : '检查完成'}</strong><small>{entry.status === 'error' ? entry.error : `扫描 ${entry.scanned_accounts ?? 0} · 401 ${entry.accounts_401 ?? 0} · 下载 ${entry.downloaded ?? 0}`}</small></div>) : <div className="compact-empty"><Clock3 size={16}/>暂无运行记录</div>}</div>
          </section>
          <section className="automation-activity-panel">
            <div className="activity-panel-head"><div><span className="detail-kicker">ACCOUNT ALERTS</span><h3>最近账号异常</h3></div><span>{monitor.error_accounts ?? 0} 个</span></div>
            <div className="account-alert-list">{recentErrors.length ? recentErrors.map(account => <div key={account.id || account.name}><TriangleAlert size={15}/><span><strong>{account.name}</strong><small>{account.platform} · {account.error}</small></span><em>{account.status}</em></div>) : <div className="compact-empty"><ShieldCheck size={16}/>当前没有账号异常</div>}</div>
          </section>
        </div>

        <Sub2ApiAccountsPanel
          data={accountsData}
          filters={accountFilters}
          onFiltersChange={patch => { onAccountFiltersChange({...accountFilters, ...patch}); }}
          busy={accountBusy}
          error={accountError}
          actions={accountActions}
          tested={testedAccounts}
          onRefresh={onLoadAccounts}
          onTest={onTestAccount}
          onDelete={onDeleteAccount}
          onCopyName={onCopyAccountName}
          onPage={page => onLoadAccounts({page})}
        />
      </div>
    </section>
  );
}
