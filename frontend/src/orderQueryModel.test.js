import test from 'node:test';
import assert from 'node:assert/strict';
import {
  canOpenProtectedOrderDetail,
  COMPLAINT_MAX_IMAGE_BYTES,
  COMPLAINT_REASON_OPTIONS,
  complaintActionForOrder,
  complaintHistoryErrorState,
  complaintImageFileError,
  complaintStatusMeta,
  createComplaintDraft,
  formatOrderDateTime,
  formatOrderCardsForCopy,
  formatOrderMoney,
  normalizeOrder,
  normalizeComplaintHistoryResponse,
  normalizeOrderDetail,
  normalizeOrderResponse,
  orderDeliveryKindLabel,
  orderDetailErrorState,
  normalizeComplaintPayload,
  orderQueryContextChanged,
  orderGoodsActionLabel,
  orderStatusMeta,
  safeOrderContentUrl,
  safeOfficialOrderUrl,
  selectComplaintImageFiles,
  summarizeOrders,
  validateComplaintPayload,
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
    can_complaint: '1',
    complaint_status: '-1',
  });

  assert.equal(order.status_label, '已付款');
  assert.equal(order.status_tone, 'success');
  assert.equal(order.goods_type_label, '卡密商品');
  assert.equal(order.goods_action_label, '获取卡密');
  assert.equal(order.quantity, 2);
  assert.equal(order.need_query_password, true);
  assert.equal(order.can_complaint, true);
  assert.equal(order.complaint_status, -1);
  assert.equal(order.detail_url, 'https://pay.ldxp.cn/order/info/LD-1001');
  assert.equal(canOpenProtectedOrderDetail(order), true);
  assert.equal(canOpenProtectedOrderDetail({...order, status: 0}), false);
  assert.equal(canOpenProtectedOrderDetail({...order, need_query_password: false}), false);
});

test('distinguishes complaint actions and statuses conservatively', () => {
  const eligible = {trade_no: 'LD-1001', can_complaint: true};
  assert.deepEqual(complaintActionForOrder({...eligible, complaint_status: null}), {
    kind: 'apply',
    label: '申请售后',
    status: {key: null, label: '未申请售后', tone: 'neutral'},
  });
  assert.deepEqual(complaintActionForOrder({...eligible, complaint_status: -1}), {
    kind: 'history',
    label: '查看记录',
    status: {key: -1, label: '售后已撤销', tone: 'muted'},
    canReapply: true,
    reapplyLabel: '重新申请',
  });
  assert.deepEqual(complaintActionForOrder({trade_no: 'LD-1001', can_complaint: false, complaint_status: -1}), {
    kind: 'history',
    label: '查看记录',
    status: {key: -1, label: '售后已撤销', tone: 'muted'},
    canReapply: false,
    reapplyLabel: '重新申请',
  });
  assert.deepEqual(complaintActionForOrder({...eligible, complaint_status: 0}), {
    kind: 'history',
    label: '查看记录',
    status: {key: 0, label: '售后待处理', tone: 'pending'},
  });
  assert.deepEqual(complaintActionForOrder({...eligible, complaint_status: 1}), {
    kind: 'history',
    label: '售后完成',
    status: {key: 1, label: '售后已完成', tone: 'success'},
  });
  assert.equal(complaintActionForOrder({...eligible, complaint_status: 7}).kind, 'status');
  assert.equal(complaintActionForOrder({...eligible, complaint_status: 'unexpected'}).kind, 'status');
  assert.equal(complaintStatusMeta(7).label, '售后状态未知');
  assert.equal(complaintActionForOrder({trade_no: 'LD-1001', can_complaint: false, complaint_status: null}).kind, 'none');
});

test('normalizes complaint history messages and image fields conservatively', () => {
  const result = normalizeComplaintHistoryResponse({
    session_id: ' session-1 ',
    expires_in: '88.9',
    trade_no: ' LD-1001 ',
    need_query_password: 1,
    complaint: {
      status: '0',
      status_label: ' 待平台处理 ',
      reason: ' 描述不符 ',
      content: ' 商品内容不一致 ',
      images: ['https://cdn.example.test/a.png', 'javascript:alert(1)', null],
      contact: ' buyer@example.test ',
      created_at: '1710000000',
      collect_image: 'https://cdn.example.test/qr.png',
      can_complaint: '1',
      messages: [
        {identity: 'buyer', identity_label: '', content_type: 0, content: ' 买家补充 ', created_at: '1710000001'},
        {identity: 'platform', identity_label: ' 平台客服 ', content_type: 1, content: 'https://cdn.example.test/reply.png', created_at: '1710000002'},
        {identity: 'user', content_type: 'image', content: 'file:///tmp/leak.png'},
        {identity: '<unsafe>', content_type: 'text', content: ' 协商回复 '},
      ],
      internal_note: 'must-not-leak',
    },
    internal: 'must-not-leak',
  });

  assert.deepEqual(result, {
    session_id: 'session-1',
    expires_in: 88,
    trade_no: 'LD-1001',
    need_query_password: true,
    complaint: {
      status: 0,
      status_label: '待平台处理',
      status_tone: 'pending',
      reason: '描述不符',
      content: '商品内容不一致',
      images: ['https://cdn.example.test/a.png'],
      contact: 'buyer@example.test',
      created_at: '1710000000',
      collect_image: 'https://cdn.example.test/qr.png',
      messages: [
        {identity: 'buyer', identity_label: '买家', content_type: 'text', content: '买家补充', created_at: '1710000001'},
        {identity: 'platform', identity_label: '平台客服', content_type: 'image', content: 'https://cdn.example.test/reply.png', created_at: '1710000002'},
        {identity: 'unknown', identity_label: '协商方', content_type: 'text', content: '协商回复', created_at: ''},
      ],
      can_complaint: true,
    },
  });
  assert.equal(JSON.stringify(result).includes('must-not-leak'), false);
});

test('normalizes empty complaint history and classifies password and expiry errors', () => {
  assert.deepEqual(normalizeComplaintHistoryResponse({need_query_password: false}, {trade_no: 'LD-FALLBACK'}), {
    session_id: '',
    expires_in: 0,
    trade_no: 'LD-FALLBACK',
    need_query_password: false,
    complaint: null,
  });
  assert.deepEqual(complaintHistoryErrorState({code: 'order_query_password_required'}), {
    sessionExpired: false,
    passwordRequired: true,
    passwordInvalid: false,
    endpointUnavailable: false,
    message: '请输入订单安全密码',
  });
  assert.deepEqual(complaintHistoryErrorState({status: 403, code: 'order_query_password_invalid', message: '密码错误'}), {
    sessionExpired: false,
    passwordRequired: false,
    passwordInvalid: true,
    endpointUnavailable: false,
    message: '密码错误',
  });
  assert.equal(complaintHistoryErrorState({status: 410}).sessionExpired, true);
  assert.deepEqual(complaintHistoryErrorState({status: 404, message: '接口不存在'}), {
    sessionExpired: false,
    passwordRequired: false,
    passwordInvalid: false,
    endpointUnavailable: true,
    message: '售后记录接口未启用，请重启后端服务',
  });
});

test('creates and normalizes the exact complaint submission payload', () => {
  const draft = createComplaintDraft({trade_no: ' LD-1001 '}, ' buyer@example.com ');
  assert.deepEqual(draft.images, ['', '', '']);
  const payload = normalizeComplaintPayload({
    ...draft,
    reason: ' 描述不符 ',
    content: ' 补充说明 ',
    images: [' https://cdn.example.test/one.png ', '', 'https://cdn.example.test/two.png'],
    collect_image: ' https://cdn.example.test/qr.png ',
    query_pwd: ' 123456 ',
    email_code: ' 9988 ',
  });

  assert.deepEqual(payload, {
    trade_no: 'LD-1001',
    reason: '描述不符',
    content: '补充说明',
    contact: 'buyer@example.com',
    images: ['https://cdn.example.test/one.png', 'https://cdn.example.test/two.png'],
    collect_image: 'https://cdn.example.test/qr.png',
    query_pwd: '123456',
    email_code: '',
  });
});

test('validates complaint fields, limits and URL schemes', () => {
  const valid = {
    trade_no: 'LD-1001',
    reason: COMPLAINT_REASON_OPTIONS[0],
    content: 'a'.repeat(200),
    contact: 'buyer@example.com',
    images: ['https://cdn.example.test/1.png', 'http://cdn.example.test/2.png', ''],
    collect_image: 'https://cdn.example.test/qr.png',
    query_pwd: '123456',
    email_code: '',
  };
  assert.deepEqual(validateComplaintPayload(valid), {
    valid: true,
    errors: {},
    payload: {...valid, images: valid.images.slice(0, 2)},
  });

  const missing = validateComplaintPayload({});
  assert.deepEqual(Object.keys(missing.errors).sort(), ['contact', 'content', 'query_pwd', 'reason', 'trade_no']);
  assert.equal(validateComplaintPayload({...valid, reason: '其他原因'}).errors.reason, '请选择投诉类型');
  assert.equal(validateComplaintPayload({...valid, content: 'a'.repeat(201)}).errors.content, '补充说明不能超过 200 字');
  assert.equal(validateComplaintPayload({...valid, contact: 'not-an-email'}).errors.contact, '请输入正确的邮箱地址');
  assert.ok(validateComplaintPayload({...valid, query_pwd: '12345'}).errors.query_pwd);
  assert.ok(validateComplaintPayload({...valid, query_pwd: '1234567'}).errors.query_pwd);
  assert.ok(validateComplaintPayload({...valid, query_pwd: '１２３４５６'}).errors.query_pwd);
  assert.ok(validateComplaintPayload({...valid, query_pwd: 'abc123'}).errors.query_pwd);
  assert.ok(validateComplaintPayload({...valid, images: ['https://a.test/1', 'https://a.test/2', 'https://a.test/3', 'https://a.test/4']}).errors.images);
  assert.ok(validateComplaintPayload({...valid, images: ['javascript:alert(1)']}).errors.images);
  assert.ok(validateComplaintPayload({...valid, images: ['https://user:secret@a.test/image.png']}).errors.images);
  assert.ok(validateComplaintPayload({...valid, images: [`https://a.test/${'x'.repeat(1000)}`]}).errors.images);
  assert.ok(validateComplaintPayload({...valid, collect_image: 'file:///tmp/qr.png'}).errors.collect_image);
});

test('validates complaint image MIME types and size boundaries', () => {
  const validFiles = [
    {name: 'proof.png', type: 'image/png', size: 1},
    {name: 'proof.jpg', type: 'IMAGE/JPEG', size: COMPLAINT_MAX_IMAGE_BYTES},
    {name: 'proof.webp', type: 'image/webp', size: 1024},
  ];
  validFiles.forEach(file => assert.equal(complaintImageFileError(file), ''));

  assert.equal(
    complaintImageFileError({name: 'proof.gif', type: 'image/gif', size: 1}),
    '仅支持 PNG、JPEG 或 WebP 图片',
  );
  assert.equal(
    complaintImageFileError({name: 'empty.png', type: 'image/png', size: 0}),
    '图片文件为空',
  );
  assert.equal(
    complaintImageFileError({name: 'large.png', type: 'image/png', size: COMPLAINT_MAX_IMAGE_BYTES + 1}),
    '单张图片不能超过 5 MB',
  );
  assert.equal(
    complaintImageFileError({name: 'missing-type', size: 10}),
    '仅支持 PNG、JPEG 或 WebP 图片',
  );
});

test('selects complaint image files while retaining valid files and reporting limits', () => {
  const png = {name: 'proof.png', type: 'image/png', size: 1024};
  const jpeg = {name: 'proof.jpg', type: 'image/jpeg', size: 2048};
  const webp = {name: 'proof.webp', type: 'image/webp', size: 4096};
  const invalid = {name: 'proof.gif', type: 'image/gif', size: 1024};
  const oversized = {name: 'large.webp', type: 'image/webp', size: COMPLAINT_MAX_IMAGE_BYTES + 1};

  const selected = selectComplaintImageFiles([png, invalid, jpeg, oversized, webp]);
  assert.deepEqual(selected.accepted, [png, jpeg, webp]);
  assert.deepEqual(selected.errors, [
    'proof.gif：仅支持 PNG、JPEG 或 WebP 图片',
    'large.webp：单张图片不能超过 5 MB',
  ]);

  const limited = selectComplaintImageFiles([png, jpeg, webp], {currentCount: 2});
  assert.deepEqual(limited.accepted, [png]);
  assert.deepEqual(limited.errors, ['最多上传 3 张图片']);
});

test('always submits a blank email code when verification is disabled', () => {
  const base = {
    trade_no: 'LD-1001',
    reason: COMPLAINT_REASON_OPTIONS[0],
    content: '补充说明',
    contact: 'buyer@example.com',
    images: [],
    collect_image: '',
    query_pwd: '123456',
    email_code: '',
  };

  const result = validateComplaintPayload({...base, email_code: 'ignored-value'});
  assert.equal(result.valid, true);
  assert.equal(result.payload.email_code, '');
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

test('normalizes password-protected order detail with delivery and seller fields', () => {
  const detail = normalizeOrderDetail({
    detail: {
      trade_no: 'LD-DETAIL-1',
      goods_name: '测试卡密',
      goods_type: 'card',
      status: 1,
      status_label: '交易成功',
      total_amount: 28.8,
      quantity: 2,
      created_at: 1_788_000_000,
      success_at: 1_788_000_120,
      sendout: 1,
      contact: 'buyer@example.test',
      can_complaint: 1,
      seller: {
        nickname: '测试店铺',
        avatar: 'https://cdn.example.test/avatar.png',
        shop_url: 'https://pay.ldxp.cn/shop/TEST',
        contact_qq: '12345',
      },
      instructions: {
        text: '请妥善保管卡密',
        links: [{label: '使用说明', url: 'https://docs.example.test/use'}],
      },
      delivery: {
        kind: 'card',
        cards: ['CARD-A', 'CARD-B'],
        api_status: 1,
        message: '发货完成',
        links: [{label: '导出卡密', url: 'https://pay.ldxp.cn/export/cards'}],
        truncated: true,
      },
      query_password: 'must-not-leak',
      internal_ticket: 'must-not-leak',
    },
  });

  assert.equal(detail.status_label, '交易成功');
  assert.equal(detail.status_tone, 'success');
  assert.equal(detail.goods_type_label, '卡密商品');
  assert.equal(detail.sendout, 1);
  assert.deepEqual(detail.delivery.cards, ['CARD-A', 'CARD-B']);
  assert.equal(detail.delivery.links[0].url, 'https://pay.ldxp.cn/export/cards');
  assert.equal(detail.seller.contact_qq, '12345');
  assert.equal(detail.instructions.links[0].label, '使用说明');
  assert.equal(detail.delivery.truncated, true);
  assert.equal('query_password' in detail, false);
  assert.doesNotMatch(JSON.stringify(detail), /must-not-leak/);
});

test('filters unsafe detail links and uses stable fallbacks', () => {
  const detail = normalizeOrderDetail({
    goods_type: 'resource',
    seller: {avatar: 'javascript:alert(1)', shop_url: 'https://example.test/shop'},
    instructions: {links: [{label: 'bad', url: 'data:text/html,bad'}]},
    delivery: {
      cards: ['  ', null, 'RESOURCE-CODE'],
      api_status: 'not-a-number',
      links: [{label: 'download', url: 'ftp://example.test/file'}],
    },
  }, {trade_no: 'FALLBACK-1', goods_name: '备用商品', status: 0, quantity: 1});

  assert.equal(detail.trade_no, 'FALLBACK-1');
  assert.equal(detail.goods_name, '备用商品');
  assert.equal(detail.status_label, '待付款');
  assert.equal(detail.seller.avatar, '');
  assert.equal(detail.seller.shop_url, 'https://example.test/shop');
  assert.deepEqual(detail.instructions.links, []);
  assert.deepEqual(detail.delivery.cards, ['RESOURCE-CODE']);
  assert.equal(detail.delivery.api_status, null);
  assert.deepEqual(detail.delivery.links, []);
  assert.equal(orderDeliveryKindLabel(detail.delivery.kind), '资源信息');
});

test('allows only credential-free HTTP detail URLs', () => {
  assert.equal(safeOrderContentUrl('https://example.test/path'), 'https://example.test/path');
  assert.equal(safeOrderContentUrl('http://example.test/path'), 'http://example.test/path');
  assert.equal(safeOrderContentUrl('https://user:pass@example.test/path'), '');
  assert.equal(safeOrderContentUrl('javascript:alert(1)'), '');
});

test('formats one or many delivered cards for a single clipboard write', () => {
  assert.equal(formatOrderCardsForCopy(['CARD-A']), 'CARD-A');
  assert.equal(formatOrderCardsForCopy(['CARD-A', 'CARD-B']), 'CARD-A\nCARD-B');
  assert.equal(formatOrderCardsForCopy(['', null, 'CARD-C']), 'CARD-C');
  assert.equal(formatOrderCardsForCopy(['  CARD-A\n']), '  CARD-A\n');
});

test('preserves delivered card bytes and classifies an expired detail session', () => {
  const detail = normalizeOrderDetail({delivery: {kind: 'card', cards: ['  CARD-A\n', '   ']}});
  assert.deepEqual(detail.delivery.cards, ['  CARD-A\n']);
  assert.deepEqual(orderDetailErrorState({status: 410, message: 'expired'}), {
    sessionExpired: true,
    message: '订单查询会话已失效，请重新查询订单后再验证',
  });
  assert.deepEqual(orderDetailErrorState({status: 403, message: '安全密码错误'}), {
    sessionExpired: false,
    message: '安全密码错误',
  });
  assert.deepEqual(orderDetailErrorState({status: 502, message: ''}), {
    sessionExpired: false,
    message: '订单详情读取失败，请重试',
  });
});
