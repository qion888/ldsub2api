import React, {useEffect, useMemo, useState} from 'react';
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  CloudDownload,
  ExternalLink,
  GitBranch,
  KeyRound,
  LockKeyhole,
  PackageCheck,
  RefreshCw,
  Save,
  SlidersHorizontal,
  Settings2,
  ShieldCheck,
  Trash2,
  UserCircle,
  UserPlus,
  Users,
  X,
} from 'lucide-react';

import {AUTH_MODES, isAdmin, normalizeMode, normalizeUser, roleLabel} from './authModel.js';
import {normalizeBackupList, normalizeVersionInfo, versionBlockReason, versionStatusLabel} from './versionModel.js';

const EMPTY_BASIC = {site_name: '', announcement: '', contact_email: '', timezone: 'Asia/Shanghai', base_url: ''};
const EMPTY_SYSTEM = {
  session_ttl_hours: 24,
  maintenance_mode: false,
  log_level: 'info',
  force_login: true,
  allow_user_reclaim: false,
  allow_user_sub2api_import: false,
};

function asUsers(payload) {
  const list = Array.isArray(payload) ? payload : payload?.items;
  return Array.isArray(list) ? list.map(normalizeUser).filter(Boolean) : [];
}

function settingsPart(payload, key, fallback) {
  return payload?.[key] && typeof payload[key] === 'object' ? {...fallback, ...payload[key]} : {...fallback};
}

function formatVersionDate(value) {
  if (!value) return '未知时间';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '未知时间' : date.toLocaleString('zh-CN', {hour12: false});
}

function formatBackupSize(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function backupReasonLabel(value) {
  return ({
    manual: '手动备份',
    'before-update': '升级前自动备份',
    'before-restore': '恢复前安全备份',
    'before-rollback': '回退前安全备份',
  })[String(value || '')] || '系统备份';
}

function ErrorNotice({message}) {
  return message ? <div className="settings-error" role="alert">{message}</div> : null;
}

export default function SettingsView({request, user, mode, notify, onUserUpdated, onModeChange, onPasswordChanged}) {
  const admin = isAdmin(user);
  const [tab, setTab] = useState(admin ? 'basic' : 'profile');
  const [basic, setBasic] = useState(EMPTY_BASIC);
  const [system, setSystem] = useState(EMPTY_SYSTEM);
  const [settingsMode, setSettingsMode] = useState(normalizeMode(mode));
  const [allowRegistration, setAllowRegistration] = useState(false);
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [usersBusy, setUsersBusy] = useState(false);
  const [error, setError] = useState('');
  const [newUser, setNewUser] = useState({username: '', password: '', display_name: '', role: 'user'});
  const [creatingUser, setCreatingUser] = useState(false);
  const [resetId, setResetId] = useState(null);
  const [resetPassword, setResetPassword] = useState('');
  const [resetBusy, setResetBusy] = useState(false);
  const [passwordForm, setPasswordForm] = useState({current_password: '', password: '', confirm_password: ''});
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [versionInfo, setVersionInfo] = useState(null);
  const [versionBusy, setVersionBusy] = useState('');
  const [versionError, setVersionError] = useState('');
  const [backupBusy, setBackupBusy] = useState('');

  const loadUsers = async () => {
    if (!admin) return;
    setUsersBusy(true);
    try {
      const payload = await request('/users');
      setUsers(asUsers(payload));
    } catch (requestError) {
      setError(requestError.message || '用户列表加载失败');
    } finally {
      setUsersBusy(false);
    }
  };

  const loadVersion = async () => {
    if (!admin) return;
    setVersionBusy('load');
    setVersionError('');
    try {
      const info = await request('/version');
      // The aggregate endpoint already includes backup metadata. Only use the
      // dedicated endpoint when talking to an older backend without that field.
      let backups = info?.backups;
      if (!backups) {
        try {
          backups = await request('/version/backups');
        } catch (requestError) {
          if (requestError.status !== 404) throw requestError;
        }
      }
      setVersionInfo(normalizeVersionInfo({...info, backups: normalizeBackupList(backups)}));
    } catch (requestError) {
      setVersionError(requestError.message || '版本信息加载失败');
    } finally {
      setVersionBusy('');
    }
  };

  const loadSettings = async () => {
    setLoading(true);
    setError('');
    if (!admin) {
      setLoading(false);
      return;
    }
    try {
      let payload;
      try {
        payload = await request('/settings');
      } catch (requestError) {
        if (requestError.status !== 404) throw requestError;
        const [basicPayload, systemPayload] = await Promise.all([
          request('/settings/basic'),
          admin ? request('/settings/system') : Promise.resolve({}),
        ]);
        payload = {basic: basicPayload, system: systemPayload};
      }
      setBasic(settingsPart(payload, 'basic', EMPTY_BASIC));
      setSystem(settingsPart(payload, 'system', EMPTY_SYSTEM));
      setSettingsMode(normalizeMode(payload?.mode || mode));
      setAllowRegistration(Boolean(payload?.allow_registration));
      // The settings form is independent from the heavier admin panels. Paint
      // it as soon as its own payload is ready, then hydrate secondary data.
      setLoading(false);
      if (admin) {
        void loadUsers();
        void loadVersion();
      }
    } catch (requestError) {
      setError(requestError.message || '设置加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setTab(admin ? 'basic' : 'profile');
    loadSettings();
    // The settings endpoint is scoped by the authenticated session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [admin, user?.id]);

  const saveBasic = async () => {
    setSaving(true);
    setError('');
    try {
      let result;
      try {
        result = await request('/settings/basic', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(basic)});
      } catch (requestError) {
        if (requestError.status !== 404) throw requestError;
        result = await request('/settings', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({basic})});
      }
      setBasic(settingsPart(result, 'basic', basic));
      notify?.('基础设置已保存');
    } catch (requestError) {
      setError(requestError.message || '基础设置保存失败');
    } finally {
      setSaving(false);
    }
  };

  const saveSystem = async () => {
    if (!admin) return;
    setSaving(true);
    setError('');
    try {
      const payload = {mode: settingsMode, allow_registration: allowRegistration, ...system};
      let result;
      try {
        result = await request('/settings/system', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      } catch (requestError) {
        if (requestError.status !== 404) throw requestError;
        result = await request('/settings', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode: settingsMode, allow_registration: allowRegistration, system})});
      }
      setSystem(settingsPart(result, 'system', system));
      setSettingsMode(normalizeMode(result?.mode || settingsMode));
      if (result?.allow_registration !== undefined) setAllowRegistration(Boolean(result.allow_registration));
      const nextMode = normalizeMode(result?.mode || settingsMode);
      onModeChange?.({
        mode: nextMode,
        auth_required: result?.auth_required,
        force_login: result?.force_login ?? result?.system?.force_login,
        allow_user_reclaim: result?.allow_user_reclaim ?? result?.system?.allow_user_reclaim,
        allow_user_sub2api_import: result?.allow_user_sub2api_import ?? result?.system?.allow_user_sub2api_import,
      });
      notify?.('系统设置已保存');
    } catch (requestError) {
      setError(requestError.message || '系统设置保存失败');
    } finally {
      setSaving(false);
    }
  };

  const createUser = async event => {
    event.preventDefault();
    if (!newUser.username.trim() || !newUser.password) {
      setError('请输入新用户账号和密码');
      return;
    }
    setCreatingUser(true);
    setError('');
    try {
      const created = await request('/users', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...newUser, username: newUser.username.trim()}),
      });
      const normalized = normalizeUser(created?.user || created);
      if (normalized) setUsers(current => [...current.filter(item => item.id !== normalized.id), normalized]);
      setNewUser({username: '', password: '', display_name: '', role: 'user'});
      notify?.('用户已创建');
      onUserUpdated?.(normalized);
    } catch (requestError) {
      setError(requestError.message || '用户创建失败');
    } finally {
      setCreatingUser(false);
    }
  };

  const updateUser = async (entry, patch) => {
    setUsersBusy(true);
    setError('');
    try {
      const result = await request(`/users/${entry.id}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(patch),
      });
      const normalized = normalizeUser(result?.user || result) || {...entry, ...patch};
      setUsers(current => current.map(item => item.id === entry.id ? normalized : item));
      notify?.('用户设置已更新');
    } catch (requestError) {
      setError(requestError.message || '用户更新失败');
    } finally {
      setUsersBusy(false);
    }
  };

  const removeUser = async entry => {
    if (entry.id === user?.id) return;
    if (!window.confirm(`删除用户“${entry.username}”？`)) return;
    setUsersBusy(true);
    setError('');
    try {
      await request(`/users/${entry.id}`, {method: 'DELETE'});
      setUsers(current => current.filter(item => item.id !== entry.id));
      notify?.('用户已删除');
    } catch (requestError) {
      setError(requestError.message || '用户删除失败');
    } finally {
      setUsersBusy(false);
    }
  };

  const resetUserPassword = async event => {
    event.preventDefault();
    if (!resetId || !resetPassword) return;
    setResetBusy(true);
    setError('');
    try {
      await request(`/users/${resetId}/password`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({password: resetPassword}),
      });
      setResetId(null);
      setResetPassword('');
      notify?.('密码已重置');
    } catch (requestError) {
      setError(requestError.message || '密码重置失败');
    } finally {
      setResetBusy(false);
    }
  };

  const changeOwnPassword = async event => {
    event.preventDefault();
    const currentPassword = passwordForm.current_password;
    const nextPassword = passwordForm.password;
    if (!currentPassword || !nextPassword) {
      setError('请输入当前密码和新密码');
      return;
    }
    if (nextPassword !== passwordForm.confirm_password) {
      setError('两次输入的新密码不一致');
      return;
    }
    setPasswordBusy(true);
    setError('');
    try {
      await request('/auth/password', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({current_password: currentPassword, password: nextPassword}),
      });
      setPasswordForm({current_password: '', password: '', confirm_password: ''});
      notify?.('密码已更新，请重新登录');
      await onPasswordChanged?.();
    } catch (requestError) {
      setError(requestError.message || '密码更新失败');
    } finally {
      setPasswordBusy(false);
    }
  };

  const checkVersionUpdates = async () => {
    setVersionBusy('check');
    setVersionError('');
    try {
      const result = normalizeVersionInfo(await request('/version/check', {method: 'POST'}));
      setVersionInfo(result);
      notify?.(result.message || (result.update_available ? '发现可用更新' : '当前已是最新版本'));
    } catch (requestError) {
      setVersionError(requestError.message || '检查更新失败');
    } finally {
      setVersionBusy('');
    }
  };

  const installVersionUpdate = async () => {
    setVersionBusy('update');
    setVersionError('');
    try {
      let current = versionInfo;
      if (!current?.update_ready) {
        current = normalizeVersionInfo(await request('/version/check', {method: 'POST'}));
        setVersionInfo(current);
      }
      if (!current.update_available || !current.update_ready) {
        notify?.(current.message || versionBlockReason(current) || '当前已是最新版本');
        return;
      }
      if (!window.confirm(`将从 GitHub 更新到 ${current.latest_version || current.latest_short_commit}，是否继续？`)) return;
      const result = normalizeVersionInfo(await request('/version/update', {method: 'POST'}));
      setVersionInfo(previous => {
        const previousInfo = normalizeVersionInfo(previous || {});
        const candidates = [
          ...(result.backup ? [result.backup] : []),
          ...(result.backups?.items || []),
          ...previousInfo.backups.items,
        ];
        const items = candidates.filter((item, index, list) => (
          item?.id && list.findIndex(candidate => candidate?.id === item.id) === index
        ));
        return normalizeVersionInfo({
          ...previousInfo,
          ...result,
          backups: {
            ...previousInfo.backups,
            ...result.backups,
            ok: true,
            items,
            total: items.length,
            latest: items[0] || null,
          },
        });
      });
      notify?.(result.message || '版本更新完成');
    } catch (requestError) {
      setVersionError(requestError.message || '版本更新失败');
    } finally {
      setVersionBusy('');
    }
  };

  const createVersionBackup = async () => {
    setBackupBusy('create');
    setVersionError('');
    try {
      const result = await request('/version/backup', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({reason: 'manual'}),
      });
      const created = result?.backup;
      setVersionInfo(current => normalizeVersionInfo({
        ...(current || {}),
        database: result?.database || current?.database,
        backups: {
          ...(current?.backups || {}),
          items: created ? [created, ...(current?.backups?.items || []).filter(item => item.id !== created.id)] : current?.backups?.items,
          total: created ? Number(current?.backups?.total || 0) + 1 : current?.backups?.total,
          latest: created || current?.backups?.latest,
        },
      }));
      notify?.('数据备份已完成');
    } catch (requestError) {
      setVersionError(requestError.message || '数据备份失败');
    } finally {
      setBackupBusy('');
    }
  };

  const deleteVersionBackup = async backup => {
    if (!backup?.id) return;
    if (!window.confirm(`删除 ${formatVersionDate(backup.created_at)} 的数据备份？删除后无法恢复。`)) return;
    setBackupBusy(`delete:${backup.id}`);
    setVersionError('');
    try {
      const result = await request(`/version/backups/${encodeURIComponent(backup.id)}`, {method: 'DELETE'});
      setVersionInfo(current => normalizeVersionInfo({...current, ...result, backups: result?.backups || current?.backups}));
      notify?.(result.message || '数据备份已删除');
    } catch (requestError) {
      setVersionError(requestError.message || '数据备份删除失败');
    } finally {
      setBackupBusy('');
    }
  };

  const restoreVersionBackup = async backup => {
    if (!backup?.id) return;
    if (!window.confirm(`将数据库恢复到 ${formatVersionDate(backup.created_at)} 的备份，现有数据会自动备份，继续吗？`)) return;
    setBackupBusy(`restore:${backup.id}`);
    setVersionError('');
    try {
      const result = await request('/version/restore', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({backup_id: backup.id}),
      });
      setVersionInfo(current => normalizeVersionInfo({...current, ...result, backups: current?.backups}));
      notify?.(result.message || '数据已恢复，请重启服务');
    } catch (requestError) {
      setVersionError(requestError.message || '数据恢复失败');
    } finally {
      setBackupBusy('');
    }
  };

  const rollbackVersionUpdate = async () => {
    const lastUpdate = versionInfo?.last_update;
    if (!lastUpdate?.can_rollback) return;
    if (!window.confirm('将恢复升级前的代码，并保留现有数据。回退后需重启服务，继续吗？')) return;
    setBackupBusy('rollback');
    setVersionError('');
    try {
      const result = await request('/version/rollback', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({backup_id: lastUpdate.backup_id, restore_data: false}),
      });
      setVersionInfo(current => normalizeVersionInfo({...current, ...result, status: 'updated', needs_restart: true, last_update: null}));
      notify?.(result.message || '代码已回退，请重启服务');
    } catch (requestError) {
      setVersionError(requestError.message || '代码回退失败');
    } finally {
      setBackupBusy('');
    }
  };

  const userCountLabel = useMemo(() => `${users.length} 个用户`, [users.length]);

  return <section className="settings-view">
    <div className="settings-head">
      <div><span className="detail-kicker">SYSTEM CONTROL</span><h2>设置</h2><p>基础配置、系统模式、版本与账号管理</p></div>
      <button className="icon-button" type="button" aria-label="刷新设置" title="刷新设置" onClick={loadSettings} disabled={loading || saving || usersBusy || Boolean(versionBusy)}><RefreshCw size={16} className={loading ? 'spin' : ''}/></button>
    </div>
    <div className="settings-layout">
      <nav className="settings-tabs" aria-label="设置分类">
        {admin && <button type="button" className={tab === 'basic' ? 'active' : ''} onClick={() => setTab('basic')}><SlidersHorizontal size={16}/>基础设置</button>}
        <button type="button" className={tab === 'profile' ? 'active' : ''} onClick={() => setTab('profile')}><UserCircle size={16}/>我的账号</button>
        {admin && <button type="button" className={tab === 'system' ? 'active' : ''} onClick={() => setTab('system')}><Settings2 size={16}/>系统设置</button>}
        {admin && <button type="button" className={tab === 'version' ? 'active' : ''} onClick={() => setTab('version')}><GitBranch size={16}/>版本与更新</button>}
        {admin && <button type="button" className={tab === 'users' ? 'active' : ''} onClick={() => setTab('users')}><Users size={16}/>用户管理</button>}
      </nav>
      <div className="settings-content">
        <ErrorNotice message={error}/>
        {loading ? <div className="settings-state" role="status"><RefreshCw size={18} className="spin"/>正在读取设置</div> : tab === 'profile' ? <section className="settings-section profile-section">
          <div className="settings-section-head"><div><span className="detail-kicker">ACCOUNT</span><h3>我的账号</h3></div><ShieldCheck size={19}/></div>
          <div className="profile-card"><span className="profile-avatar"><UserCircle size={25}/></span><div><strong>{user?.display_name || user?.username || '本机用户'}</strong><small>{user?.username || 'guest'} · {roleLabel(user)}</small></div><span className="settings-badge">{normalizeMode(mode) === AUTH_MODES.EXTERNAL ? '对外模式' : '自用模式'}</span></div>
          <div className="settings-note">普通用户可使用监控与订单查询；管理配置由管理员维护。</div>
          {user && <form className="password-form" onSubmit={changeOwnPassword}>
            <div className="settings-section-head password-form-head"><div><span className="detail-kicker">PASSWORD</span><h3>修改登录密码</h3></div><KeyRound size={19}/></div>
            <div className="settings-form-grid">
              <label><span>当前密码</span><input type="password" value={passwordForm.current_password} onChange={event => setPasswordForm({...passwordForm, current_password: event.target.value})} autoComplete="current-password" placeholder="输入当前密码"/></label>
              <label><span>新密码</span><input type="password" value={passwordForm.password} onChange={event => setPasswordForm({...passwordForm, password: event.target.value})} autoComplete="new-password" placeholder="至少 8 个字符"/></label>
              <label><span>确认新密码</span><input type="password" value={passwordForm.confirm_password} onChange={event => setPasswordForm({...passwordForm, confirm_password: event.target.value})} autoComplete="new-password" placeholder="再次输入新密码"/></label>
            </div>
            <div className="settings-actions"><button className="button primary" type="submit" disabled={passwordBusy || !passwordForm.current_password || !passwordForm.password || !passwordForm.confirm_password}><KeyRound size={15}/>{passwordBusy ? '更新中' : '更新密码'}</button></div>
          </form>}
        </section> : tab === 'basic' ? <section className="settings-section">
          <div className="settings-section-head"><div><span className="detail-kicker">BASIC</span><h3>基础设置</h3></div><SlidersHorizontal size={19}/></div>
          <div className="settings-form-grid">
            <label><span>站点名称</span><input value={basic.site_name} onChange={event => setBasic({...basic, site_name: event.target.value})} placeholder="链动监控台"/></label>
            <label><span>时区</span><input value={basic.timezone} onChange={event => setBasic({...basic, timezone: event.target.value})} placeholder="Asia/Shanghai"/></label>
            <label className="wide"><span>公告</span><textarea value={basic.announcement} onChange={event => setBasic({...basic, announcement: event.target.value})} rows="3" placeholder="可选公告"/></label>
            <label><span>联系邮箱</span><input type="email" value={basic.contact_email} onChange={event => setBasic({...basic, contact_email: event.target.value})} placeholder="admin@example.com"/></label>
            <label><span>服务地址</span><input value={basic.base_url} onChange={event => setBasic({...basic, base_url: event.target.value})} placeholder="http://127.0.0.1:8000"/></label>
          </div>
          <div className="settings-actions"><button className="button primary" type="button" onClick={saveBasic} disabled={saving}><Save size={15}/>{saving ? '保存中' : '保存基础设置'}</button></div>
        </section> : tab === 'system' ? <section className="settings-section">
          <div className="settings-section-head"><div><span className="detail-kicker">SYSTEM</span><h3>系统设置</h3></div><LockKeyhole size={19}/></div>
          <div className="settings-form-grid">
            <label><span>运行模式</span><select value={settingsMode} onChange={event => { const nextMode = normalizeMode(event.target.value); setSettingsMode(nextMode); if (nextMode === AUTH_MODES.EXTERNAL && settingsMode !== AUTH_MODES.EXTERNAL) setSystem({...system, force_login: true, allow_user_reclaim: false, allow_user_sub2api_import: false}); }}><option value={AUTH_MODES.SELF_USE}>自用模式</option><option value={AUTH_MODES.EXTERNAL}>对外模式</option></select></label>
            <label><span>会话有效期（小时）</span><input type="number" min="1" max="720" value={system.session_ttl_hours} onChange={event => setSystem({...system, session_ttl_hours: Math.max(1, Math.min(720, Number(event.target.value) || 1))})}/></label>
            <label><span>日志级别</span><select value={system.log_level} onChange={event => setSystem({...system, log_level: event.target.value})}><option value="debug">debug</option><option value="info">info</option><option value="warning">warning</option><option value="error">error</option></select></label>
            <label className="auth-check-row"><input type="checkbox" checked={allowRegistration} onChange={event => setAllowRegistration(event.target.checked)} disabled={settingsMode !== AUTH_MODES.EXTERNAL}/><span><strong>允许公开注册</strong><small>对外模式下开放注册入口</small></span></label>
            <label className="auth-check-row"><input type="checkbox" checked={settingsMode === AUTH_MODES.EXTERNAL ? Boolean(system.force_login) : false} onChange={event => setSystem({...system, force_login: event.target.checked})} disabled={settingsMode !== AUTH_MODES.EXTERNAL}/><span><strong>强制登录</strong><small>关闭后允许匿名查看首页与公开数据</small></span></label>
            <label className="auth-check-row"><input type="checkbox" checked={settingsMode === AUTH_MODES.EXTERNAL ? Boolean(system.allow_user_reclaim) : true} onChange={event => setSystem({...system, allow_user_reclaim: event.target.checked})} disabled={settingsMode !== AUTH_MODES.EXTERNAL}/><span><strong>普通用户使用 401 找回</strong><small>仅开放找回流程，不开放系统配置</small></span></label>
            <label className="auth-check-row"><input type="checkbox" checked={settingsMode === AUTH_MODES.EXTERNAL ? Boolean(system.allow_user_sub2api_import) : true} onChange={event => setSystem({...system, allow_user_sub2api_import: event.target.checked})} disabled={settingsMode !== AUTH_MODES.EXTERNAL}/><span><strong>普通用户使用 Sub2API 导入</strong><small>仅开放账号 JSON 导入，不开放管理密钥</small></span></label>
            <label className="auth-check-row"><input type="checkbox" checked={Boolean(system.maintenance_mode)} onChange={event => setSystem({...system, maintenance_mode: event.target.checked})}/><span><strong>维护模式</strong><small>暂时阻止写入操作</small></span></label>
          </div>
          <div className="settings-mode-callout"><ShieldCheck size={16}/><span>{settingsMode === AUTH_MODES.EXTERNAL ? '对外模式：登录后按角色显示功能。' : '自用模式：保留本机工作流，可选择登录管理。'}</span></div>
          <div className="settings-actions"><button className="button primary" type="button" onClick={saveSystem} disabled={saving}><Save size={15}/>{saving ? '保存中' : '保存系统设置'}</button></div>
        </section> : tab === 'users' ? <section className="settings-section users-section">
          <div className="settings-section-head"><div><span className="detail-kicker">USERS</span><h3>用户管理</h3></div><span className="settings-count">{userCountLabel}</span></div>
          <form className="user-create-form" onSubmit={createUser}>
            <div className="settings-form-grid"><label><span>账号</span><input value={newUser.username} onChange={event => setNewUser({...newUser, username: event.target.value})} autoComplete="off" placeholder="新用户账号"/></label><label><span>临时密码</span><input type="password" value={newUser.password} onChange={event => setNewUser({...newUser, password: event.target.value})} autoComplete="new-password" placeholder="初始密码"/></label><label><span>显示名称</span><input value={newUser.display_name} onChange={event => setNewUser({...newUser, display_name: event.target.value})} placeholder="可选"/></label><label><span>角色</span><select value={newUser.role} onChange={event => setNewUser({...newUser, role: event.target.value})}><option value="user">普通用户</option><option value="admin">管理员</option></select></label></div>
            <button className="button primary" type="submit" disabled={creatingUser}><UserPlus size={15}/>{creatingUser ? '创建中' : '创建用户'}</button>
          </form>
          <div className="user-table" role="table" aria-label="用户列表"><div className="user-row user-row-head" role="row"><span>账号</span><span>角色</span><span>状态</span><span>操作</span></div>{users.map(entry => <div className="user-row" role="row" key={entry.id}><span><strong>{entry.display_name}</strong><small>{entry.username}</small></span><span><select value={entry.role} onChange={event => updateUser(entry, {role: event.target.value})} disabled={usersBusy || entry.id === user?.id}><option value="user">普通用户</option><option value="admin">管理员</option></select></span><span><em className={`settings-user-status ${entry.enabled === false ? 'disabled' : ''}`}>{entry.enabled === false ? '已停用' : '正常'}</em></span><span className="user-row-actions"><button className="icon-button" type="button" title="重置密码" aria-label={`重置 ${entry.username} 的密码`} onClick={() => { setResetId(entry.id); setResetPassword(''); }} disabled={usersBusy}><KeyRound size={15}/></button><button className="icon-button danger-icon" type="button" title="删除用户" aria-label={`删除用户 ${entry.username}`} onClick={() => removeUser(entry)} disabled={usersBusy || entry.id === user?.id}><Trash2 size={15}/></button></span></div>)}{!users.length && <div className="settings-state">暂无其他用户</div>}</div>
        </section> : <section className="settings-section version-section">
          <div className="settings-section-head"><div><span className="detail-kicker">VERSION CONTROL</span><h3>版本与更新</h3></div><PackageCheck size={19}/></div>
          {versionError && <div className="settings-error version-error" role="alert">{versionError}</div>}
          {versionInfo ? <>
            <div className="version-overview">
              <div className="version-identity"><span className="version-mark"><PackageCheck size={21}/></span><div><small>当前版本</small><strong>v{versionInfo.current_version.replace(/^v/i, '')}</strong><code>{versionInfo.current_short_commit || (versionInfo.installation_mode === 'archive' ? 'download' : 'unknown')}</code></div></div>
              <span className={`version-status version-status-${versionInfo.status}`}>{versionInfo.needs_restart ? <AlertTriangle size={14}/> : versionInfo.status === 'up_to_date' ? <CheckCircle2 size={14}/> : <GitBranch size={14}/>} {versionStatusLabel(versionInfo)}</span>
            </div>
            <div className="version-meta-grid">
              <div><small>安装来源</small><strong>{versionInfo.installation_mode === 'archive' ? 'GitHub 下载版' : (versionInfo.branch || 'detached HEAD')}</strong><span>跟踪 {versionInfo.target_branch}</span></div>
              <div><small>GitHub 最新</small><strong>{versionInfo.latest_version ? `v${versionInfo.latest_version.replace(/^v/i, '')}` : '尚未检查'}</strong><span>{versionInfo.latest_short_commit || '点击检查更新'}{(versionInfo.remote_ahead_by || versionInfo.local_ahead_by) ? ` · 远端 +${versionInfo.remote_ahead_by} / 本地 +${versionInfo.local_ahead_by}` : ''}</span></div>
              <div><small>{versionInfo.installation_mode === 'archive' ? '本地数据' : '工作树'}</small><strong>{versionInfo.installation_mode === 'archive' ? '原位置保留' : (versionInfo.worktree_clean ? '干净' : `${versionInfo.dirty_file_count} 项改动`)}</strong><span>{versionInfo.installation_mode === 'archive' ? '数据库、配置与依赖不会覆盖' : (versionInfo.repository_matches ? '更新源已核对' : '更新源不一致')}</span></div>
              <div><small>更新方式</small><strong>{versionInfo.installation_mode === 'archive' ? 'GitHub 源码包更新' : 'GitHub 快进更新'}</strong><span>{versionInfo.installation_mode === 'archive' ? '升级前备份代码和数据' : '仅允许 fast-forward'}</span></div>
            </div>
            {(versionInfo.message || versionBlockReason(versionInfo) || versionInfo.needs_restart) && <div className={`version-callout ${versionInfo.needs_restart || versionBlockReason(versionInfo) ? 'warning' : 'success'}`}>
              {versionInfo.needs_restart || versionBlockReason(versionInfo) ? <AlertTriangle size={16}/> : <CheckCircle2 size={16}/>}<span>{versionInfo.needs_restart ? '新版本已安装，请重启后端与前端服务。' : versionBlockReason(versionInfo) || versionInfo.message}</span>
            </div>}
            {versionInfo.checked_at && <div className="version-checked">最近检查：{new Date(versionInfo.checked_at).toLocaleString('zh-CN', {hour12: false})}</div>}
            <div className="version-data-panel">
              <div className="version-panel-head">
                <div><span className="detail-kicker">DATA INTEGRITY</span><h4>数据兼容与备份</h4></div>
                <span className={`version-integrity ${versionInfo.database.integrity ? 'ok' : 'error'}`}><ShieldCheck size={14}/>{versionInfo.database.integrity ? 'SQLite 完整性正常' : '需要检查数据库'}</span>
              </div>
              <div className="version-data-grid">
                <div><small>数据库</small><strong>{versionInfo.database.file_name || 'monitor.db'}</strong><span>{formatBackupSize(versionInfo.database.size_bytes)} · schema {versionInfo.database.schema_version ?? 0}</span>{versionInfo.database.path && <code className="version-path" title={versionInfo.database.path}>{versionInfo.database.path}</code>}</div>
                <div><small>数据兼容</small><strong>{versionInfo.database.compatibility === 'sqlite-preserved' ? '原库保留' : versionInfo.database.compatibility}</strong><span>{versionInfo.database.integrity_message || '升级不修改 SQLite 结构'}</span></div>
                <div><small>备份记录</small><strong>{versionInfo.backups.total || versionInfo.database.backup_count} 份</strong><span>{versionInfo.backups.latest ? `最近 ${formatVersionDate(versionInfo.backups.latest.created_at)}` : '尚无备份'}</span></div>
              </div>
              <div className="version-panel-actions">
                <button className="button secondary" type="button" onClick={createVersionBackup} disabled={Boolean(versionBusy) || Boolean(backupBusy)}><Save size={15}/>{backupBusy === 'create' ? '备份中' : '立即备份'}</button>
                {versionInfo.last_update?.can_rollback && <button className="button secondary" type="button" onClick={rollbackVersionUpdate} disabled={Boolean(versionBusy) || Boolean(backupBusy)}><RefreshCw size={15}/>{backupBusy === 'rollback' ? '回退中' : '回退上次升级'}</button>}
              </div>
            </div>
            <div className="version-backups-panel">
              <div className="version-panel-head">
                <div><span className="detail-kicker">BACKUP HISTORY</span><h4>数据备份记录</h4></div>
                <span className="settings-count">{versionInfo.backups.total} 份</span>
              </div>
              {versionInfo.backups.items.length ? <div className="version-backup-list">
                {versionInfo.backups.items.map(backup => <div className="version-backup-row" key={backup.id}>
                  <div className="version-backup-main"><strong>{backupReasonLabel(backup.reason)}</strong><small>{formatVersionDate(backup.created_at)} · {formatBackupSize(backup.size_bytes)} · schema {backup.schema_version ?? 0}</small>{backup.path && <code className="version-backup-path" title={backup.path}>{backup.path}</code>}</div>
                  <code title={backup.sha256}>{backup.sha256 ? `${backup.sha256.slice(0, 12)}…` : '无校验摘要'}</code>
                  <div className="version-backup-actions"><button className="icon-button" type="button" title="恢复此备份" aria-label={`恢复 ${formatVersionDate(backup.created_at)} 的备份`} onClick={() => restoreVersionBackup(backup)} disabled={Boolean(versionBusy) || Boolean(backupBusy)}><RefreshCw size={15} className={backupBusy === `restore:${backup.id}` ? 'spin' : ''}/></button><button className="icon-button danger-icon" type="button" title="删除此备份" aria-label={`删除 ${formatVersionDate(backup.created_at)} 的备份`} onClick={() => deleteVersionBackup(backup)} disabled={Boolean(versionBusy) || Boolean(backupBusy)}><Trash2 size={15} className={backupBusy === `delete:${backup.id}` ? 'spin' : ''}/></button></div>
                </div>)}
              </div> : <div className="settings-state version-backup-empty">暂无数据备份，升级前会自动创建安全备份</div>}
            </div>
          </> : <div className="settings-state" role="status"><RefreshCw size={18} className={versionBusy ? 'spin' : ''}/>{versionBusy ? '正在读取版本信息' : '暂无版本信息'}</div>}
          <div className="settings-actions version-actions">
            {versionInfo?.repository_url && <a className="button secondary" href={versionInfo.repository_url} target="_blank" rel="noreferrer"><ExternalLink size={15}/>查看 GitHub</a>}
            <button className="button secondary" type="button" onClick={checkVersionUpdates} disabled={Boolean(versionBusy)}><RefreshCw size={15} className={versionBusy === 'check' ? 'spin' : ''}/>{versionBusy === 'check' ? '检查中' : '检查更新'}</button>
            {versionInfo && <button className="button primary" type="button" onClick={installVersionUpdate} disabled={Boolean(versionBusy) || (versionInfo.update_available && !versionInfo.update_ready)}><CloudDownload size={15}/>{versionBusy === 'update' ? '更新中' : '立即更新'}</button>}
          </div>
        </section>}
      </div>
    </div>
    {resetId && <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && setResetId(null)}><form className="settings-modal" role="dialog" aria-modal="true" aria-label="重置用户密码" onSubmit={resetUserPassword}><div className="settings-section-head"><div><span className="detail-kicker">PASSWORD</span><h3>重置密码</h3></div><button className="icon-button" type="button" aria-label="关闭" onClick={() => setResetId(null)}><X size={15}/></button></div><label><span>新密码</span><input type="password" value={resetPassword} onChange={event => setResetPassword(event.target.value)} autoComplete="new-password" autoFocus placeholder="输入新密码"/></label><div className="settings-actions"><button className="button secondary" type="button" onClick={() => setResetId(null)}>取消</button><button className="button primary" type="submit" disabled={resetBusy || !resetPassword}><Check size={15}/>{resetBusy ? '保存中' : '确认重置'}</button></div></form></div>}
  </section>;
}
