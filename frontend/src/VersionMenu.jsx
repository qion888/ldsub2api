import React, {useCallback, useEffect, useRef, useState} from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  CloudDownload,
  ExternalLink,
  GitBranch,
  PackageCheck,
  RefreshCw,
  RotateCcw,
  X,
} from 'lucide-react';

import {isAdmin} from './authModel.js';
import {normalizeVersionInfo, versionBlockReason, versionStatusLabel} from './versionModel.js';

export const GITHUB_REPOSITORY_URL = 'https://github.com/qion888/ldsub2api';
const FALLBACK_VERSION = '2.1.2';

function displayVersion(value) {
  const version = String(value || FALLBACK_VERSION).trim().replace(/^v/i, '');
  return version || FALLBACK_VERSION;
}

function statusIcon(info) {
  if (info?.needs_restart || info?.update_available) return <AlertTriangle size={14}/>;
  if (info?.status === 'up_to_date') return <CheckCircle2 size={14}/>;
  return <GitBranch size={14}/>;
}

export default function VersionMenu({request, user, onOpenSettings, notify}) {
  const admin = isAdmin(user);
  const [open, setOpen] = useState(false);
  const [info, setInfo] = useState(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const menuRef = useRef(null);

  const loadVersion = useCallback(async ({check = false, announce = false} = {}) => {
    if (!admin) return null;
    setBusy(check ? 'check' : 'load');
    setError('');
    try {
      const result = normalizeVersionInfo(await request(check ? '/version/check' : '/version', check ? {method: 'POST'} : {}));
      setInfo(result);
      if (announce) notify?.(result.message || (result.update_available ? '发现可用更新' : '当前已是最新版本'));
      return result;
    } catch (requestError) {
      setError(requestError.message || '版本信息加载失败');
      return null;
    } finally {
      setBusy('');
    }
  }, [admin, notify, request]);

  useEffect(() => {
    if (!admin) {
      setInfo(null);
      setError('');
      return undefined;
    }
    let cancelled = false;
    request('/version')
      .then(result => { if (!cancelled) setInfo(normalizeVersionInfo(result)); })
      .catch(() => { /* The menu remains usable with the local fallback version. */ });
    return () => { cancelled = true; };
  }, [admin, request]);

  useEffect(() => {
    if (!open) return undefined;
    const closeOnPointerDown = event => {
      if (!menuRef.current?.contains(event.target)) setOpen(false);
    };
    const closeOnKeyDown = event => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', closeOnPointerDown);
    document.addEventListener('keydown', closeOnKeyDown);
    return () => {
      document.removeEventListener('pointerdown', closeOnPointerDown);
      document.removeEventListener('keydown', closeOnKeyDown);
    };
  }, [open]);

  const toggle = () => {
    const nextOpen = !open;
    setOpen(nextOpen);
    if (nextOpen && admin) void loadVersion({check: true});
  };

  const installUpdate = async () => {
    if (!admin) return;
    let current = info;
    if (!current?.update_ready) current = await loadVersion({check: true});
    if (!current?.update_available || !current.update_ready) {
      if (current?.message) notify?.(current.message);
      return;
    }
    if (!window.confirm(`将从 GitHub 更新到 ${displayVersion(current.latest_version || current.latest_short_commit)}，是否继续？`)) return;
    setBusy('update');
    setError('');
    try {
      const result = normalizeVersionInfo(await request('/version/update', {method: 'POST'}));
      setInfo(previous => normalizeVersionInfo({...previous, ...result}));
      notify?.(result.message || '版本更新完成');
    } catch (requestError) {
      setError(requestError.message || '版本更新失败');
    } finally {
      setBusy('');
    }
  };

  const rollbackUpdate = async () => {
    const backupId = info?.last_update?.backup_id;
    if (!admin || !backupId || !info?.last_update?.can_rollback) return;
    if (!window.confirm('将恢复升级前的代码，保留现有数据并在完成后重启服务，继续吗？')) return;
    setBusy('rollback');
    setError('');
    try {
      const result = normalizeVersionInfo(await request('/version/rollback', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({backup_id: backupId, restore_data: false}),
      }));
      setInfo(current => normalizeVersionInfo({...current, ...result, status: 'updated', needs_restart: true, last_update: null}));
      notify?.(result.message || '版本已回退，请重启服务');
    } catch (requestError) {
      setError(requestError.message || '版本回退失败');
    } finally {
      setBusy('');
    }
  };

  const currentVersion = displayVersion(info?.current_version);
  const latestVersion = info?.latest_version ? displayVersion(info.latest_version) : '';
  const status = info ? versionStatusLabel(info) : admin ? (busy ? '正在检查' : '点击检查更新') : '本地版本';
  const reason = info ? (info.needs_restart ? '新版本已安装，请重启后端与前端服务。' : versionBlockReason(info)) : '';
  const releaseUrl = info?.release?.url || info?.repository_url || GITHUB_REPOSITORY_URL;
  const statusClass = info?.status || 'ready';

  return <div className="brand-version-wrap" ref={menuRef}>
    <button className="brand-version" type="button" aria-expanded={open} aria-haspopup="dialog" onClick={toggle}>
      <span>v{currentVersion}</span><ChevronDown size={12} className={open ? 'open' : ''}/>
    </button>
    {open && <section className="version-menu-popover" role="dialog" aria-label="版本信息">
      <div className="version-menu-head"><span>当前版本</span><div><button className="version-menu-icon" type="button" aria-label="检查更新" title="检查更新" onClick={() => loadVersion({check: true, announce: true})} disabled={Boolean(busy)}><RefreshCw size={15} className={busy === 'check' || busy === 'load' ? 'spin' : ''}/></button><button className="version-menu-icon" type="button" aria-label="关闭版本信息" title="关闭" onClick={() => setOpen(false)}><X size={15}/></button></div></div>
      <div className="version-menu-current"><strong>v{currentVersion}</strong><span className={`version-menu-status ${statusClass}`}>{statusIcon(info)}{status}</span></div>
      {latestVersion && latestVersion !== currentVersion && <div className="version-menu-latest">GitHub 最新 <strong>v{latestVersion}</strong></div>}
      {reason && <p className="version-menu-notice warning"><AlertTriangle size={14}/><span>{reason}</span></p>}
      {error && <p className="version-menu-notice error"><AlertTriangle size={14}/><span>{error}</span></p>}
      {!admin && <p className="version-menu-hint">登录管理员账号后可检查和更新版本</p>}
      <div className="version-menu-actions">
        <a href={releaseUrl} target="_blank" rel="noreferrer"><ExternalLink size={14}/>查看发布</a>
        {admin && <button type="button" onClick={installUpdate} disabled={Boolean(busy) || !info?.update_available}><CloudDownload size={14}/>{busy === 'update' ? '更新中' : '立即更新'}</button>}
        {admin && info?.last_update?.can_rollback && <button type="button" onClick={rollbackUpdate} disabled={Boolean(busy)}><RotateCcw size={14}/>{busy === 'rollback' ? '回退中' : '版本回退'}</button>}
      </div>
      <button className="version-menu-detail" type="button" onClick={() => { setOpen(false); onOpenSettings?.(); }}><PackageCheck size={14}/>查看版本详情与系统设置</button>
    </section>}
  </div>;
}
