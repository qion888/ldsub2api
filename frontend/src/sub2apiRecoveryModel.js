const OUTCOME_META = Object.freeze({
  recovered: {label: '找回完成', tone: 'success'},
  no_401: {label: '未发现 401', tone: 'neutral'},
  pending: {label: '找回处理中', tone: 'warning'},
  partial: {label: '部分找回', tone: 'warning'},
  unrecoverable: {label: '存在无法找回项', tone: 'danger'},
  failed: {label: '找回失败', tone: 'danger'},
  error: {label: '找回请求失败', tone: 'danger'},
});

const IMPORT_META = Object.freeze({
  disabled: {label: '自动导入已关闭', tone: 'neutral'},
  not_attempted: {label: '尚未执行自动导入', tone: 'neutral'},
  confirmed: {label: '自动导入已确认', tone: 'success'},
  unconfirmed: {label: '自动导入未完全确认', tone: 'warning'},
  failed: {label: '自动导入失败', tone: 'danger'},
});

const ACTIVE_STATUSES = new Set(['queued', 'already_running', 'running', 'pending', 'processing', 'submitted']);
const DONE_STATUSES = new Set(['done', 'completed', 'success']);
const PERMANENT_STATUSES = new Set(['unreclaimable', 'not_owned', 'skipped', 'permanent', 'attempt_limit']);
const RETRYABLE_STATUSES = new Set(['failed', 'error', 'timeout', 'download_failed']);

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function count(...values) {
  return values.reduce((maximum, value) => {
    const number = Number(value);
    return Number.isFinite(number) && number >= 0
      ? Math.max(maximum, Math.floor(number))
      : maximum;
  }, 0);
}

function firstCount(...values) {
  for (const value of values) {
    if (value === undefined || value === null || value === '') continue;
    const number = Number(value);
    if (Number.isFinite(number) && number >= 0) return Math.floor(number);
  }
  return 0;
}

function text(value, fallback = '') {
  const valueText = String(value ?? '').trim();
  return valueText || fallback;
}

function uniqueStrings(values) {
  return [...new Set((Array.isArray(values) ? values : []).map(value => text(value)).filter(Boolean))];
}

function taskList(source, raw) {
  const candidates = [source.reclaim_failures, source.failures, raw.reclaim_failures, raw.failures];
  for (const candidate of candidates) {
    if (Array.isArray(candidate) && candidate.length) return candidate.filter(isObject);
  }
  const rawTasks = Array.isArray(raw.all_tasks) ? raw.all_tasks : [];
  const missing = Array.isArray(source.missing_card_code_accounts)
    ? source.missing_card_code_accounts.filter(isObject).map(account => ({
      id: account.id,
      name: account.name,
      status: 'unreclaimable',
      category: 'unrecoverable',
      permanent: true,
      retryable: false,
      reason: '401 账号名称中没有可用卡密',
    }))
    : [];
  return [...rawTasks.filter(isObject), ...missing];
}

function missingFailureList(source) {
  if (!Array.isArray(source?.missing_card_code_accounts)) return [];
  return source.missing_card_code_accounts.filter(isObject).map(account => ({
    id: account.id,
    name: account.name,
    status: 'unreclaimable',
    category: 'unrecoverable',
    permanent: true,
    retryable: false,
    reason: '401 account name has no usable card code',
  }));
}

function classifyFailure(item) {
  const status = text(item.status).toLowerCase();
  const category = text(item.category).toLowerCase();
  const providerStatus = Number(item.provider_status);
  const markerText = [item.reason, item.failure_reason, item.message, item.error_code, item.failure_class]
    .map(value => text(value).toLowerCase())
    .join(' ');
  const permanentMarker = /account[_ -]?deactivated|unreclaimable|not[_ -]?owned|revoked[_ -]?permanently|forbidden|disabled/.test(markerText);
  if (item.retryable === false || item.permanent === true || category === 'unrecoverable' || PERMANENT_STATUSES.has(status) || [403, 404].includes(providerStatus) || permanentMarker) {
    return 'unrecoverable';
  }
  if (item.retryable === true) return 'retryable';
  if (category === 'active' || ACTIVE_STATUSES.has(status)) return 'active';
  if (category === 'retryable' || RETRYABLE_STATUSES.has(status) || item.download_error) return 'retryable';
  if (DONE_STATUSES.has(status) && !item.download_error) return 'recovered';
  return item.reason || item.message || item.error_code || item.failure_class ? 'retryable' : 'active';
}

function normalizeFailure(item) {
  const category = classifyFailure(item);
  const status = text(item.status).toLowerCase();
  const providerStatus = Number(item.provider_status);
  const explicitBucket = text(item.failure_bucket).toLowerCase();
  const failureBucket = category === 'unrecoverable'
    ? (['unreclaimable', 'not_owned', 'skipped', 'attempt_limit'].includes(explicitBucket)
      ? explicitBucket
      : ['unreclaimable', 'not_owned', 'skipped', 'attempt_limit'].includes(status) ? status : 'unreclaimable')
    : category;
  const reason = text(
    item.reason,
    text(item.message, text(item.download_error, text(item.failure_class, text(item.error_code, providerStatus ? `上游 HTTP ${providerStatus}` : '找回状态未知')))),
  );
  return {
    ...item,
    card_code: text(item.card_code),
    order_no: text(item.order_no),
    status: text(item.status, category === 'active' ? 'pending' : category),
    reason,
    provider_status: Number.isFinite(providerStatus) && providerStatus > 0 ? providerStatus : null,
    retryable: category === 'retryable',
    permanent: category === 'unrecoverable',
    category,
    failure_bucket: failureBucket,
  };
}

function inferOutcome(source, summary, failures, retryCodes) {
  const raw = isObject(source.result) ? source.result : source;
  const explicit = text(source.outcome, text(source.recovery_status, text(raw.outcome, raw.recovery_status))).toLowerCase();
  if (OUTCOME_META[explicit]) return explicit;
  const active = count(summary.active, count(summary.queued) + count(summary.already_running));
  const done = count(summary.done);
  const unrecoverable = count(summary.unreclaimable) + count(summary.not_owned) + count(summary.skipped)
    + failures.filter(item => item.category === 'unrecoverable').length;
  const retryable = retryCodes.length || failures.filter(item => item.retryable).length;
  if ((source.ok === false || raw.ok === false) && (source.error || source.detail || raw.error || raw.detail)) return 'error';
  if (active > 0) return 'pending';
  if (unrecoverable && (done || retryable || count(summary.downloaded))) return 'partial';
  if (unrecoverable) return 'unrecoverable';
  if (retryable) return count(summary.downloaded) ? 'partial' : 'failed';
  if (done || count(summary.downloaded)) return 'recovered';
  return source.accounts_401 || source.card_code_count ? 'pending' : 'no_401';
}

function inferImportStatus(source) {
  const explicit = text(source.import_status).toLowerCase();
  if (IMPORT_META[explicit]) return explicit;
  if (source.imported === true || source.import_result?.import_verification?.confirmed === true) return 'confirmed';
  if (source.import_result?.import_verification || source.import_attempted) return 'unconfirmed';
  return source.auto_import === false ? 'disabled' : 'not_attempted';
}

export function normalizeSub2ApiRecoveryResult(result) {
  const source = isObject(result) ? result : {};
  // Legacy /redeem responses put task fields at the top level; Sub2API
  // responses wrap them in ``result``. Treat both shapes uniformly.
  const raw = isObject(source.result) ? source.result : source;
  const rawSummary = isObject(raw.reclaim_summary) ? raw.reclaim_summary : {};
  const providedSummary = isObject(source.reclaim_summary) ? source.reclaim_summary : {};
  const listedFailures = taskList(source, raw);
  const missingEntries = missingFailureList(source);
  const listedMissingIds = new Set(listedFailures.map(item => item.id).filter(value => value !== undefined));
  const failures = [
    ...listedFailures,
    ...missingEntries.filter(item => !listedMissingIds.has(item.id)),
  ].map(normalizeFailure);
  let dedupedFailures = [];
  const seenFailures = new Set();
  for (const failure of failures) {
    const key = [failure.card_code, failure.order_no, failure.id, failure.reason, failure.category].join('|');
    if (seenFailures.has(key)) continue;
    seenFailures.add(key);
    if (failure.category !== 'active' && failure.category !== 'recovered') dedupedFailures.push(failure);
  }
  const tasks = Array.isArray(raw.all_tasks) ? raw.all_tasks.filter(isObject) : [];
  const explicitPermanentBuckets = {
    not_owned: uniqueStrings([
      ...(Array.isArray(source.not_owned_card_codes) ? source.not_owned_card_codes : []),
      ...(Array.isArray(raw.not_owned_card_codes) ? raw.not_owned_card_codes : []),
    ]),
    skipped: uniqueStrings([
      ...(Array.isArray(source.skipped_card_codes) ? source.skipped_card_codes : []),
      ...(Array.isArray(raw.skipped_card_codes) ? raw.skipped_card_codes : []),
    ]),
    unreclaimable: uniqueStrings([
      ...(Array.isArray(source.permanent_card_codes) ? source.permanent_card_codes : []),
      ...(Array.isArray(source.unrecoverable_card_codes) ? source.unrecoverable_card_codes : []),
      ...(Array.isArray(source.non_retryable_card_codes) ? source.non_retryable_card_codes : []),
      ...(Array.isArray(raw.permanent_card_codes) ? raw.permanent_card_codes : []),
      ...(Array.isArray(raw.unrecoverable_card_codes) ? raw.unrecoverable_card_codes : []),
      ...(Array.isArray(raw.non_retryable_card_codes) ? raw.non_retryable_card_codes : []),
    ]),
  };
  const explicitPermanentCodeSet = new Set([
    ...explicitPermanentBuckets.unreclaimable,
    ...explicitPermanentBuckets.not_owned,
    ...explicitPermanentBuckets.skipped,
  ]);
  const normalizeWithExplicitPermanent = item => {
    const code = text(item.card_code);
    return normalizeFailure(explicitPermanentCodeSet.has(code) ? {...item, permanent: true} : item);
  };
  const normalizedTasks = tasks.map(normalizeWithExplicitPermanent);
  dedupedFailures = dedupedFailures.map(item => (
    explicitPermanentCodeSet.has(item.card_code)
      ? normalizeFailure({...item, permanent: true})
      : item
  ));
  const taskCounts = normalizedTasks.reduce((counts, task) => {
    counts[task.category] = (counts[task.category] || 0) + 1;
    return counts;
  }, {active: 0, recovered: 0, retryable: 0, unrecoverable: 0});
  const taskBucketCounts = normalizedTasks.reduce((counts, task) => {
    if (task.category === 'unrecoverable') {
      const bucket = ['unreclaimable', 'not_owned', 'skipped'].includes(task.failure_bucket)
        ? task.failure_bucket
        : 'unreclaimable';
      counts[bucket] += 1;
    }
    return counts;
  }, {unreclaimable: 0, not_owned: 0, skipped: 0});
  const taskIds = new Set(tasks.map(task => task.id).filter(value => value !== undefined));
  const missingCount = missingEntries.filter(item => !taskIds.has(item.id)).length;
  const representedPermanentCodes = new Set([
    ...normalizedTasks.filter(item => item.category === 'unrecoverable').map(item => item.card_code),
    ...dedupedFailures.filter(item => item.category === 'unrecoverable').map(item => item.card_code),
  ].filter(Boolean));
  const omittedPermanentCount = bucket => explicitPermanentBuckets[bucket]
    .filter(code => !representedPermanentCodes.has(code)).length;
  const taskQueued = normalizedTasks.filter(task => task.category === 'active' && task.status !== 'already_running').length;
  const taskAlreadyRunning = normalizedTasks.filter(task => task.category === 'active' && task.status === 'already_running').length;
  const noAction = normalizedTasks.filter(task => (
    task.category === 'recovered'
    && (task.no_action === true || String(task.no_action).toLowerCase() === 'true')
  )).length;
  const downloadedPayloads = Array.isArray(source.downloaded_payloads)
    ? source.downloaded_payloads
    : Array.isArray(raw.downloaded_payloads) ? raw.downloaded_payloads : null;
  const downloadsRequested = source.downloads_requested !== false
    && raw.downloads_requested !== false
    && source.include_downloads !== false
    && raw.include_downloads !== false
    && source.auto_import !== false
    && raw.auto_import !== false
    && source.import_status !== 'disabled'
    && raw.import_status !== 'disabled';
  const taskDownloaded = normalizedTasks.filter(task => (
    task.downloaded === true
    || text(task.download_status).toLowerCase() === 'downloaded'
  )).length;
  const downloaded = downloadedPayloads
    ? downloadedPayloads.length
    : tasks.length
      ? taskDownloaded
      : firstCount(providedSummary.downloaded, source.downloaded, rawSummary.downloaded, raw.downloaded);
  const downloadCandidates = normalizedTasks.filter(task => (
    task.category === 'recovered'
    && DONE_STATUSES.has(task.status)
    && !task.no_action
    && !task.download_skipped
    && (task.order_no || task.download_token)
  )).length;
  const downloadedOrderNos = new Set(
    (downloadedPayloads || [])
      .map(item => text(item?.task?.order_no || item?.filename))
      .filter(Boolean),
  );
  const missingDownloadTasks = normalizedTasks.filter(task => (
    downloadsRequested
    && downloadedPayloads
    && task.category === 'recovered'
    && DONE_STATUSES.has(task.status)
    && !task.no_action
    && !task.download_skipped
    && (task.order_no || task.download_token)
    && (task.order_no
      ? !downloadedOrderNos.has(task.order_no)
      : downloadedPayloads.length === 0)
  ));
  const downloadGap = downloadsRequested && downloadedPayloads
    ? Math.max(0, downloadCandidates - downloadedPayloads.length, missingDownloadTasks.length)
    : 0;
  // A task marked ``completed`` can still be permanently unrecoverable (for
  // example a provider returns a stale completed status with HTTP 403).  Once
  // task details are present, their normalized categories are authoritative;
  // aggregate counters are retained only for responses without details.
  const done = tasks.length
    ? taskCounts.recovered
    : firstCount(
      providedSummary.done,
      source.done,
      rawSummary.done,
      raw.done,
    );
  const queued = tasks.length
    ? taskQueued
    : firstCount(providedSummary.queued, source.queued, rawSummary.queued, raw.queued);
  const alreadyRunning = tasks.length
    ? taskAlreadyRunning
    : firstCount(providedSummary.already_running, source.already_running, rawSummary.already_running, raw.already_running);
  const active = tasks.length
    ? taskCounts.active
    : firstCount(providedSummary.active, source.active, rawSummary.active, raw.active, queued + alreadyRunning);
  const explicitUnreclaimable = firstCount(providedSummary.unreclaimable, source.unreclaimable, rawSummary.unreclaimable, raw.unreclaimable);
  const notOwned = firstCount(providedSummary.not_owned, source.not_owned, rawSummary.not_owned, raw.not_owned);
  const skipped = firstCount(providedSummary.skipped, source.skipped, rawSummary.skipped, raw.skipped);
  const failureBucketCount = bucket => failures.filter(item => (
    item.category === 'unrecoverable'
    && (item.failure_bucket === bucket || text(item.status).toLowerCase() === bucket)
  )).length;
  const detailUnrecoverable = failures.filter(item => item.category === 'unrecoverable').length;
  const detailNotOwned = failures.filter(item => text(item.status).toLowerCase() === 'not_owned').length;
  const detailSkipped = failures.filter(item => text(item.status).toLowerCase() === 'skipped').length;
  const unreclaimable = tasks.length
    ? Math.max(taskBucketCounts.unreclaimable + missingCount + omittedPermanentCount('unreclaimable'), failureBucketCount('unreclaimable'))
    : Math.max(explicitUnreclaimable, detailUnrecoverable - detailNotOwned - detailSkipped, explicitPermanentBuckets.unreclaimable.length);
  const detailRetryable = failures.filter(item => item.retryable).length;
  const explicitFailed = firstCount(providedSummary.failed, source.failed, rawSummary.failed, raw.failed);
  // Providers sometimes count a permanent task in both ``failed`` and
  // ``unreclaimable``.  Consume the known permanent details before creating
  // synthetic retryable rows so a 403/account-deactivated task is not shown as
  // retryable a second time.
  const failed = tasks.length
    ? Math.max(taskCounts.retryable + downloadGap, detailRetryable)
    : Math.max(detailRetryable, explicitFailed - detailUnrecoverable);
  const permanentCodes = new Set([
    ...dedupedFailures.filter(item => item.category === 'unrecoverable').map(item => item.card_code),
    ...(Array.isArray(source.permanent_card_codes) ? source.permanent_card_codes : []),
    ...(Array.isArray(source.unrecoverable_card_codes) ? source.unrecoverable_card_codes : []),
    ...(Array.isArray(source.non_retryable_card_codes) ? source.non_retryable_card_codes : []),
    ...(Array.isArray(raw.permanent_card_codes) ? raw.permanent_card_codes : []),
    ...(Array.isArray(raw.unrecoverable_card_codes) ? raw.unrecoverable_card_codes : []),
    ...(Array.isArray(raw.non_retryable_card_codes) ? raw.non_retryable_card_codes : []),
  ].map(value => text(value)).filter(Boolean));
  let retryCodes = uniqueStrings([
    ...(Array.isArray(source.retryable_card_codes) ? source.retryable_card_codes : []),
    ...dedupedFailures.filter(item => item.retryable).map(item => item.card_code),
  ]).filter(code => !permanentCodes.has(code));
  const summary = {
    queued,
    already_running: alreadyRunning,
    active,
    done,
    downloaded,
    download_failed: tasks.length
      ? normalizedTasks.filter(task => task.category === 'retryable' && Boolean(task.download_error)).length + downloadGap
      : firstCount(providedSummary.download_failed, source.download_failed, rawSummary.download_failed, raw.download_failed),
    download_skipped: firstCount(providedSummary.download_skipped, source.download_skipped, rawSummary.download_skipped, raw.download_skipped),
    unreclaimable,
    not_owned: tasks.length
      ? Math.max(taskBucketCounts.not_owned + omittedPermanentCount('not_owned'), failureBucketCount('not_owned'))
      : Math.max(notOwned, detailNotOwned, explicitPermanentBuckets.not_owned.length),
    skipped: tasks.length
      ? Math.max(taskBucketCounts.skipped + omittedPermanentCount('skipped'), failureBucketCount('skipped'))
      : Math.max(skipped, detailSkipped, explicitPermanentBuckets.skipped.length),
    failed,
    submitted: count(providedSummary.submitted, source.card_code_count, source.requested_cards, rawSummary.submitted, raw.requested_cards),
    no_action: noAction,
  };
  if (!retryCodes.length && failed > 0 && !(unreclaimable || notOwned || skipped) && summary.active === 0) {
    retryCodes = uniqueStrings([
      ...(Array.isArray(source.reclaim_card_codes) ? source.reclaim_card_codes : []),
      ...(Array.isArray(source.card_codes) ? source.card_codes : []),
    ]).filter(code => !permanentCodes.has(code));
  }
  if (downloadGap > 0) {
    for (const task of missingDownloadTasks) {
      if (task.card_code && !permanentCodes.has(task.card_code) && !retryCodes.includes(task.card_code)) {
        retryCodes.push(task.card_code);
      }
    }
  }
  retryCodes = retryCodes.slice(0, 100);
  const aggregateReason = text(source.error, text(raw.error, '上游返回无法找回'));
  const addAggregateFailure = (countValue, category, status, reason, codes = []) => {
    const expectedBucket = category === 'unrecoverable' ? status : category;
    const existing = dedupedFailures.filter(item => {
      if (item.category !== category) return false;
      const itemStatus = text(item.status).toLowerCase();
      const itemBucket = item.failure_bucket
        || (category === 'unrecoverable' && ['unreclaimable', 'not_owned', 'skipped'].includes(itemStatus)
          ? itemStatus
          : category);
      return itemBucket === expectedBucket;
    }).length;
    const remaining = Math.max(0, countValue - existing);
    for (let index = 0; index < remaining; index += 1) {
      dedupedFailures.push({
        card_code: codes[index] || '',
        order_no: '',
        status,
        reason,
        message: reason,
        retryable: category === 'retryable',
        permanent: category === 'unrecoverable',
        category,
        failure_bucket: expectedBucket,
        aggregate: true,
      });
    }
  };
  addAggregateFailure(unreclaimable, 'unrecoverable', 'unreclaimable', aggregateReason);
  addAggregateFailure(notOwned, 'unrecoverable', 'not_owned', text(source.not_owned_reason, '该卡密不属于当前账号'));
  addAggregateFailure(skipped, 'unrecoverable', 'skipped', text(source.skipped_reason, '上游跳过该项目'));
  addAggregateFailure(failed, 'retryable', 'failed', text(source.error, '找回失败，可重新提交'), retryCodes);
  const outcome = inferOutcome(source, summary, dedupedFailures, retryCodes);
  const importStatus = inferImportStatus(source);
  const recoveryMessage = text(
    source.recovery_message,
    text(source.detail, text(source.error, text(raw.error, OUTCOME_META[outcome]?.label || '找回状态未知'))),
  );
  const retryAvailable = source.retry_available === false
    ? false
    : Boolean(retryCodes.length && active === 0 && ['failed', 'partial', 'error'].includes(outcome));
  return {
    source,
    outcome,
    outcomeMeta: OUTCOME_META[outcome] || {label: '找回状态未知', tone: 'neutral'},
    recoveryMessage,
    recoveryOk: typeof source.recovery_ok === 'boolean' ? source.recovery_ok : ['recovered', 'no_401'].includes(outcome),
    summary,
    failures: dedupedFailures,
    retryCodes,
    retryAvailable,
    importStatus,
    importMeta: IMPORT_META[importStatus] || IMPORT_META.not_attempted,
    importError: text(source.import_error, text(source.import_result?.error)),
    downloadsRequested,
    // ``failed`` is a separate card/task bucket.  Subtracting it from done
    // makes a mixed success/failure response report zero updated accounts.
    updated: Math.max(0, (tasks.length ? taskCounts.recovered : done) - noAction - downloadGap),
    noAction,
  };
}

export function recoveryResultForProgress(previous, progress, snapshot = null) {
  const base = isObject(previous) ? previous : {};
  const next = isObject(progress) ? progress : {};
  const baseRaw = isObject(base.result) ? base.result : base;
  const nextRaw = isObject(next.result) ? next.result : next;

  const summaryOf = value => {
    if (!isObject(value)) return {};
    const rawValue = isObject(value.result) ? value.result : value;
    if (isObject(value.reclaim_summary)) return value.reclaim_summary;
    return isObject(rawValue.reclaim_summary) ? rawValue.reclaim_summary : {};
  };
  const failureEntriesOf = value => {
    if (!isObject(value)) return [];
    const rawValue = isObject(value.result) ? value.result : value;
    const listed = taskList(value, rawValue);
    const listedIds = new Set(listed.map(item => item.id).filter(id => id !== undefined));
    return [...listed, ...missingFailureList(value).filter(item => !listedIds.has(item.id))]
      .map(normalizeFailure)
      .filter(item => item.category !== 'active' && item.category !== 'recovered');
  };
  const failureIdentity = item => {
    const subject = item.card_code || item.order_no || item.id;
    return subject
      ? `${subject}`
      : `${item.category}|${item.failure_bucket || ''}|${item.reason || ''}`;
  };
  const mergeFailures = () => {
    const baseFailures = failureEntriesOf(base);
    const nextFailures = failureEntriesOf(next);
    const permanentCodes = new Set(
      baseFailures
        .filter(item => item.category === 'unrecoverable' && item.card_code)
        .map(item => item.card_code),
    );
    const seen = new Set();
    const mergedFailures = [];
    for (const item of nextFailures) {
      // A permanent result is terminal.  Do not let a later subset poll turn
      // the same card back into an active/retryable row.
      if (permanentCodes.has(item.card_code) && item.category !== 'unrecoverable') continue;
      const key = failureIdentity(item);
      if (seen.has(key)) continue;
      seen.add(key);
      mergedFailures.push(item);
    }
    for (const item of baseFailures) {
      if (item.category !== 'unrecoverable') continue;
      const key = failureIdentity(item);
      const sameSubject = item.card_code && mergedFailures.some(row => row.card_code === item.card_code);
      if (seen.has(key) || sameSubject) continue;
      seen.add(key);
      mergedFailures.push(item);
    }
    return mergedFailures;
  };
  const baseSummary = summaryOf(base);
  const nextSummary = summaryOf(next);
  const mergedSummary = {...baseSummary, ...nextSummary};
  for (const bucket of ['unreclaimable', 'not_owned', 'skipped']) {
    if (nextSummary[bucket] !== undefined) mergedSummary[bucket] = nextSummary[bucket];
    else if (baseSummary[bucket] !== undefined) mergedSummary[bucket] = baseSummary[bucket];
  }
  const mergedFailures = mergeFailures();
  const mergedDownloads = (() => {
    const values = [];
    const seen = new Set();
    const add = item => {
      if (!isObject(item)) return;
      const key = item?.task?.order_no || item.filename || item.content_base64;
      if (!key || seen.has(String(key))) return;
      seen.add(String(key));
      values.push(item);
    };
    const snapshotDownloads = snapshot && Array.isArray(snapshot.downloads) ? snapshot.downloads : null;
    const baseDownloads = Array.isArray(base.downloaded_payloads) ? base.downloaded_payloads : [];
    const nextDownloads = Array.isArray(next.downloaded_payloads) ? next.downloaded_payloads : [];
    const nestedNextDownloads = Array.isArray(nextRaw.downloaded_payloads) ? nextRaw.downloaded_payloads : [];
    (snapshotDownloads || baseDownloads || nextDownloads).forEach(add);
    if (!snapshotDownloads) nestedNextDownloads.forEach(add);
    return values;
  })();
  const merged = {
    ...base,
    ...next,
    reclaim_summary: mergedSummary,
    result: {...nextRaw, reclaim_summary: mergedSummary},
  };
  if (mergedFailures.length) {
    merged.reclaim_failures = mergedFailures;
    merged.result.reclaim_failures = mergedFailures;
  }
  if (mergedDownloads.length || Array.isArray(base.downloaded_payloads) || Array.isArray(next.downloaded_payloads)) {
    merged.downloaded_payloads = mergedDownloads;
    merged.result.downloaded_payloads = mergedDownloads;
  }
  if (snapshot && isObject(snapshot)) {
    merged.polling = {
      attempts: snapshot.attempts,
      elapsed_ms: snapshot.elapsedMs,
      timeout_ms: snapshot.timeoutMs,
      active: snapshot.activeTasks,
      downloaded: snapshot.downloadedCount,
    };
  }
  const nextHasOutcome = Boolean(
    next.outcome !== undefined
    || next.recovery_status !== undefined
    || nextRaw.outcome !== undefined
    || nextRaw.recovery_status !== undefined,
  );
  // Progress responses generally carry only counters.  Drop the initial
  // explicit outcome/message so the latest task categories determine the
  // visible state instead of leaving a stale ``pending``/``partial`` label.
  if (!nextHasOutcome && (Object.keys(nextSummary).length || Array.isArray(nextRaw.all_tasks))) {
    delete merged.outcome;
    delete merged.recovery_status;
    delete merged.recovery_message;
    delete merged.detail;
    delete merged.result.outcome;
    delete merged.result.recovery_status;
    delete merged.result.recovery_message;
    delete merged.result.detail;
  }
  // Preserve scan-level context while letting the latest progress metadata win.
  for (const key of ['reclaim_card_codes', 'card_code_count', 'scanned_accounts', 'accounts_401', 'missing_card_code_accounts']) {
    if (base[key] !== undefined && merged[key] === undefined) merged[key] = base[key];
  }
  return merged;
}

export function recoveryStatusLabel(result) {
  return normalizeSub2ApiRecoveryResult(result).outcomeMeta.label;
}
