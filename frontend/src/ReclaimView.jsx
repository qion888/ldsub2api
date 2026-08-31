import React from 'react';
import {Activity, AlertCircle, CircleCheck, CircleX, FileUp, Info, KeyRound, RefreshCw, Save, TriangleAlert, Upload, Zap} from 'lucide-react';
import {normalizeSub2ApiRecoveryResult} from './sub2apiRecoveryModel.js';

function recoveryCount(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.floor(number)) : 0;
}

function LegacyRecoveryResult({result, busy, onRetry}) {
  const modelInput = result && !result.result
    && (Array.isArray(result.all_tasks) || result.failed !== undefined || result.unreclaimable !== undefined)
    ? {...result, result}
    : result;
  const model = normalizeSub2ApiRecoveryResult(modelInput);
  const {summary, failures, retryCodes, retryAvailable, outcomeMeta} = model;
  const unrecoverable = recoveryCount(summary.unreclaimable)
    + recoveryCount(summary.not_owned)
    + recoveryCount(summary.skipped);
  const failed = recoveryCount(summary.failed);
  const statusTone = outcomeMeta.tone || 'neutral';
  return (
    <section className={`sub2api-recovery-result compact tone-${statusTone}`} data-testid="legacy-recovery-result">
      <div className="recovery-result-head">
        <div className="recovery-result-title">
          {statusTone === 'success' ? <CircleCheck size={17}/> : statusTone === 'danger' ? <CircleX size={17}/> : statusTone === 'warning' ? <AlertCircle size={17}/> : <Info size={17}/>}
          <div><span className="detail-kicker">401 RECOVERY RESULT</span><h3>{outcomeMeta.label}</h3><p>{model.recoveryMessage}</p></div>
        </div>
        {retryAvailable && onRetry && <button className="button secondary recovery-retry-button" type="button" onClick={() => onRetry(model)} disabled={busy}>
          <RefreshCw size={14} className={busy ? 'spin' : ''}/>{busy ? '重新找回中' : `重新找回${retryCodes.length ? `（${retryCodes.length}）` : ''}`}
        </button>}
      </div>
      <div className="recovery-result-metrics">
        <div className="success"><span>已更新</span><strong>{model.updated}</strong></div>
        <div><span>本来正常</span><strong>{model.noAction}</strong></div>
        <div className={unrecoverable ? 'danger' : ''}><span>无法找回</span><strong>{unrecoverable}</strong></div>
        <div className={retryCodes.length || failed ? 'warning' : ''}><span>失败</span><strong>{failed || retryCodes.length}</strong></div>
        <div><span>已下载</span><strong>{recoveryCount(summary.downloaded)}</strong></div>
      </div>
      {failures.length ? <div className="recovery-failure-list">
        <div className="recovery-failure-head"><strong>找回失败明细</strong><span>{failures.length} 条</span></div>
        <div className="recovery-failure-items">{failures.slice(0, 20).map((failure, index) => {
          const retryable = Boolean(failure.retryable);
          const subject = failure.card_code || failure.order_no || failure.name || `项目 ${index + 1}`;
          const reason = failure.reason || failure.message || '未返回具体原因';
          const attemptLimited = ['attempt_limit', 'attempts_exhausted'].includes(String(failure.failure_bucket || failure.status || '').toLowerCase());
          return <div className={`recovery-failure-item ${retryable ? 'retryable' : 'permanent'}`} key={`${subject}-${index}`}>
            {retryable ? <RefreshCw size={13}/> : <TriangleAlert size={13}/>}<span><strong>{subject}</strong><small>{reason}{failure.provider_status ? ` · HTTP ${failure.provider_status}` : ''}</small></span><em>{retryable ? '可重试' : attemptLimited ? '已达上限' : '无法找回'}</em>
          </div>;
        })}</div>
        {failures.length > 20 && <small className="recovery-failure-more">仅显示前 20 条明细</small>}
      </div> : <div className="recovery-no-failures"><CircleCheck size={14}/>没有需要人工处理的找回项目</div>}
    </section>
  );
}

export default function ReclaimView({config, setConfig, cardCodes, setCardCodes, result, busy, onSave, onRun, onDownload, onImport, onRetry, canConfigure = true, canImport = true}) {
  const tasks = result?.all_tasks || [];
  return (
    <section className="tool-view">
      <div className="tool-grid">
        <div className="tool-panel">
          <div className="section-heading"><div><span className="detail-kicker">REDEEM SERVICE</span><h2>401 找回服务</h2><p>卡密只发送到配置的服务地址</p></div><KeyRound size={22}/></div>
          <label><span>服务地址</span><input value={config.base_url} onChange={event => setConfig({...config, base_url: event.target.value})} placeholder="https://30d.team" disabled={!canConfigure}/></label>
          {canConfigure && <button className="button secondary tool-save" onClick={onSave}><Save size={15}/>保存地址</button>}
          <label className="code-field"><span>卡密列表</span><textarea value={cardCodes} onChange={event => setCardCodes(event.target.value)} placeholder="每行输入一个卡密" rows={9}/></label>
          <div className="tool-actions"><button className="button secondary" onClick={() => onRun('health')} disabled={busy}><Activity size={15}/>检测 401</button><button className="button primary" onClick={() => onRun('reclaim')} disabled={busy}><Zap size={15}/>只找回 401</button><button className="button secondary" onClick={() => onRun('progress')} disabled={busy}><RefreshCw size={15}/>刷新进度</button></div>
        </div>
        <div className="tool-panel result-panel">
          <div className="section-heading"><div><span className="detail-kicker">RESULT</span><h2>任务结果</h2></div>{busy && <RefreshCw className="spin" size={18}/>}</div>
          {!result ? <div className="tool-empty"><KeyRound size={28}/><span>尚未执行检测</span></div> : <>
            {result.reclaim_action === 'health' && result.ok !== false && <div className="result-metrics"><div><span>总数</span><strong>{result.total ?? result.requested_cards ?? '--'}</strong></div><div><span>需找回</span><strong>{result.need_reclaim ?? result.queued ?? '--'}</strong></div><div><span>已完成</span><strong>{result.done ?? '--'}</strong></div><div><span>失败</span><strong>{result.failed ?? '--'}</strong></div></div>}
            {(result.reclaim_action !== 'health' || result.ok === false) && (
              <LegacyRecoveryResult result={result} busy={busy} onRetry={onRetry}/>
            )}
            <div className="task-list">{tasks.length ? tasks.map((task, index) => <div className="task-row" key={`${task.order_no || task.card_code}-${index}`}><div><strong>{task.card_code || task.order_no || `任务 ${index + 1}`}</strong><small>{task.message || task.status || '处理中'}</small></div>{task.download_token && task.order_no && <button className="icon-button" title="下载恢复 JSON" aria-label="下载恢复 JSON" onClick={() => onDownload(task)}><FileUp size={15}/></button>}</div>) : <p className="tool-muted">当前没有可下载任务</p>}</div>
            {canImport && result.reclaim_action !== 'health' && result.ok && <button className="button secondary import-recovered" onClick={onImport}><Upload size={15}/>转到 Sub2API 导入</button>}
          </>}
        </div>
      </div>
    </section>
  );
}
