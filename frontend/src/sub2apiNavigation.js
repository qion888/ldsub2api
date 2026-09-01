export const SUB2API_SECTIONS = Object.freeze([
  Object.freeze({value: 'cards', label: '卡密核验与推送', shortLabel: '卡密推送', description: '核验卡密、下载账号并完成推送'}),
  Object.freeze({value: 'automation', label: '定时找回与自动导入', shortLabel: '定时自动化', description: '管理 401 找回计划和自动导入策略'}),
  Object.freeze({value: 'accounts', label: '账号列表', shortLabel: '账号列表', description: '查看账号状态、额度和路由归属'}),
]);

export const DEFAULT_SUB2API_SECTION = 'cards';

export function normalizeSub2ApiSection(value) {
  return SUB2API_SECTIONS.some(section => section.value === value) ? value : DEFAULT_SUB2API_SECTION;
}
