import assert from 'node:assert/strict';
import test from 'node:test';

import {
  AUTH_MODES,
  canManageWorkspace,
  canAccessView,
  defaultViewFor,
  normalizeInstallStatus,
  normalizeSession,
  normalizeUser,
  requiresLogin,
  roleLabel,
} from './authModel.js';

test('normalizes administrator and ordinary users', () => {
  const admin = normalizeUser({id: 7, username: 'root', role: 'admin'});
  const user = normalizeUser({user_id: 8, name: 'reader', is_admin: false});

  assert.equal(admin.is_admin, true);
  assert.equal(admin.role, 'admin');
  assert.equal(roleLabel(admin), '管理员');
  assert.equal(user.id, 8);
  assert.equal(user.role, 'user');
  assert.equal(roleLabel(user), '普通用户');
});

test('normalizes install mode and setup state', () => {
  assert.deepEqual(normalizeInstallStatus({configured: false, mode: 'external', allow_registration: 1}), {
    configured: false,
    needs_setup: true,
    mode: AUTH_MODES.EXTERNAL,
    allow_registration: true,
    force_login: true,
    auth_required: true,
    allow_user_reclaim: false,
    allow_user_sub2api_import: false,
  });
  assert.equal(normalizeInstallStatus({needs_setup: false, mode: 'local'}).mode, AUTH_MODES.SELF_USE);
});

test('requires login only for external mode', () => {
  assert.equal(requiresLogin(AUTH_MODES.EXTERNAL), true);
  assert.equal(requiresLogin(AUTH_MODES.SELF_USE), false);
  assert.equal(requiresLogin({mode: AUTH_MODES.EXTERNAL, force_login: false}), false);
  assert.equal(requiresLogin({mode: AUTH_MODES.EXTERNAL, auth_required: true}), true);
});

test('gates admin views while retaining public views for ordinary users', () => {
  const admin = normalizeUser({username: 'root', role: 'admin'});
  const user = normalizeUser({username: 'reader', role: 'user'});

  assert.equal(canAccessView('sub2api', admin, AUTH_MODES.EXTERNAL), true);
  assert.equal(canAccessView('sub2api', user, AUTH_MODES.EXTERNAL), false);
  assert.equal(canAccessView('reclaim', null, AUTH_MODES.SELF_USE), true);
  assert.equal(canAccessView('sub2api', null, AUTH_MODES.SELF_USE), true);
  assert.equal(canAccessView('reclaim', user, AUTH_MODES.EXTERNAL, {allow_user_reclaim: true}), true);
  assert.equal(canAccessView('sub2api', user, AUTH_MODES.EXTERNAL, {allow_user_sub2api_import: true}), true);
  assert.equal(canAccessView('orders', user, AUTH_MODES.EXTERNAL), true);
  assert.equal(canAccessView('settings', user, AUTH_MODES.EXTERNAL), true);
  assert.equal(canAccessView('settings', null, AUTH_MODES.EXTERNAL), false);
  assert.equal(canAccessView('settings', null, AUTH_MODES.SELF_USE), true);
  assert.equal(defaultViewFor(user, AUTH_MODES.EXTERNAL), 'products');
});

test('matches monitor write access with the backend role boundary', () => {
  const admin = normalizeUser({username: 'root', role: 'admin'});
  const user = normalizeUser({username: 'reader', role: 'user'});

  assert.equal(canManageWorkspace(null, AUTH_MODES.SELF_USE), true);
  assert.equal(canManageWorkspace(admin, AUTH_MODES.SELF_USE), true);
  assert.equal(canManageWorkspace(user, AUTH_MODES.SELF_USE), false);
  assert.equal(canManageWorkspace(user, AUTH_MODES.EXTERNAL), false);
});

test('normalizes authenticated session only when a user is present', () => {
  const session = normalizeSession({authenticated: true, token: 'abc', mode: 'external', user: {username: 'root', is_admin: true}});
  assert.equal(session.authenticated, true);
  assert.equal(session.token, 'abc');
  assert.equal(session.user.role, 'admin');
  assert.equal(normalizeSession({authenticated: true}).authenticated, false);
});
