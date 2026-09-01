export const AUTH_MODES = Object.freeze({
  SELF_USE: 'self_use',
  EXTERNAL: 'external',
});

export const ROLES = Object.freeze({
  ADMIN: 'admin',
  USER: 'user',
});

export const PUBLIC_VIEWS = Object.freeze(['products', 'monitor', 'history', 'orders']);
export const ADMIN_VIEWS = Object.freeze(['reclaim', 'sub2api']);

export function normalizeMode(value, fallback = AUTH_MODES.SELF_USE) {
  const mode = String(value || '').trim().toLowerCase();
  if (mode === AUTH_MODES.EXTERNAL || mode === 'public' || mode === 'multi_user') return AUTH_MODES.EXTERNAL;
  if (mode === AUTH_MODES.SELF_USE || mode === 'private' || mode === 'local') return AUTH_MODES.SELF_USE;
  return fallback;
}

export function normalizeRole(user) {
  if (user?.is_admin === true || user?.is_admin === 1 || user?.role === ROLES.ADMIN) return ROLES.ADMIN;
  return ROLES.USER;
}

export function normalizeUser(value) {
  const source = value?.user && typeof value.user === 'object' ? value.user : value;
  if (!source || typeof source !== 'object') return null;
  const username = String(source.username || source.name || '').trim();
  if (!username && source.id === undefined && source.user_id === undefined) return null;
  const id = source.id ?? source.user_id ?? null;
  const displayName = String(source.display_name || source.nickname || username || '用户').trim();
  const role = normalizeRole(source);
  return {
    id,
    username: username || `user-${id ?? 'local'}`,
    display_name: displayName,
    role,
    is_admin: role === ROLES.ADMIN,
  };
}

export function normalizeInstallStatus(value) {
  const source = value && typeof value === 'object' ? value : {};
  const mode = normalizeMode(source.mode);
  const configured = source.configured !== undefined
    ? Boolean(source.configured)
    : !Boolean(source.needs_setup);
  const needsSetup = source.needs_setup !== undefined
    ? Boolean(source.needs_setup)
    : !configured;
  const forceLogin = source.force_login !== undefined
    ? Boolean(source.force_login)
    : source.auth_required !== undefined ? Boolean(source.auth_required) : mode === AUTH_MODES.EXTERNAL;
  return {
    configured,
    needs_setup: needsSetup,
    mode,
    allow_registration: Boolean(source.allow_registration),
    force_login: forceLogin,
    auth_required: source.auth_required !== undefined
      ? Boolean(source.auth_required)
      : mode === AUTH_MODES.EXTERNAL && forceLogin,
    allow_user_reclaim: Boolean(source.allow_user_reclaim),
    allow_user_sub2api_import: Boolean(source.allow_user_sub2api_import),
  };
}

export function normalizeSession(value) {
  const source = value && typeof value === 'object' ? value : {};
  const user = normalizeUser(source.user || source);
  return {
    authenticated: Boolean(source.authenticated && user),
    token: String(source.token || '').trim(),
    expires_at: source.expires_at || null,
    user,
    mode: normalizeMode(source.mode),
  };
}

export function requiresLogin(modeOrStatus) {
  if (modeOrStatus && typeof modeOrStatus === 'object') {
    const source = modeOrStatus;
    const mode = normalizeMode(source.mode);
    if (mode !== AUTH_MODES.EXTERNAL) return false;
    if (source.auth_required !== undefined) return Boolean(source.auth_required);
    if (source.force_login !== undefined) return Boolean(source.force_login);
    return true;
  }
  return normalizeMode(modeOrStatus) === AUTH_MODES.EXTERNAL;
}

export function isAdmin(user) {
  return normalizeRole(user) === ROLES.ADMIN;
}

export function canManageWorkspace(user, mode = AUTH_MODES.SELF_USE) {
  // Self-use keeps the legacy anonymous local workflow; signed-in users still
  // follow the role boundary used by the API.
  return isAdmin(user) || (!user && normalizeMode(mode) === AUTH_MODES.SELF_USE);
}

export function canAccessView(view, user, mode = AUTH_MODES.SELF_USE, permissions = {}) {
  const normalizedMode = normalizeMode(mode);
  if (ADMIN_VIEWS.includes(view)) {
    if (isAdmin(user)) return true;
    if (normalizedMode === AUTH_MODES.SELF_USE && !user) return true;
    if (normalizedMode === AUTH_MODES.EXTERNAL && user?.role === ROLES.USER) {
      return view === 'reclaim' ? Boolean(permissions.allow_user_reclaim) : Boolean(permissions.allow_user_sub2api_import);
    }
    return false;
  }
  if (view === 'settings') return Boolean(user) || normalizedMode === AUTH_MODES.SELF_USE;
  if (view === 'users') return isAdmin(user);
  return PUBLIC_VIEWS.includes(view);
}

export function defaultViewFor(user, mode = AUTH_MODES.SELF_USE) {
  if (canAccessView('products', user, mode)) return 'products';
  return 'monitor';
}

export function roleLabel(user) {
  return isAdmin(user) ? '管理员' : '普通用户';
}
