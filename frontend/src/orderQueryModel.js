export const ORDER_STATUS_OPTIONS = Object.freeze([
  {value: 999, label: '全部'},
  {value: 0, label: '待付款'},
  {value: 1, label: '已付款'},
  {value: 2, label: '已关闭'},
  {value: 3, label: '已退款'},
]);

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
    complaint_status: raw.complaint_status ?? raw.complaint?.status ?? null,
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
