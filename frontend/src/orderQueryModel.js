export const ORDER_STATUS_OPTIONS = Object.freeze([
  {value: 999, label: '全部'},
  {value: 0, label: '待付款'},
  {value: 1, label: '已付款'},
  {value: 2, label: '已关闭'},
  {value: 3, label: '已退款'},
]);

export const COMPLAINT_REASON_OPTIONS = Object.freeze([
  '不会使用',
  '无效商品',
  '涉嫌色情',
  '涉嫌赌博',
  '欺诈骗钱',
  '没人售后',
  '描述不符',
]);

export const COMPLAINT_IMAGE_TYPES = Object.freeze(['image/png', 'image/jpeg', 'image/webp']);
export const COMPLAINT_MAX_IMAGE_BYTES = 5 * 1024 * 1024;

const COMPLAINT_STATUS_META = Object.freeze({
  '-1': {label: '售后已撤销', tone: 'muted'},
  0: {label: '售后待处理', tone: 'pending'},
  1: {label: '售后已完成', tone: 'success'},
});

const ORDER_STATUS_META = {
  0: {label: '待付款', tone: 'pending'},
  1: {label: '已付款', tone: 'success'},
  2: {label: '已关闭', tone: 'muted'},
  3: {label: '已退款', tone: 'refunded'},
};

const GOODS_TYPE_LABELS = {
  card: '卡密商品',
  article: '文章商品',
  resource: '资源商品',
  equity: '权益商品',
};

const GOODS_ACTION_LABELS = {
  card: '获取卡密',
  article: '查看文章',
  resource: '获取资源',
  equity: '查看权益',
};

function finiteNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function complaintStatusKey(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isInteger(number) ? number : 'unknown';
}

function normalizedText(value) {
  return String(value ?? '').trim();
}

function isHttpUrl(value) {
  if (value.length > 1000 || /\s/.test(value)) return false;
  try {
    const url = new URL(value);
    return (url.protocol === 'http:' || url.protocol === 'https:')
      && Boolean(url.hostname)
      && !url.username
      && !url.password;
  } catch {
    return false;
  }
}

export function complaintStatusMeta(value) {
  const key = complaintStatusKey(value);
  if (key === null) return {key: null, label: '未申请售后', tone: 'neutral'};
  return {key, ...(COMPLAINT_STATUS_META[key] || {label: '售后状态未知', tone: 'neutral'})};
}

export function complaintActionForOrder(order = {}) {
  const status = complaintStatusMeta(order.complaint_status);
  const canApply = Boolean(order.trade_no && order.can_complaint);
  if (status.key === null && canApply) return {kind: 'apply', label: '申请售后', status};
  if (status.key === -1 && canApply) return {kind: 'apply', label: '重新申请', status};
  if (status.key === null) return {kind: 'none', label: '', status};
  return {kind: 'status', label: status.label, status};
}

export function createComplaintDraft(order = {}, contact = '') {
  return {
    trade_no: normalizedText(order.trade_no),
    reason: '',
    content: '',
    contact: normalizedText(contact),
    // Keep three empty slots in the model for callers that still consume the
    // legacy draft shape; the upload UI stores completed URLs in these slots.
    images: ['', '', ''],
    collect_image: '',
    query_pwd: '',
    email_code: '',
  };
}

export function normalizeComplaintPayload(raw = {}) {
  const images = Array.isArray(raw.images)
    ? raw.images.map(normalizedText).filter(Boolean)
    : [];
  return {
    trade_no: normalizedText(raw.trade_no),
    reason: normalizedText(raw.reason),
    content: normalizedText(raw.content),
    contact: normalizedText(raw.contact),
    images,
    collect_image: normalizedText(raw.collect_image),
    query_pwd: normalizedText(raw.query_pwd),
    // The platform accepts an empty email_code when email verification is disabled.
    email_code: '',
  };
}

export function validateComplaintPayload(raw = {}) {
  const payload = normalizeComplaintPayload(raw);
  const errors = {};
  if (!payload.trade_no) errors.trade_no = '订单号不能为空';
  else if (!/^[A-Za-z0-9_-]{1,160}$/.test(payload.trade_no)) errors.trade_no = '订单号格式无效';
  if (!COMPLAINT_REASON_OPTIONS.includes(payload.reason)) errors.reason = '请选择投诉类型';
  if (!payload.content) errors.content = '请输入补充说明';
  else if (payload.content.length > 200) errors.content = '补充说明不能超过 200 字';
  if (!payload.contact) errors.contact = '请输入通知邮箱';
  else if (payload.contact.length > 254 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(payload.contact)) errors.contact = '请输入正确的邮箱地址';
  if (!/^\d{6}$/.test(payload.query_pwd)) errors.query_pwd = '查询密码必须为 6 位数字';
  if (payload.images.length > 3) errors.images = '图片凭证最多上传 3 张';
  else if (payload.images.some(value => !isHttpUrl(value))) errors.images = '图片凭证尚未上传完成';
  if (payload.collect_image && !isHttpUrl(payload.collect_image)) errors.collect_image = '退款二维码尚未上传完成';
  return {valid: Object.keys(errors).length === 0, errors, payload};
}

export function complaintImageFileError(file) {
  if (!file || !COMPLAINT_IMAGE_TYPES.includes(String(file.type || '').toLowerCase())) {
    return '仅支持 PNG、JPEG 或 WebP 图片';
  }
  const size = Number(file.size || 0);
  if (!Number.isFinite(size) || size <= 0) return '图片文件为空';
  if (size > COMPLAINT_MAX_IMAGE_BYTES) return '单张图片不能超过 5 MB';
  return '';
}

export function selectComplaintImageFiles(files, {currentCount = 0, limit = 3} = {}) {
  const accepted = [];
  const errors = [];
  for (const file of Array.from(files || [])) {
    if (currentCount + accepted.length >= limit) {
      errors.push(`最多上传 ${limit} 张图片`);
      break;
    }
    const error = complaintImageFileError(file);
    if (error) errors.push(`${String(file?.name || '图片')}：${error}`);
    else accepted.push(file);
  }
  return {accepted, errors};
}

export function orderStatusMeta(value, upstreamLabel = '') {
  const key = finiteNumber(value, -1);
  const meta = ORDER_STATUS_META[key] || {label: '状态未知', tone: 'neutral'};
  return {...meta, key, label: String(upstreamLabel || meta.label)};
}

export function orderGoodsTypeLabel(value) {
  return GOODS_TYPE_LABELS[String(value || '').toLowerCase()] || '其他商品';
}

export function orderGoodsActionLabel(value) {
  return GOODS_ACTION_LABELS[String(value || '').toLowerCase()] || '查看订单';
}

export function canOpenProtectedOrderDetail(order = {}) {
  const passwordRequired = order.need_query_password === true || Number(order.need_query_password) === 1;
  return Number(order.status) === 1 && passwordRequired && Boolean(String(order.trade_no || '').trim());
}

export function formatOrderMoney(value) {
  if (value === null || value === undefined || value === '') return '--';
  const number = Number(value);
  if (!Number.isFinite(number)) return '--';
  return `¥${number.toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
}

export function formatOrderDateTime(value) {
  if (value === null || value === undefined || value === '') return '时间未知';
  let candidate = value;
  if (typeof value === 'number' || /^\d+$/.test(String(value))) {
    const timestamp = Number(value);
    candidate = timestamp < 10_000_000_000 ? timestamp * 1000 : timestamp;
  }
  const date = new Date(candidate);
  if (Number.isNaN(date.getTime())) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
}

export function safeOfficialOrderUrl(value, fallbackPath = '/order') {
  try {
    const url = new URL(String(value || fallbackPath), 'https://pay.ldxp.cn');
    if (url.protocol !== 'https:' || url.hostname !== 'pay.ldxp.cn') return '';
    return url.href;
  } catch {
    return '';
  }
}

export function safeOrderContentUrl(value) {
  try {
    const url = new URL(String(value || ''));
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return '';
    return url.href;
  } catch {
    return '';
  }
}

function plainText(value, fallback = '') {
  if (typeof value === 'string') return value.trim();
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return fallback;
}

function optionalNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function normalizeDetailLinks(value) {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry, index) => {
    if (!entry || typeof entry !== 'object') return [];
    const url = safeOrderContentUrl(entry.url);
    if (!url) return [];
    return [{label: plainText(entry.label, `链接 ${index + 1}`), url}];
  });
}

export function orderDeliveryKindLabel(value) {
  const labels = {
    card: '卡密信息',
    article: '文章内容',
    resource: '资源信息',
    equity: '权益信息',
  };
  return labels[String(value || '').toLowerCase()] || '交付内容';
}

export function formatOrderCardsForCopy(cards = []) {
  if (!Array.isArray(cards)) return '';
  return cards.filter(value => typeof value === 'string' && value.trim()).join('\n');
}

export function orderDetailErrorState(error = {}) {
  const sessionExpired = Number(error?.status) === 410;
  const message = plainText(error?.message);
  return {
    sessionExpired,
    message: sessionExpired
      ? '订单查询会话已失效，请重新查询订单后再验证'
      : message || '订单详情读取失败，请重试',
  };
}

export function normalizeOrderDetail(payload = {}, fallback = {}) {
  const source = payload?.detail && typeof payload.detail === 'object' ? payload.detail : payload;
  const safeSource = source && typeof source === 'object' ? source : {};
  const safeFallback = fallback && typeof fallback === 'object' ? fallback : {};
  const status = orderStatusMeta(
    safeSource.status ?? safeFallback.status,
    safeSource.status_label || safeFallback.status_label,
  );
  const goodsType = plainText(safeSource.goods_type || safeFallback.goods_type).toLowerCase();
  const sellerSource = safeSource.seller && typeof safeSource.seller === 'object' ? safeSource.seller : {};
  const instructionSource = safeSource.instructions && typeof safeSource.instructions === 'object'
    ? safeSource.instructions
    : {};
  const legacyEquity = safeSource.equity && typeof safeSource.equity === 'object' ? safeSource.equity : {};
  const deliverySource = safeSource.delivery && typeof safeSource.delivery === 'object'
    ? safeSource.delivery
    : {
        kind: goodsType,
        cards: safeSource.cards,
        api_status: legacyEquity.status,
        message: legacyEquity.message,
        content: legacyEquity.content,
      };
  const cards = Array.isArray(deliverySource.cards)
    ? deliverySource.cards.filter(value => typeof value === 'string' && value.trim())
    : [];
  const sendout = optionalNumber(safeSource.sendout);

  return {
    trade_no: plainText(safeSource.trade_no || safeFallback.trade_no),
    goods_name: plainText(safeSource.goods_name || safeFallback.goods_name, '未命名商品'),
    goods_type: goodsType,
    goods_type_label: orderGoodsTypeLabel(goodsType),
    status: status.key,
    status_label: status.label,
    status_tone: status.tone,
    total_amount: safeSource.total_amount ?? safeFallback.total_amount ?? null,
    quantity: Math.max(0, finiteNumber(safeSource.quantity ?? safeFallback.quantity, 0)),
    created_at: safeSource.created_at ?? safeSource.create_time ?? safeFallback.created_at ?? null,
    success_at: safeSource.success_at ?? safeSource.paid_at ?? safeSource.success_time ?? null,
    sendout: sendout === null ? null : Math.max(0, Math.trunc(sendout)),
    contact: plainText(safeSource.contact),
    can_complaint: safeSource.can_complaint === true || Number(safeSource.can_complaint) === 1,
    seller: {
      nickname: plainText(sellerSource.nickname),
      avatar: safeOrderContentUrl(sellerSource.avatar),
      shop_url: safeOrderContentUrl(sellerSource.shop_url),
      contact_qq: plainText(sellerSource.contact_qq ?? sellerSource.qq),
      contact_mobile: plainText(sellerSource.contact_mobile ?? sellerSource.mobile),
      contact_wechat: plainText(sellerSource.contact_wechat ?? sellerSource.wechat),
    },
    instructions: {
      text: plainText(instructionSource.text, plainText(safeSource.instructions)),
      links: normalizeDetailLinks(instructionSource.links),
    },
    delivery: {
      kind: plainText(deliverySource.kind, goodsType).toLowerCase(),
      cards,
      api_status: optionalNumber(deliverySource.api_status),
      message: plainText(deliverySource.message),
      content: plainText(deliverySource.content),
      links: normalizeDetailLinks(deliverySource.links),
      truncated: deliverySource.truncated === true || Number(deliverySource.truncated) === 1,
    },
  };
}

export function normalizeOrder(raw = {}) {
  const status = orderStatusMeta(raw.status, raw.status_label);
  const goodsType = String(raw.goods_type || raw.goods?.goods_type || '').toLowerCase();
  const tradeNo = String(raw.trade_no || '').trim();
  const goodsKey = String(raw.goods_key || raw.goods?.goods_key || '').trim();
  return {
    ...raw,
    trade_no: tradeNo,
    goods_name: String(raw.goods_name || '未命名商品'),
    goods_key: goodsKey,
    goods_type: goodsType,
    goods_type_label: orderGoodsTypeLabel(goodsType),
    goods_action_label: orderGoodsActionLabel(goodsType),
    goods_image: String(raw.goods_image || raw.goods?.image || ''),
    created_at: raw.created_at ?? raw.create_time ?? null,
    total_amount: raw.total_amount,
    quantity: Math.max(0, finiteNumber(raw.quantity, 0)),
    status: status.key,
    status_label: status.label,
    status_tone: status.tone,
    need_query_password: Boolean(Number(raw.need_query_password || 0)),
    can_complaint: Boolean(Number(raw.can_complaint || 0)),
    complaint_status: complaintStatusMeta(raw.complaint_status ?? raw.complaint?.status ?? null).key,
    detail_url: safeOfficialOrderUrl(raw.detail_url, tradeNo ? `/order/info/${encodeURIComponent(tradeNo)}` : '/order'),
    result_url: safeOfficialOrderUrl(raw.result_url, tradeNo ? `/order/result/${encodeURIComponent(tradeNo)}` : '/order'),
    goods_url: safeOfficialOrderUrl(raw.goods_url, goodsKey ? `/item/${encodeURIComponent(goodsKey)}` : '/order'),
  };
}

export function normalizeOrderResponse(payload = {}, fallback = {}) {
  const orders = Array.isArray(payload.orders) ? payload.orders.map(normalizeOrder) : [];
  const source = payload.pagination && typeof payload.pagination === 'object' ? payload.pagination : {};
  const page = Math.max(1, finiteNumber(source.page, fallback.page || 1));
  const pageSize = Math.max(1, finiteNumber(source.page_size, fallback.pageSize || 10));
  const total = Math.max(0, finiteNumber(source.total, orders.length));
  const pages = Math.max(1, finiteNumber(source.pages, Math.ceil(total / pageSize) || 1));
  return {
    orders,
    pagination: {page, page_size: pageSize, total, pages},
  };
}

export function summarizeOrders(orders = [], total = orders.length) {
  return {
    total: Math.max(0, finiteNumber(total, 0)),
    pageCount: orders.length,
    pageAmount: orders.reduce((sum, order) => sum + finiteNumber(order.total_amount, 0), 0),
  };
}

export function orderQueryContextChanged(current = {}, next = {}) {
  return String(current.keywords || '') !== String(next.keywords || '')
    || Number(current.status ?? 999) !== Number(next.status ?? 999)
    || Number(current.pageSize ?? 10) !== Number(next.pageSize ?? 10);
}

export function verificationLabel(verification = {}) {
  if (verification?.status === 'manual_required') return '等待手工确认';
  if (verification?.status !== 'verified') return '等待查询';
  if (verification?.mode === 'manual') return '手工验证通过';
  if (verification?.mode === 'session') return '会话验证有效';
  return '自动识别通过';
}
