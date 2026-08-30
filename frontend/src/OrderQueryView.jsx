import React, {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {
  AlertCircle,
  ArrowUpRight,
  Check,
  ChevronLeft,
  ChevronRight,
  Clipboard,
  Database,
  Eye,
  EyeOff,
  ExternalLink,
  Image,
  KeyRound,
  Link2,
  Package,
  ReceiptText,
  RefreshCw,
  Search,
  ShieldCheck,
  Store,
  TriangleAlert,
  X,
} from 'lucide-react';
import {
  ORDER_STATUS_OPTIONS,
  canOpenProtectedOrderDetail,
  formatOrderDateTime,
  formatOrderCardsForCopy,
  formatOrderMoney,
  normalizeOrderDetail,
  normalizeOrderResponse,
  orderDeliveryKindLabel,
  orderDetailErrorState,
  orderQueryContextChanged,
  summarizeOrders,
  verificationLabel,
} from './orderQueryModel.js';
import './orderQuery.css';

const EMPTY_RESULT = {orders: [], pagination: {page: 1, page_size: 10, total: 0, pages: 1}};

function QueryIconButton({label, children, ...props}) {
  return <button className="icon-button" type="button" aria-label={label} title={label} {...props}>{children}</button>;
}

function OrderImage({order}) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [order.goods_image]);
  if (!order.goods_image || failed) return <span className="order-product-image fallback"><Package size={18}/></span>;
  return <img className="order-product-image" src={order.goods_image} alt="" onError={() => setFailed(true)}/>;
}

function VerificationPanel({busy, verification, captcha, captchaCode, onCaptchaCode, onRefresh, onSubmit, expiresIn}) {
  const manualRequired = verification?.status === 'manual_required';
  const attempts = Number(verification?.attempts || 0);
  const state = busy ? 'busy' : manualRequired ? 'manual' : verification?.status === 'verified' ? 'verified' : 'idle';
  const stateLabel = busy
    ? busy === 'refresh' ? '正在刷新验证码' : busy === 'manual' ? '正在核对验证码' : '正在自动识别并查询'
    : verificationLabel(verification);
  const imageSource = captcha?.image_data_url || captcha?.image_url || '';

  return <section className={`order-verification ${state}`} aria-live="polite">
    <div className="order-verification-head">
      <span className="order-verification-icon">{state === 'busy' ? <RefreshCw className="spin" size={18}/> : state === 'verified' ? <Check size={18}/> : state === 'manual' ? <TriangleAlert size={18}/> : <ShieldCheck size={18}/>}</span>
      <div><span>CAPTCHA SESSION</span><strong>{stateLabel}</strong></div>
      {attempts > 0 && <em>尝试 {attempts} 次</em>}
    </div>
    {!manualRequired ? <div className="order-verification-flow" aria-label="验证码处理流程">
      <span className={state !== 'idle' ? 'done' : ''}>获取挑战</span>
      <span className={state === 'verified' ? 'done' : state === 'busy' ? 'active' : ''}>自动识别</span>
      <span className={state === 'verified' ? 'done' : ''}>官网校验</span>
      <span className={state === 'verified' ? 'done' : ''}>读取订单</span>
    </div> : <form className="order-captcha-form" onSubmit={onSubmit}>
      <div className="order-captcha-preview">
        {imageSource ? <img src={imageSource} alt="链动小铺验证码"/> : <span><Image size={22}/>验证码图片待刷新</span>}
        <QueryIconButton label="刷新验证码" onClick={onRefresh} disabled={Boolean(busy)}><RefreshCw size={15} className={busy === 'refresh' ? 'spin' : ''}/></QueryIconButton>
      </div>
      <label><span>验证码</span><input value={captchaCode} onChange={event => onCaptchaCode(event.target.value.replace(/[^A-Za-z0-9]/g, '').slice(0, 4))} placeholder="4 位字母或数字" autoComplete="off" spellCheck="false" inputMode="text" pattern="[A-Za-z0-9]{4}" maxLength={4} autoFocus/></label>
      <button className="button primary" type="submit" disabled={Boolean(busy) || !/^[A-Za-z0-9]{4}$/.test(captchaCode)}><ShieldCheck size={15}/>{busy === 'manual' ? '正在验证' : '验证并继续'}</button>
      <small>{expiresIn ? `当前验证会话约 ${Math.max(1, Math.ceil(Number(expiresIn) / 60))} 分钟内有效` : '自动识别未通过，请确认图片内容'}</small>
    </form>}
  </section>;
}

function SellerAvatar({seller}) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [seller.avatar]);
  if (!seller.avatar || failed) return <span className="order-seller-avatar fallback"><Store size={18}/></span>;
  return <img className="order-seller-avatar" src={seller.avatar} alt="" onError={() => setFailed(true)}/>;
}

function DetailCopyButton({label, value, onCopy}) {
  return <QueryIconButton label={`复制${label}`} onClick={() => onCopy(value, label)} disabled={!value}><Clipboard size={13}/></QueryIconButton>;
}

function OrderDetailDialog({
  order,
  detail,
  password,
  passwordVisible,
  busy,
  error,
  sessionExpired,
  dialogRef,
  passwordInputRef,
  onPassword,
  onTogglePassword,
  onSubmit,
  onClose,
  onCopy,
  onRequery,
}) {
  const showingDetail = Boolean(detail);
  const dialogTitle = showingDetail ? '订单详情' : '安全密码验证';
  const seller = detail?.seller || {};
  const sellerContacts = detail ? [
    {label: '卖家 QQ', value: seller.contact_qq},
    {label: '卖家微信', value: seller.contact_wechat},
    {label: '卖家手机', value: seller.contact_mobile},
  ].filter(item => item.value) : [];
  const hasSeller = Boolean(seller.nickname || seller.avatar || seller.shop_url || sellerContacts.length);
  const delivery = detail?.delivery || {};
  const hasDelivery = Boolean(delivery.cards?.length || delivery.content || delivery.message || delivery.links?.length);
  const detailFacts = detail ? [
    {label: '订单号', value: detail.trade_no || '--', copy: Boolean(detail.trade_no)},
    {label: '下单时间', value: detail.created_at ? formatOrderDateTime(detail.created_at) : '--'},
    {label: '支付时间', value: detail.success_at ? formatOrderDateTime(detail.success_at) : '--'},
    {label: '购买数量', value: `${detail.quantity} 件`},
    {label: '实付金额', value: formatOrderMoney(detail.total_amount)},
    {label: '发货进度', value: detail.sendout === null ? '--' : `已发 ${detail.sendout} / ${detail.quantity} 件`},
    {label: '买家联系方式', value: detail.contact || '--', copy: Boolean(detail.contact)},
    {label: '售后服务', value: detail.can_complaint ? '支持申诉' : '暂不支持申诉'},
  ] : [];

  return <div className="modal-backdrop order-detail-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}>
    <div
      className={`checkout-modal ${showingDetail ? 'order-detail-modal' : 'order-password-modal'}`}
      role="dialog"
      aria-modal="true"
      aria-labelledby="order-detail-dialog-title"
      aria-describedby={showingDetail ? undefined : 'order-password-description'}
      ref={dialogRef}
      tabIndex={-1}
    >
      <div className="modal-head order-dialog-head">
        <div><span>{showingDetail ? 'ORDER DETAIL' : 'SECURE ORDER'}</span><h2 id="order-detail-dialog-title">{dialogTitle}</h2></div>
        <QueryIconButton label="关闭订单详情" onClick={onClose}><X size={17}/></QueryIconButton>
      </div>

      {!showingDetail ? <form className="order-password-form" onSubmit={onSubmit}>
        <div className="order-password-context">
          <span className="order-password-context-icon"><KeyRound size={20}/></span>
          <div><strong>{order.goods_name}</strong><small>{order.trade_no}</small></div>
          <span className={`order-status ${order.status_tone}`}>{order.status_label}</span>
        </div>
        <p id="order-password-description" className="order-password-description">此订单的交付内容受安全密码保护</p>
        <label className="order-password-label" htmlFor="order-query-password">安全密码</label>
        <div className={`order-password-control ${error ? 'invalid' : ''}`}>
          <KeyRound size={16}/>
          <input
            id="order-query-password"
            ref={passwordInputRef}
            type={passwordVisible ? 'text' : 'password'}
            value={password}
            onChange={event => onPassword(event.target.value)}
            placeholder="请输入安全密码"
            autoComplete="off"
            maxLength={128}
            aria-invalid={Boolean(error)}
            aria-describedby={error ? 'order-password-error' : 'order-password-description'}
            disabled={busy || sessionExpired}
          />
          <QueryIconButton label={passwordVisible ? '隐藏安全密码' : '显示安全密码'} onClick={onTogglePassword} disabled={busy || sessionExpired}>{passwordVisible ? <EyeOff size={15}/> : <Eye size={15}/>}</QueryIconButton>
        </div>
        {error && <div className="order-password-error" id="order-password-error" role="alert"><TriangleAlert size={15}/><span>{error}</span></div>}
        <div className="order-dialog-foot">
          <span>{sessionExpired ? <TriangleAlert size={14}/> : <ShieldCheck size={14}/>} {sessionExpired ? '查询验证会话已失效' : '密码仅用于本次订单验证'}</span>
          <div>
            <button className="button secondary" type="button" onClick={onClose}>取消</button>
            {sessionExpired
              ? <button className="button primary" type="button" onClick={onRequery}><RefreshCw size={15}/>关闭并重新查询</button>
              : <button className="button primary" type="submit" disabled={busy || !password.length}><ShieldCheck size={15}/>{busy ? '正在验证' : '验证并查看'}</button>}
          </div>
        </div>
      </form> : <>
        <div className="order-detail-scroll">
          <section className="order-detail-identity">
            <OrderImage order={order}/>
            <div><span>{detail.goods_type_label}</span><strong>{detail.goods_name}</strong><small>{detail.trade_no}</small></div>
            <span className={`order-status ${detail.status_tone}`}>{detail.status_label}</span>
          </section>

          <section className="order-detail-section">
            <div className="order-detail-section-head"><div><span>ORDER INFORMATION</span><h3>订单信息</h3></div></div>
            <dl className="order-detail-facts">
              {detailFacts.map(item => <div key={item.label}><dt>{item.label}</dt><dd><span>{item.value}</span>{item.copy && <DetailCopyButton label={item.label} value={item.value} onCopy={onCopy}/>}</dd></div>)}
            </dl>
          </section>

          {hasSeller && <section className="order-detail-section">
            <div className="order-detail-section-head"><div><span>SELLER</span><h3>卖家信息</h3></div>{seller.shop_url && <a className="icon-button" href={seller.shop_url} target="_blank" rel="noreferrer" aria-label="打开卖家店铺" title="打开卖家店铺"><ExternalLink size={14}/></a>}</div>
            <div className="order-seller-row"><SellerAvatar seller={seller}/><div><strong>{seller.nickname || '链动小铺卖家'}</strong><small>{sellerContacts.length ? '可通过以下方式联系卖家' : '卖家店铺信息'}</small></div></div>
            {sellerContacts.length > 0 && <dl className="order-seller-contacts">{sellerContacts.map(item => <div key={item.label}><dt>{item.label}</dt><dd><span>{item.value}</span><DetailCopyButton label={item.label} value={item.value} onCopy={onCopy}/></dd></div>)}</dl>}
          </section>}

          {(detail.instructions.text || detail.instructions.links.length > 0) && <section className="order-detail-section">
            <div className="order-detail-section-head"><div><span>INSTRUCTIONS</span><h3>使用说明</h3></div></div>
            {detail.instructions.text && <p className="order-detail-rich-text">{detail.instructions.text}</p>}
            {detail.instructions.links.length > 0 && <div className="order-detail-links">{detail.instructions.links.map((link, index) => <a href={link.url} target="_blank" rel="noreferrer" key={`${link.url}-${index}`}><Link2 size={14}/><span>{link.label}</span><ExternalLink size={12}/></a>)}</div>}
          </section>}

          <section className="order-detail-section order-delivery-section">
            <div className="order-detail-section-head"><div><span>DELIVERY</span><h3>{orderDeliveryKindLabel(delivery.kind)}</h3></div><div className="order-detail-section-actions">
              {delivery.api_status !== null && delivery.api_status !== undefined && <em>接口状态 {delivery.api_status}</em>}
              {delivery.cards?.length > 0 && <button className="button secondary order-copy-all" type="button" onClick={() => onCopy(formatOrderCardsForCopy(delivery.cards), '全部卡密')}><Clipboard size={14}/>复制全部</button>}
            </div></div>
            {delivery.message && <div className="order-delivery-message"><ShieldCheck size={15}/><span>{delivery.message}</span></div>}
            {delivery.cards?.length > 0 && <div className="order-card-list">{delivery.cards.map((card, index) => <div key={`${card}-${index}`}><span>卡密 {index + 1}</span><code>{card}</code><DetailCopyButton label={`卡密 ${index + 1}`} value={card} onCopy={onCopy}/></div>)}</div>}
            {delivery.content && <p className="order-detail-rich-text delivery-content">{delivery.content}</p>}
            {delivery.links?.length > 0 && <div className="order-detail-links">{delivery.links.map((link, index) => <a href={link.url} target="_blank" rel="noreferrer" key={`${link.url}-${index}`}><Link2 size={14}/><span>{link.label}</span><ExternalLink size={12}/></a>)}</div>}
            {!hasDelivery && <div className="order-detail-empty"><ReceiptText size={20}/><strong>暂无可展示的交付内容</strong><span>订单详情已读取，但没有返回卡密、正文或资源链接</span></div>}
            {delivery.truncated && <div className="order-detail-truncated"><TriangleAlert size={14}/><span>交付内容较长，当前仅显示部分结果</span></div>}
          </section>
        </div>
        <div className="order-dialog-foot order-detail-foot">
          <span><ShieldCheck size={14}/>安全密码已验证</span>
          <button className="button primary" type="button" onClick={onClose}>关闭</button>
        </div>
      </>}
    </div>
  </div>;
}

function OrderRow({order, onCopy, onOpenProtectedDetail}) {
  return <tr>
    <td data-label="商品">
      <div className="order-product-cell"><OrderImage order={order}/><span><strong>{order.goods_name}</strong><small>{formatOrderDateTime(order.created_at)}</small></span></div>
    </td>
    <td data-label="订单号"><div className="order-number"><strong title={order.trade_no}>{order.trade_no || '--'}</strong><QueryIconButton label={`复制订单号 ${order.trade_no}`} onClick={() => onCopy(order.trade_no)} disabled={!order.trade_no}><Clipboard size={13}/></QueryIconButton></div></td>
    <td data-label="金额 / 数量"><div className="order-amount"><strong>{formatOrderMoney(order.total_amount)}</strong><small>共 {order.quantity} 件</small></div></td>
    <td data-label="状态"><span className={`order-status ${order.status_tone}`}>{order.status_label}</span></td>
    <td data-label="商品类型"><div className="order-type"><strong>{order.goods_type_label}</strong>{order.need_query_password && <small><ShieldCheck size={11}/>权益需安全密码</small>}</div></td>
    <td data-label="操作"><div className="order-row-actions">
      {order.detail_url && <a className="icon-button" href={order.detail_url} target="_blank" rel="noreferrer" aria-label="打开官方订单详情" title="打开官方订单详情"><ExternalLink size={14}/></a>}
      {canOpenProtectedOrderDetail(order) && <button className="button secondary order-result-link order-password-button" type="button" onClick={event => onOpenProtectedDetail(order, event.currentTarget)}><KeyRound size={14}/>安全密码</button>}
      {order.status === 1 && order.result_url && !order.need_query_password && <a className="button secondary order-result-link" href={order.result_url} target="_blank" rel="noreferrer"><ArrowUpRight size={14}/>{order.goods_action_label}</a>}
    </div></td>
  </tr>;
}

export default function OrderQueryView({request, notify, initialKeywords = ''}) {
  const [keywords, setKeywords] = useState(initialKeywords || '');
  const [submittedKeywords, setSubmittedKeywords] = useState('');
  const [activeStatus, setActiveStatus] = useState(999);
  const [pageSize, setPageSize] = useState(10);
  const [result, setResult] = useState(EMPTY_RESULT);
  const [verification, setVerification] = useState(null);
  const [captcha, setCaptcha] = useState(null);
  const [captchaCode, setCaptchaCode] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [expiresIn, setExpiresIn] = useState(0);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [hasQueried, setHasQueried] = useState(false);
  const [queriedAt, setQueriedAt] = useState(null);
  const [detailOrder, setDetailOrder] = useState(null);
  const [orderDetail, setOrderDetail] = useState(null);
  const [detailPassword, setDetailPassword] = useState('');
  const [detailPasswordVisible, setDetailPasswordVisible] = useState(false);
  const [detailBusy, setDetailBusy] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [detailSessionExpired, setDetailSessionExpired] = useState(false);
  const requestVersion = useRef(0);
  const detailRequestVersion = useRef(0);
  const detailAbortController = useRef(null);
  const detailDialogRef = useRef(null);
  const detailPasswordInputRef = useRef(null);
  const detailTriggerRef = useRef(null);
  const resultContext = useRef({keywords: '', status: 999, pageSize: 10});
  const initialKeywordsApplied = useRef(false);

  const closeOrderDetail = useCallback(() => {
    detailRequestVersion.current += 1;
    detailAbortController.current?.abort();
    detailAbortController.current = null;
    setDetailOrder(null);
    setOrderDetail(null);
    setDetailPassword('');
    setDetailPasswordVisible(false);
    setDetailBusy(false);
    setDetailError('');
    setDetailSessionExpired(false);
    const trigger = detailTriggerRef.current;
    detailTriggerRef.current = null;
    window.requestAnimationFrame(() => {
      if (trigger?.isConnected) trigger.focus();
    });
  }, []);

  useEffect(() => () => {
    detailRequestVersion.current += 1;
    detailAbortController.current?.abort();
    detailAbortController.current = null;
  }, []);

  useEffect(() => {
    if (initialKeywordsApplied.current || !initialKeywords) return;
    initialKeywordsApplied.current = true;
    setKeywords(current => current || initialKeywords);
  }, [initialKeywords]);

  useEffect(() => {
    if (!detailOrder) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const focusFrame = window.requestAnimationFrame(() => {
      if (orderDetail) detailDialogRef.current?.focus();
      else detailPasswordInputRef.current?.focus();
    });
    const handleKeyDown = event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeOrderDetail();
        return;
      }
      if (event.key !== 'Tab' || !detailDialogRef.current) return;
      const focusable = Array.from(detailDialogRef.current.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ));
      if (!focusable.length) {
        event.preventDefault();
        detailDialogRef.current.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || !detailDialogRef.current.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !detailDialogRef.current.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [closeOrderDetail, detailOrder, orderDetail]);

  const summary = useMemo(() => summarizeOrders(result.orders, result.pagination.total), [result]);
  const manualRequired = verification?.status === 'manual_required';

  const sendSearch = async (payload, allowExpiredRetry = true) => {
    try {
      return await request('/order-query/search', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
    } catch (requestError) {
      if (allowExpiredRetry && requestError.status === 410 && payload.session_id) {
        const retryPayload = {...payload};
        delete retryPayload.session_id;
        delete retryPayload.captcha_code;
        return sendSearch(retryPayload, false);
      }
      throw requestError;
    }
  };

  const runSearch = async ({
    keywordValue = submittedKeywords || keywords,
    statusValue = activeStatus,
    pageValue = 1,
    pageSizeValue = pageSize,
    manualCode = '',
    refreshCaptcha = false,
    reuseSession = true,
    quiet = false,
  } = {}) => {
    const normalizedKeywords = keywordValue.trim();
    if (!normalizedKeywords) {
      notify('请输入预留联系方式或订单号', 'error');
      return false;
    }

    const version = requestVersion.current + 1;
    requestVersion.current = version;
    const isNewKeyword = normalizedKeywords !== submittedKeywords;
    const canReuseSession = reuseSession && !isNewKeyword && Boolean(sessionId);
    const nextContext = {
      keywords: normalizedKeywords,
      status: Number(statusValue),
      pageSize: Number(pageSizeValue),
    };
    const resultWouldBeStale = hasQueried && orderQueryContextChanged(resultContext.current, nextContext);
    const clearStaleResult = () => {
      if (!resultWouldBeStale) return;
      setResult({orders: [], pagination: {page: 1, page_size: nextContext.pageSize, total: 0, pages: 1}});
      setHasQueried(false);
      setQueriedAt(null);
    };
    if (isNewKeyword) {
      setSubmittedKeywords(normalizedKeywords);
      setResult(EMPTY_RESULT);
      setHasQueried(false);
      setVerification(null);
      setCaptcha(null);
      setCaptchaCode('');
      setSessionId('');
      setExpiresIn(0);
      setQueriedAt(null);
    }
    setBusy(refreshCaptcha ? 'refresh' : manualCode ? 'manual' : 'search');
    setError('');
    const payload = {
      keywords: normalizedKeywords,
      status: Number(statusValue),
      page: Number(pageValue),
      page_size: Number(pageSizeValue),
    };
    if (canReuseSession) payload.session_id = sessionId;
    if (manualCode) payload.captcha_code = manualCode;
    if (refreshCaptcha) payload.refresh_captcha = true;

    try {
      const response = await sendSearch(payload);
      if (version !== requestVersion.current) return false;
      setSessionId(String(response.session_id || ''));
      setExpiresIn(Number(response.expires_in || 0));
      setVerification(response.verification || null);
      setSubmittedKeywords(normalizedKeywords);

      if (response.verification?.status === 'manual_required') {
        clearStaleResult();
        setCaptcha(response.captcha || null);
        setCaptchaCode('');
        if (!quiet && !refreshCaptcha) notify({
          type: 'warning',
          title: '需要确认验证码',
          message: manualCode ? '验证码未通过，请刷新或重新输入' : '自动识别未通过，请核对验证码后继续查询',
          duration: 6000,
        });
        return false;
      }

      const normalized = normalizeOrderResponse(response, {page: pageValue, pageSize: pageSizeValue});
      setResult(normalized);
      resultContext.current = nextContext;
      setCaptcha(null);
      setCaptchaCode('');
      setHasQueried(true);
      setQueriedAt(new Date());
      if (!quiet) notify({type: 'success', title: '订单查询完成', message: `已获取 ${normalized.pagination.total} 笔订单`, duration: 4200});
      return true;
    } catch (requestError) {
      if (version !== requestVersion.current) return false;
      clearStaleResult();
      setError(requestError.message || '订单查询失败');
      notify(requestError.message || '订单查询失败', 'error');
      return false;
    } finally {
      if (version === requestVersion.current) setBusy('');
    }
  };

  const submitSearch = event => {
    event.preventDefault();
    runSearch({keywordValue: keywords, pageValue: 1, reuseSession: !manualRequired});
  };

  const submitManualCaptcha = event => {
    event.preventDefault();
    runSearch({manualCode: captchaCode.trim(), pageValue: 1});
  };

  const openProtectedOrderDetail = (order, trigger) => {
    const sessionAvailable = Boolean(sessionId && submittedKeywords);
    detailRequestVersion.current += 1;
    detailAbortController.current?.abort();
    detailAbortController.current = null;
    detailTriggerRef.current = trigger || document.activeElement;
    setDetailOrder(order);
    setOrderDetail(null);
    setDetailPassword('');
    setDetailPasswordVisible(false);
    setDetailBusy(false);
    setDetailError(sessionAvailable ? '' : '订单查询会话已失效，请重新查询订单后再验证');
    setDetailSessionExpired(!sessionAvailable);
  };

  const submitOrderDetailPassword = async event => {
    event.preventDefault();
    if (!detailOrder || detailBusy || detailSessionExpired || !detailPassword.length) return;
    if (!sessionId || !submittedKeywords) {
      setSessionId('');
      setVerification(null);
      setExpiresIn(0);
      setDetailPassword('');
      setDetailPasswordVisible(false);
      setDetailSessionExpired(true);
      setDetailError('订单查询会话已失效，请重新查询订单后再验证');
      return;
    }

    const version = detailRequestVersion.current + 1;
    detailRequestVersion.current = version;
    detailAbortController.current?.abort();
    const controller = new AbortController();
    detailAbortController.current = controller;
    const password = detailPassword;
    setDetailBusy(true);
    setDetailError('');
    try {
      const response = await request('/order-query/detail', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          session_id: sessionId,
          keywords: submittedKeywords,
          trade_no: detailOrder.trade_no,
          query_password: password,
        }),
        signal: controller.signal,
      });
      if (version !== detailRequestVersion.current) return;
      if (!response.detail || typeof response.detail !== 'object') throw new Error('订单详情响应格式无效');
      setSessionId(String(response.session_id || sessionId));
      setExpiresIn(Number(response.expires_in || expiresIn));
      setOrderDetail(normalizeOrderDetail(response, detailOrder));
      setDetailPassword('');
      setDetailPasswordVisible(false);
      setDetailError('');
      setDetailSessionExpired(false);
    } catch (requestError) {
      if (controller.signal.aborted || requestError?.name === 'AbortError') return;
      if (version !== detailRequestVersion.current) return;
      const errorState = orderDetailErrorState(requestError);
      if (errorState.sessionExpired) {
        setSessionId('');
        setVerification(null);
        setExpiresIn(0);
      }
      setOrderDetail(null);
      setDetailPassword('');
      setDetailPasswordVisible(false);
      setDetailSessionExpired(errorState.sessionExpired);
      setDetailError(errorState.message);
      if (!errorState.sessionExpired) window.requestAnimationFrame(() => detailPasswordInputRef.current?.focus());
    } finally {
      if (detailAbortController.current === controller) detailAbortController.current = null;
      if (version === detailRequestVersion.current) setDetailBusy(false);
    }
  };

  const requeryAfterDetailExpiry = () => {
    const keyword = submittedKeywords;
    const page = result.pagination.page || 1;
    closeOrderDetail();
    if (keyword) runSearch({keywordValue: keyword, pageValue: page, reuseSession: false});
  };

  const changeStatus = status => {
    setActiveStatus(status);
    if (submittedKeywords && !manualRequired) runSearch({statusValue: status, pageValue: 1, quiet: true});
  };

  const changePage = page => {
    if (!submittedKeywords || page === result.pagination.page || busy) return;
    runSearch({pageValue: page, quiet: true});
  };

  const changePageSize = value => {
    const nextSize = Number(value);
    setPageSize(nextSize);
    if (submittedKeywords && !manualRequired) runSearch({pageSizeValue: nextSize, pageValue: 1, quiet: true});
  };

  const copyDetailValue = async (value, label = '内容') => {
    try {
      await navigator.clipboard.writeText(String(value));
      notify({type: 'success', title: `${label}已复制`, message: '已写入剪贴板', duration: 2400});
    } catch {
      notify('无法访问剪贴板，请检查浏览器权限', 'error');
    }
  };

  const copyOrderNumber = tradeNo => copyDetailValue(tradeNo, '订单号');

  const emptyMessage = busy && !result.orders.length
    ? {icon: <RefreshCw className="spin" size={22}/>, title: '正在自动验证并读取订单', detail: '结果返回后会显示在这里'}
    : error && !result.orders.length
      ? {icon: <AlertCircle size={22}/>, title: '订单查询未完成', detail: error}
      : hasQueried
        ? {icon: <Database size={22}/>, title: '没有查询到订单', detail: '请核对联系方式、订单号或切换订单状态'}
        : {icon: <ReceiptText size={22}/>, title: '尚未查询订单', detail: '输入购买时预留的联系方式或订单号'};

  return <section className="order-query-view">
    <section className="order-query-console">
      <div className="order-query-form-panel">
        <div className="order-section-heading"><div><span>LDXP ORDER LOOKUP</span><h2>链动小铺订单查询</h2><p>使用购买时预留的联系方式或订单号查询</p></div><a className="icon-button" href="https://pay.ldxp.cn/order" target="_blank" rel="noreferrer" aria-label="打开官方订单查询" title="打开官方订单查询"><ArrowUpRight size={16}/></a></div>
        <form className="order-search-form" onSubmit={submitSearch}>
          <label><span>联系方式 / 订单号</span><div><Search size={16}/><input value={keywords} onChange={event => setKeywords(event.target.value)} placeholder="邮箱、手机号、QQ 或订单号" autoComplete="off" spellCheck="false"/></div></label>
          <button className="button primary" type="submit" disabled={Boolean(busy) || !keywords.trim()}><ShieldCheck size={16}/>{busy === 'search' ? '正在查询' : '自动验证并查询'}</button>
        </form>
        <div className="order-query-source"><span><ShieldCheck size={13}/>数据来源</span><strong>pay.ldxp.cn/order</strong>{submittedKeywords && <em>当前查询：{submittedKeywords}</em>}</div>
        {error && <div className="order-query-error"><TriangleAlert size={15}/><span>{error}</span><button type="button" onClick={() => runSearch({pageValue: result.pagination.page || 1})}>重试</button></div>}
      </div>
      <VerificationPanel
        busy={busy}
        verification={verification}
        captcha={captcha}
        captchaCode={captchaCode}
        onCaptchaCode={setCaptchaCode}
        onRefresh={() => runSearch({refreshCaptcha: true, quiet: true})}
        onSubmit={submitManualCaptcha}
        expiresIn={expiresIn}
      />
    </section>

    <section className={`order-results-panel ${busy && result.orders.length ? 'updating' : ''}`}>
      <div className="order-results-head">
        <div><span>ORDER LIST</span><h2>购买订单</h2><p>{hasQueried ? `${submittedKeywords} · 共 ${result.pagination.total} 笔` : '查询完成后按订单状态查看结果'}</p></div>
        <div className="order-results-actions">
          <label><span>每页</span><select value={pageSize} onChange={event => changePageSize(event.target.value)} disabled={!hasQueried || Boolean(busy)}><option value="10">10</option><option value="20">20</option><option value="50">50</option></select></label>
          <QueryIconButton label="刷新当前订单" onClick={() => runSearch({pageValue: result.pagination.page, quiet: true})} disabled={!submittedKeywords || Boolean(busy)}><RefreshCw size={16} className={busy ? 'spin' : ''}/></QueryIconButton>
        </div>
      </div>

      <div className="order-status-tabs" role="tablist" aria-label="订单状态">
        {ORDER_STATUS_OPTIONS.map(option => <button type="button" role="tab" aria-selected={activeStatus === option.value} className={activeStatus === option.value ? 'active' : ''} key={option.value} onClick={() => changeStatus(option.value)} disabled={Boolean(busy)}>{option.label}</button>)}
      </div>

      {hasQueried && <div className="order-summary-strip">
        <div><span>订单总数</span><strong>{summary.total}</strong></div>
        <div><span>当前页</span><strong>{summary.pageCount}</strong></div>
        <div><span>本页金额</span><strong>{formatOrderMoney(summary.pageAmount)}</strong></div>
        <div><span>验证状态</span><strong>{verificationLabel(verification)}</strong><small>{queriedAt ? formatOrderDateTime(queriedAt) : '--'}</small></div>
      </div>}

      <div className="order-table-wrap">
        <table className="order-query-table">
          <thead><tr><th>商品 / 下单时间</th><th>订单号</th><th>金额 / 数量</th><th>状态</th><th>商品类型</th><th>操作</th></tr></thead>
          <tbody>{result.orders.length ? result.orders.map((order, index) => <OrderRow order={order} onCopy={copyOrderNumber} onOpenProtectedDetail={openProtectedOrderDetail} key={order.trade_no || `${order.goods_key}-${index}`}/>) : <tr className="order-empty-row"><td colSpan="6"><div className="order-empty-state">{emptyMessage.icon}<strong>{emptyMessage.title}</strong><span>{emptyMessage.detail}</span></div></td></tr>}</tbody>
        </table>
      </div>

      <div className="order-pagination">
        <span>第 {result.pagination.page} / {result.pagination.pages} 页 · 共 {result.pagination.total} 笔</span>
        <div><QueryIconButton label="上一页" onClick={() => changePage(Math.max(1, result.pagination.page - 1))} disabled={Boolean(busy) || result.pagination.page <= 1}><ChevronLeft size={16}/></QueryIconButton><QueryIconButton label="下一页" onClick={() => changePage(Math.min(result.pagination.pages, result.pagination.page + 1))} disabled={Boolean(busy) || result.pagination.page >= result.pagination.pages}><ChevronRight size={16}/></QueryIconButton></div>
      </div>
      {busy && result.orders.length > 0 && <div className="order-update-indicator"><RefreshCw className="spin" size={14}/>正在更新订单</div>}
    </section>
    {detailOrder && <OrderDetailDialog
      order={detailOrder}
      detail={orderDetail}
      password={detailPassword}
      passwordVisible={detailPasswordVisible}
      busy={detailBusy}
      error={detailError}
      sessionExpired={detailSessionExpired}
      dialogRef={detailDialogRef}
      passwordInputRef={detailPasswordInputRef}
      onPassword={value => {
        setDetailPassword(value);
        if (detailError) setDetailError('');
      }}
      onTogglePassword={() => setDetailPasswordVisible(current => !current)}
      onSubmit={submitOrderDetailPassword}
      onClose={closeOrderDetail}
      onCopy={copyDetailValue}
      onRequery={requeryAfterDetailExpiry}
    />}
  </section>;
}
