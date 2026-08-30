import React, {useEffect, useMemo, useRef, useState} from 'react';
import {
  AlertCircle,
  ArrowUpRight,
  Check,
  ChevronLeft,
  ChevronRight,
  Clipboard,
  Clock3,
  Database,
  ExternalLink,
  Image,
  Package,
  ReceiptText,
  RefreshCw,
  Search,
  ShieldCheck,
  TriangleAlert,
} from 'lucide-react';
import {
  ORDER_STATUS_OPTIONS,
  formatOrderDateTime,
  formatOrderMoney,
  normalizeOrderResponse,
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

function OrderRow({order, onCopy}) {
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
  const requestVersion = useRef(0);
  const resultContext = useRef({keywords: '', status: 999, pageSize: 10});
  const initialKeywordsApplied = useRef(false);

  useEffect(() => {
    if (initialKeywordsApplied.current || !initialKeywords) return;
    initialKeywordsApplied.current = true;
    setKeywords(current => current || initialKeywords);
  }, [initialKeywords]);

  const summary = useMemo(() => summarizeOrders(result.orders, result.pagination.total), [result]);
  const verified = verification?.status === 'verified';
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

  const copyOrderNumber = async tradeNo => {
    try {
      await navigator.clipboard.writeText(tradeNo);
      notify(`已复制订单号：${tradeNo}`);
    } catch {
      notify('无法访问剪贴板，请检查浏览器权限', 'error');
    }
  };

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
          <tbody>{result.orders.length ? result.orders.map((order, index) => <OrderRow order={order} onCopy={copyOrderNumber} key={order.trade_no || `${order.goods_key}-${index}`}/>) : <tr className="order-empty-row"><td colSpan="6"><div className="order-empty-state">{emptyMessage.icon}<strong>{emptyMessage.title}</strong><span>{emptyMessage.detail}</span></div></td></tr>}</tbody>
        </table>
      </div>

      <div className="order-pagination">
        <span>第 {result.pagination.page} / {result.pagination.pages} 页 · 共 {result.pagination.total} 笔</span>
        <div><QueryIconButton label="上一页" onClick={() => changePage(Math.max(1, result.pagination.page - 1))} disabled={Boolean(busy) || result.pagination.page <= 1}><ChevronLeft size={16}/></QueryIconButton><QueryIconButton label="下一页" onClick={() => changePage(Math.min(result.pagination.pages, result.pagination.page + 1))} disabled={Boolean(busy) || result.pagination.page >= result.pagination.pages}><ChevronRight size={16}/></QueryIconButton></div>
      </div>
      {busy && result.orders.length > 0 && <div className="order-update-indicator"><RefreshCw className="spin" size={14}/>正在更新订单</div>}
    </section>
  </section>;
}
