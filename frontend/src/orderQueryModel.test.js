import test from 'node:test';
import assert from 'node:assert/strict';
import {
  formatOrderDateTime,
  formatOrderMoney,
  normalizeOrder,
  normalizeOrderResponse,
  orderQueryContextChanged,
  orderGoodsActionLabel,
  orderStatusMeta,
  safeOfficialOrderUrl,
  summarizeOrders,
  verificationLabel,
} from './orderQueryModel.js';

test('normalizes upstream order fields and status labels', () => {
  const order = normalizeOrder({
    trade_no: 'LD-1001',
    goods_name: '测试卡密',
    goods_type: 'card',
    goods_key: 'goods-1',
    create_time: 1_788_000_000,
    total_amount: '19.9',
    quantity: '2',
    status: 1,
    need_query_password: 1,
  });

  assert.equal(order.status_label, '已付款');
  assert.equal(order.status_tone, 'success');
  assert.equal(order.goods_type_label, '卡密商品');
  assert.equal(order.goods_action_label, '获取卡密');
  assert.equal(order.quantity, 2);
  assert.equal(order.need_query_password, true);
  assert.equal(order.detail_url, 'https://pay.ldxp.cn/order/info/LD-1001');
});

test('normalizes pagination and computes current page summary', () => {
  const result = normalizeOrderResponse({
    orders: [
      {trade_no: 'A', total_amount: '9.90', status: 0},
      {trade_no: 'B', total_amount: 10, status: 3},
    ],
    pagination: {page: 2, page_size: 2, total: 7, pages: 4},
  });
  const summary = summarizeOrders(result.orders, result.pagination.total);

  assert.deepEqual(result.pagination, {page: 2, page_size: 2, total: 7, pages: 4});
  assert.deepEqual(summary, {total: 7, pageCount: 2, pageAmount: 19.9});
  assert.equal(formatOrderMoney(summary.pageAmount), '¥19.90');
});

test('handles unknown values and restricts official links', () => {
  assert.equal(orderStatusMeta(9).label, '状态未知');
  assert.equal(orderGoodsActionLabel('other'), '查看订单');
  assert.equal(formatOrderMoney('not-a-number'), '--');
  assert.equal(formatOrderDateTime('not-a-date'), '时间未知');
  assert.equal(safeOfficialOrderUrl('https://example.com/order/1'), '');
  assert.equal(verificationLabel(null), '等待查询');
  assert.equal(verificationLabel({status: 'manual_required'}), '等待手工确认');
  assert.equal(verificationLabel({status: 'verified', mode: 'ocr'}), '自动识别通过');
});

test('detects result context changes that would make preserved rows stale', () => {
  const current = {keywords: 'buyer', status: 999, pageSize: 10};
  assert.equal(orderQueryContextChanged(current, {...current}), false);
  assert.equal(orderQueryContextChanged(current, {...current, status: 3}), true);
  assert.equal(orderQueryContextChanged(current, {...current, pageSize: 20}), true);
  assert.equal(orderQueryContextChanged(current, {...current, keywords: 'other'}), true);
});
