import React, {useState} from 'react';
import {
  ArrowRight,
  Check,
  KeyRound,
  LogIn,
  ShieldCheck,
  UserPlus,
  Users,
  Wrench,
} from 'lucide-react';

import {AUTH_MODES, normalizeMode} from './authModel.js';

function AuthFrame({eyebrow, title, description, children, footer}) {
  return <main className="auth-frame">
    <div className="auth-brand"><span className="auth-brand-mark"><Wrench size={19}/></span><span><strong>链动监控台</strong><small>LOCAL WATCH</small></span></div>
    <section className="auth-panel">
      <div className="auth-panel-head"><span className="auth-kicker">{eyebrow}</span><h1>{title}</h1>{description && <p>{description}</p>}</div>
      {children}
    </section>
    {footer && <p className="auth-footer">{footer}</p>}
  </main>;
}

function FormError({message}) {
  return message ? <div className="auth-error" role="alert">{message}</div> : null;
}

export function LoginView({request, mode = AUTH_MODES.EXTERNAL, allowRegistration = false, onAuthenticated, onGuest, onBack}) {
  const [registering, setRegistering] = useState(false);
  const [form, setForm] = useState({username: '', password: '', display_name: ''});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const submit = async event => {
    event.preventDefault();
    setError('');
    if (!form.username.trim() || !form.password) {
      setError('请输入账号和密码');
      return;
    }
    setBusy(true);
    try {
      const path = registering ? '/auth/register' : '/auth/login';
      const result = await request(path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          username: form.username.trim(),
          password: form.password,
          ...(registering && form.display_name.trim() ? {display_name: form.display_name.trim()} : {}),
        }),
      }, {auth: false});
      if (result?.token || result?.authenticated || result?.user) {
        onAuthenticated(result);
      } else if (registering) {
        setRegistering(false);
        setForm(current => ({...current, password: ''}));
        setError('注册完成，请登录');
      } else {
        setError('登录响应无效');
      }
    } catch (requestError) {
      setError(requestError.message || '请求失败');
    } finally {
      setBusy(false);
    }
  };

  const selfUse = normalizeMode(mode) === AUTH_MODES.SELF_USE;
  return <AuthFrame
    eyebrow={registering ? 'CREATE ACCOUNT' : 'SECURE LOGIN'}
    title={registering ? '创建用户' : '登录系统'}
    description={selfUse ? '本机模式可直接进入，也可以登录管理后台。' : '请输入账号凭据继续。'}
    footer="本地凭据仅通过当前服务端验证"
  >
    <form className="auth-form" onSubmit={submit}>
      <label><span>账号</span><div className="auth-input"><Users size={16}/><input value={form.username} onChange={event => setForm({...form, username: event.target.value})} autoComplete="username" placeholder="输入账号" autoFocus/></div></label>
      {registering && <label><span>显示名称 <small>可选</small></span><input value={form.display_name} onChange={event => setForm({...form, display_name: event.target.value})} autoComplete="nickname" placeholder="输入显示名称"/></label>}
      <label><span>密码</span><div className="auth-input"><KeyRound size={16}/><input type="password" value={form.password} onChange={event => setForm({...form, password: event.target.value})} autoComplete={registering ? 'new-password' : 'current-password'} placeholder="输入密码"/></div></label>
      <FormError message={error}/>
      <button className="button auth-submit" type="submit" disabled={busy}><LogIn size={16}/>{busy ? '处理中' : registering ? '创建并登录' : '登录'}</button>
    </form>
    <div className="auth-actions">
      {allowRegistration && !registering && <button className="button secondary" type="button" onClick={() => { setError(''); setRegistering(true); }}><UserPlus size={15}/>注册用户</button>}
      {registering && <button className="button secondary" type="button" onClick={() => { setError(''); setRegistering(false); }}><ArrowRight size={15}/>返回登录</button>}
      {selfUse && onGuest && <button className="button ghost" type="button" onClick={onGuest}>继续本机模式</button>}
      {onBack && <button className="button ghost" type="button" onClick={onBack}>返回</button>}
    </div>
  </AuthFrame>;
}

export function InstallWizard({request, onComplete}) {
  const [mode, setMode] = useState(AUTH_MODES.SELF_USE);
  const [form, setForm] = useState({username: '', password: '', display_name: '', allow_registration: false});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const submit = async event => {
    event.preventDefault();
    setError('');
    if (!form.username.trim() || !form.password) {
      setError('请先设置管理员账号和密码');
      return;
    }
    setBusy(true);
    try {
      const result = await request('/install/setup', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          mode,
          username: form.username.trim(),
          password: form.password,
          display_name: form.display_name.trim() || undefined,
          allow_registration: mode === AUTH_MODES.EXTERNAL ? form.allow_registration : false,
        }),
      }, {auth: false});
      onComplete({
        ...result,
        mode: normalizeMode(result?.mode || mode),
        allow_registration: mode === AUTH_MODES.EXTERNAL ? form.allow_registration : false,
      });
    } catch (requestError) {
      setError(requestError.message || '安装配置失败');
    } finally {
      setBusy(false);
    }
  };

  return <AuthFrame
    eyebrow="FIRST RUN SETUP"
    title="安装引导"
    description="选择运行模式并创建首个管理员账号。"
    footer="安装完成后可在系统设置中调整公开注册和站点信息"
  >
    <div className="setup-mode-grid" role="radiogroup" aria-label="运行模式">
      <button type="button" className={`setup-mode-card ${mode === AUTH_MODES.SELF_USE ? 'active' : ''}`} role="radio" aria-checked={mode === AUTH_MODES.SELF_USE} onClick={() => setMode(AUTH_MODES.SELF_USE)}>
        <span className="setup-mode-icon"><KeyRound size={18}/></span><strong>自用模式</strong><small>本机使用，保留现有监控流程</small>{mode === AUTH_MODES.SELF_USE && <Check size={16} className="setup-mode-check"/>}
      </button>
      <button type="button" className={`setup-mode-card ${mode === AUTH_MODES.EXTERNAL ? 'active' : ''}`} role="radio" aria-checked={mode === AUTH_MODES.EXTERNAL} onClick={() => setMode(AUTH_MODES.EXTERNAL)}>
        <span className="setup-mode-icon"><Users size={18}/></span><strong>对外模式</strong><small>多用户登录，按角色限制管理功能</small>{mode === AUTH_MODES.EXTERNAL && <Check size={16} className="setup-mode-check"/>}
      </button>
    </div>
    <form className="auth-form setup-form" onSubmit={submit}>
      <div className="setup-form-grid">
        <label><span>管理员账号</span><input value={form.username} onChange={event => setForm({...form, username: event.target.value})} autoComplete="username" placeholder="例如 admin"/></label>
        <label><span>管理员密码</span><input type="password" value={form.password} onChange={event => setForm({...form, password: event.target.value})} autoComplete="new-password" placeholder="设置登录密码"/></label>
      </div>
      <label><span>显示名称 <small>可选</small></span><input value={form.display_name} onChange={event => setForm({...form, display_name: event.target.value})} autoComplete="nickname" placeholder="例如 系统管理员"/></label>
      {mode === AUTH_MODES.EXTERNAL && <label className="auth-check-row"><input type="checkbox" checked={form.allow_registration} onChange={event => setForm({...form, allow_registration: event.target.checked})}/><span><strong>允许公开注册</strong><small>关闭后仅管理员可创建用户</small></span></label>}
      <FormError message={error}/>
      <button className="button auth-submit" type="submit" disabled={busy}><ShieldCheck size={16}/>{busy ? '正在初始化' : '完成安装并进入系统'}</button>
    </form>
  </AuthFrame>;
}
