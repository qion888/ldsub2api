import React, {useEffect, useState} from 'react';
import {Bell, Check, Layers, Mail, Plus, RefreshCw, Send, Trash2, X} from 'lucide-react';

const EMPTY_CFG = {
  enabled: false,
  smtp_host: '',
  smtp_port: 465,
  smtp_user: '',
  smtp_password: '',
  smtp_from: '',
  recipient: '',
  use_tls: true,
};

const KIND_LABELS = {restock: '补货', price_drop: '降价'};

function EmailAlertPanel({request, notify}) {
  const [cfg, setCfg] = useState(EMPTY_CFG);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [alerts, setAlerts] = useState([]);
  const [busyDelete, setBusyDelete] = useState('');
  const [catOptions, setCatOptions] = useState([]);
  const [catAlerts, setCatAlerts] = useState([]);
  const [catShopToken, setCatShopToken] = useState('');
  const [catCategory, setCatCategory] = useState('');
  const [catBusy, setCatBusy] = useState(false);
  const [catLoading, setCatLoading] = useState(false);

  const loadConfig = async () => {
    try {
      const payload = await request('/settings/alert');
      setCfg({...EMPTY_CFG, ...payload});
    } catch (err) {
      notify?.(err.message || '邮箱配置加载失败', 'error');
    }
  };
  const loadAlerts = async () => {
    try {
      const payload = await request('/alerts');
      setAlerts(Array.isArray(payload?.items) ? payload.items : []);
    } catch (err) {
      // 提醒列表加载失败不阻塞配置区
    }
  };

  useEffect(() => {
    void loadConfig();
    void loadAlerts();
    void loadCategoryOptions();
    void loadCategoryAlerts();
    setLoaded(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadCategoryOptions = async () => {
    setCatLoading(true);
    try {
      const payload = await request('/category-alerts/options');
      const shops = Array.isArray(payload?.shops) ? payload.shops : [];
      setCatOptions(shops);
      if (!catShopToken && shops.length) setCatShopToken(shops[0].shop_token);
    } catch (err) {
      notify?.(err.message || '分组列表加载失败', 'error');
    } finally {
      setCatLoading(false);
    }
  };
  const loadCategoryAlerts = async () => {
    try {
      const payload = await request('/category-alerts');
      setCatAlerts(Array.isArray(payload?.items) ? payload.items : []);
    } catch (err) {
      // non-blocking
    }
  };
  const addCategoryAlert = async () => {
    const shop = catOptions.find(entry => entry.shop_token === catShopToken);
    if (!shop || !catCategory) {
      notify?.('请先选择店铺与分组', 'error');
      return;
    }
    setCatBusy(true);
    try {
      await request('/category-alerts/add', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({shop_token: catShopToken, category: catCategory, shop_name: shop.shop_name}),
      });
      notify?.(`已订阅「${shop.shop_name} · ${catCategory}」上新提醒`);
      await loadCategoryAlerts();
    } catch (err) {
      notify?.(err.message || '订阅失败', 'error');
    } finally {
      setCatBusy(false);
    }
  };
  const removeCategoryAlert = async (entry) => {
    setCatBusy(true);
    try {
      await request('/category-alerts/remove', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({shop_token: entry.shop_token, category: entry.category}),
      });
      setCatAlerts(current => current.filter(item => !(item.shop_token === entry.shop_token && item.category === entry.category)));
    } catch (err) {
      notify?.(err.message || '删除失败', 'error');
    } finally {
      setCatBusy(false);
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      const body = {...cfg};
      if (body.smtp_password === '********' || body.smtp_password === '') delete body.smtp_password;
      const saved = await request('/settings/alert', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      setCfg({...EMPTY_CFG, ...saved});
      notify?.('邮箱提醒设置已保存');
    } catch (err) {
      notify?.(err.message || '保存失败', 'error');
    } finally {
      setSaving(false);
    }
  };

  const sendTest = async () => {
    setTesting(true);
    try {
      await request('/settings/alert/test', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
      notify?.('测试邮件已发送，请检查收件箱');
    } catch (err) {
      notify?.(err.message || '测试邮件发送失败，请检查 SMTP 配置', 'error');
    } finally {
      setTesting(false);
    }
  };

  const removeAlert = async (entry) => {
    setBusyDelete(`${entry.watch_id}:${entry.kind}`);
    try {
      await request('/alerts/remove', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({watch_id: entry.watch_id, kind: entry.kind}),
      });
      setAlerts(current => current.filter(item => !(item.watch_id === entry.watch_id && item.kind === entry.kind)));
    } catch (err) {
      notify?.(err.message || '删除失败', 'error');
    } finally {
      setBusyDelete('');
    }
  };

  const patch = (key, value) => setCfg(current => ({...current, [key]: value}));

  return (
    <section className="settings-section email-alert-section">
      <div className="settings-section-head">
        <div><span className="detail-kicker">EMAIL ALERTS</span><h3>补货 / 降价 邮箱提醒</h3></div>
        <Bell size={19}/>
      </div>
      <p className="email-alert-intro">在商品列表点 <b>铃铛图标</b> 关注商品；被关注商品<b>补货</b>或<b>降价</b>时自动发邮件通知你。</p>
      <div className="email-alert-grid">
        <label className="email-alert-switch"><span><strong>启用提醒</strong><small>总开关，关闭则不发送任何通知</small></span><button className={`switch ${cfg.enabled ? 'on' : ''}`} role="switch" aria-checked={Boolean(cfg.enabled)} onClick={() => patch('enabled', !cfg.enabled)}><span/></button></label>
        <div className="email-alert-fields">
          <label><span>收件邮箱 <small>通知发到这个邮箱</small></span><input type="email" value={cfg.recipient} onChange={event => patch('recipient', event.target.value)} placeholder="you@example.com"/></label>
        </div>
      </div>
      <div className="email-alert-smtp">
        <div className="email-alert-smtp-head"><strong><Mail size={14}/>SMTP 发件账号</strong><small>使用任意支持 SMTP 的邮箱（QQ/163/Gmail 等），填"授权码"作为密码</small></div>
        <div className="email-alert-fields">
          <label><span>SMTP 服务器</span><input value={cfg.smtp_host} onChange={event => patch('smtp_host', event.target.value)} placeholder="smtp.qq.com"/></label>
          <label><span>端口</span><input type="number" value={cfg.smtp_port} onChange={event => patch('smtp_port', Number(event.target.value) || 465)}/></label>
          <label><span>发件邮箱账号</span><input value={cfg.smtp_user} onChange={event => patch('smtp_user', event.target.value)} placeholder="sender@example.com"/></label>
          <label><span>授权码/密码</span><input type="password" value={cfg.smtp_password} onChange={event => patch('smtp_password', event.target.value)} placeholder={cfg.smtp_password === '********' ? '已保存(留空不修改)' : ''}/></label>
          <label><span>发件人显示</span><input value={cfg.smtp_from} onChange={event => patch('smtp_from', event.target.value)} placeholder="可留空，默认用账号"/></label>
          <label className="email-alert-tls-row">
            <span>传输加密</span>
            <button type="button" className={`switch ${cfg.use_tls ? 'on' : ''}`} role="switch" aria-checked={Boolean(cfg.use_tls)} onClick={() => patch('use_tls', !cfg.use_tls)}><span/></button>
            <small>使用 SSL/TLS 加密 (推荐 QQ/163 用 465+SSL)</small>
          </label>
        </div>
        <div className="settings-actions">
          <button className="button secondary" type="button" onClick={sendTest} disabled={testing || saving}><Send size={15}/>{testing ? '发送中' : '发送测试邮件'}</button>
          <button className="button primary" type="button" onClick={save} disabled={saving}><Check size={15}/>{saving ? '保存中' : '保存设置'}</button>
        </div>
      </div>
      <div className="email-alert-list-head"><strong><Bell size={14}/>当前关注商品（{alerts.length}）</strong></div>
      {alerts.length ? (
        <div className="email-alert-list">
          {alerts.map(entry => (
            <div className="email-alert-item" key={`${entry.watch_id}:${entry.kind}`}>
              <div className="email-alert-item-main"><strong title={entry.watch_name}>{entry.watch_name || `商品 #${entry.watch_id}`}</strong><small>{KIND_LABELS[entry.kind] || entry.kind}提醒{entry.last_price ? ` · 现价 ${entry.last_price}` : ''}{entry.last_notified_at ? ` · 最近通知 ${new Date(entry.last_notified_at).toLocaleString('zh-CN')}` : ' · 尚未触发'}</small></div>
              <button className="icon-button" type="button" title="移除提醒" aria-label="移除提醒" onClick={() => removeAlert(entry)} disabled={Boolean(busyDelete)}><Trash2 size={15} className={busyDelete === `${entry.watch_id}:${entry.kind}` ? 'spin' : ''}/></button>
            </div>
          ))}
        </div>
      ) : (
        <div className="settings-state email-alert-empty"><Bell size={16}/>还没有关注商品，去商品列表点铃铛开始关注</div>
      )}

      <div className="category-alert-divider"><Layers size={15}/><strong>分组上新提醒</strong><small>商家把商品下架换壳重上架也没关系——订阅某个稳定分组，出现新商品即通知</small></div>
      <div className="category-alert-picker">
        <select value={catShopToken} onChange={event => { setCatShopToken(event.target.value); setCatCategory(''); }} disabled={catLoading}>
          {catOptions.length ? catOptions.map(shop => <option value={shop.shop_token} key={shop.shop_token}>{shop.shop_name}（{shop.shop_token}）</option>) : <option value="">{catLoading ? '加载中…' : '暂无店铺数据'}</option>}
        </select>
        <select value={catCategory} onChange={event => setCatCategory(event.target.value)} disabled={catLoading || !catShopToken}>
          {catOptions.length ? (() => { const shop = catOptions.find(entry => entry.shop_token === catShopToken); const categories = shop ? shop.categories : []; return categories.length ? categories.map(cat => <option value={cat.category} key={cat.category}>{cat.category}（{cat.live_count} 件）</option>) : <option value="">该店铺暂无在售分组</option>; })() : <option value="">先选择店铺</option>}
        </select>
        <button className="button primary" type="button" onClick={addCategoryAlert} disabled={catBusy || !catCategory}><Plus size={15}/>{catBusy ? '处理中' : '订阅上新'}</button>
      </div>
      {catAlerts.length ? (
        <div className="email-alert-list category-alert-list">
          {catAlerts.map(entry => (
            <div className="email-alert-item" key={`${entry.shop_token}:${entry.category}`}>
              <div className="email-alert-item-main"><strong>{entry.shop_name} · {entry.category}</strong><small>关注 {entry.watched_count} 件在售 · {entry.last_notified_at ? `最近通知 ${new Date(entry.last_notified_at).toLocaleString('zh-CN')}` : '尚未触发'}</small></div>
              <button className="icon-button" type="button" title="取消订阅" aria-label="取消订阅" onClick={() => removeCategoryAlert(entry)} disabled={catBusy}><Trash2 size={15}/></button>
            </div>
          ))}
        </div>
      ) : (
        <div className="settings-state email-alert-empty"><Layers size={16}/>还没有订阅任何分组，选一个店铺分组开始</div>
      )}
    </section>
  );
}

export default EmailAlertPanel;
