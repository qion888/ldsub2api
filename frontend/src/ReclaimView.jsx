import React from 'react';
import {Activity, FileUp, KeyRound, RefreshCw, Save, Upload, Zap} from 'lucide-react';

export default function ReclaimView({config, setConfig, cardCodes, setCardCodes, result, busy, onSave, onRun, onDownload, onImport}) {
  const tasks = result?.all_tasks || [];
  return (
    <section className="tool-view">
      <div className="tool-grid">
        <div className="tool-panel">
          <div className="section-heading"><div><span className="detail-kicker">REDEEM SERVICE</span><h2>401 找回服务</h2><p>卡密只发送到配置的服务地址</p></div><KeyRound size={22}/></div>
          <label><span>服务地址</span><input value={config.base_url} onChange={event => setConfig({...config, base_url: event.target.value})} placeholder="https://30d.team"/></label>
          <button className="button secondary tool-save" onClick={onSave}><Save size={15}/>保存地址</button>
          <label className="code-field"><span>卡密列表</span><textarea value={cardCodes} onChange={event => setCardCodes(event.target.value)} placeholder="每行输入一个卡密" rows={9}/></label>
          <div className="tool-actions"><button className="button secondary" onClick={() => onRun('health')} disabled={busy}><Activity size={15}/>检测 401</button><button className="button primary" onClick={() => onRun('reclaim')} disabled={busy}><Zap size={15}/>只找回 401</button><button className="button secondary" onClick={() => onRun('progress')} disabled={busy}><RefreshCw size={15}/>刷新进度</button></div>
        </div>
        <div className="tool-panel result-panel">
          <div className="section-heading"><div><span className="detail-kicker">RESULT</span><h2>任务结果</h2></div>{busy && <RefreshCw className="spin" size={18}/>}</div>
          {!result ? <div className="tool-empty"><KeyRound size={28}/><span>尚未执行检测</span></div> : <>
            <div className="result-metrics"><div><span>总数</span><strong>{result.total ?? result.requested_cards ?? '--'}</strong></div><div><span>需找回</span><strong>{result.need_reclaim ?? result.queued ?? '--'}</strong></div><div><span>已完成</span><strong>{result.done ?? '--'}</strong></div><div><span>失败</span><strong>{result.failed ?? '--'}</strong></div></div>
            <div className="task-list">{tasks.length ? tasks.map((task, index) => <div className="task-row" key={`${task.order_no || task.card_code}-${index}`}><div><strong>{task.card_code || task.order_no || `任务 ${index + 1}`}</strong><small>{task.message || task.status || '处理中'}</small></div>{task.download_token && task.order_no && <button className="icon-button" title="下载恢复 JSON" aria-label="下载恢复 JSON" onClick={() => onDownload(task)}><FileUp size={15}/></button>}</div>) : <p className="tool-muted">当前没有可下载任务</p>}</div>
            {result.ok && <button className="button secondary import-recovered" onClick={onImport}><Upload size={15}/>转到 Sub2API 导入</button>}
          </>}
        </div>
      </div>
    </section>
  );
}
