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
  FileUp,
  Image,
  ImagePlus,
  KeyRound,
  Link2,
  Maximize2,
  MessageSquareWarning,
  MessagesSquare,
  Package,
  ReceiptText,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Send,
  ShieldCheck,
  Store,
  Trash2,
  TriangleAlert,
  X,
} from 'lucide-react';
import {
  COMPLAINT_REASON_OPTIONS,
  ORDER_STATUS_OPTIONS,
  canOpenOrderDetail,
  canOpenProtectedOrderDetail,
  complaintActionForOrder,
  complaintHistoryErrorState,
  createComplaintDraft,
  formatOrderDateTime,
  formatOrderCardsForCopy,
  formatOrderMoney,
  normalizeOrderDetail,
  normalizeComplaintHistoryResponse,
  normalizeOrderResponse,
  orderDeliveryKindLabel,
  orderDetailErrorState,
  orderQueryContextChanged,
  selectComplaintImageFiles,
  summarizeOrders,
  validateComplaintPayload,
  verificationLabel,
} from './orderQueryModel.js';
import './orderQuery.css';

const EMPTY_RESULT = {orders: [], pagination: {page: 1, page_size: 10, total: 0, pages: 1}};
const COMPLAINT_ACCEPT = '.png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp';
const COMPLAINT_PHASES = new Set(['loading', 'error', 'edit', 'confirm', 'submitting', 'success']);

function complaintPreviewUrl(file) {
  try {
    return typeof URL !== 'undefined' && typeof URL.createObjectURL === 'function'
      ? URL.createObjectURL(file)
      : '';
  } catch {
    return '';
  }
}

function revokeComplaintPreview(url) {
  if (!url || !String(url).startsWith('blob:')) return;
  try {
    URL.revokeObjectURL(url);
  } catch {
    // The URL may already have been revoked by the browser.
  }
}

function QueryIconButton({label, children, ...props}) {
  return <button className="icon-button" type="button" aria-label={label} title={label} {...props}>{children}</button>;
}

// Complaint evidence is rendered as a button so mouse, keyboard, and assistive
// technology users receive the same preview affordance.
function ComplaintImageButton({src, alt = '售后图片', label = '查看图片', onOpen, className = ''}) {
  if (!src) return null;
  return <button
    type="button"
    className={`complaint-image-button ${className}`.trim()}
    aria-label={label}
    title={label}
    onClick={event => onOpen?.(src, alt, event.currentTarget)}
  >
    <img src={src} alt={alt}/>
    <span className="complaint-image-expand" aria-hidden="true"><Maximize2 size={13}/></span>
  </button>;
}

function OrderImage({order}) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [order.goods_image]);
  if (!order.goods_image || failed) return <span className="order-product-image fallback"><Package size={18}/></span>;
  return <img className="order-product-image" src={order.goods_image} alt="" onError={() => setFailed(true)}/>;
}

function VerificationPanel({busy, verification, captcha, captchaCode, onCaptchaCode, onRefresh, onSubmit, expiresIn, waf, wafBusy, onStartWaf, onCompleteWaf}) {
  const manualRequired = verification?.status === 'manual_required';
  const attempts = Number(verification?.attempts || 0);
  const state = busy ? 'busy' : manualRequired ? 'manual' : verification?.status === 'verified' ? 'verified' : 'idle';
  const stateLabel = busy
    ? busy === 'refresh' ? '正在刷新验证码' : busy === 'manual' ? '正在核对验证码' : '正在自动识别并查询'
    : verificationLabel(verification);
  const imageSource = captcha?.image_data_url || captcha?.image_url || '';

  const wafPending = waf?.status === 'awaiting_verification';
  const wafRequired = waf?.status === 'required' || wafPending;
  return <section className={`order-verification ${state} ${wafRequired ? 'waf-required' : ''}`} aria-live="polite">
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
    {wafRequired && <div className="order-waf-panel" role="status">
      <div className="order-waf-copy"><span className="order-waf-icon"><ShieldCheck size={17}/></span><div><strong>需要完成阿里云 WAF 验证</strong><small>{wafPending ? '独立浏览器已打开，完成滑块后系统会自动检测并同步订单' : (waf?.detail || '订单接口暂时拦截了当前请求')}</small></div></div>
      <div className="order-waf-actions">
        {!wafPending ? <button className="button secondary" type="button" onClick={onStartWaf} disabled={Boolean(wafBusy) || Boolean(busy)}><ArrowUpRight size={14}/>{wafBusy === 'start' ? '正在打开浏览器' : '打开浏览器验证'}</button> : <button className="button primary" type="button" onClick={onCompleteWaf} disabled={Boolean(wafBusy) || Boolean(busy)}><ShieldCheck size={14}/>{wafBusy === 'complete' ? '正在同步订单' : '立即检查验证'}</button>}
      </div>
    </div>}
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
  passwordRequired,
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
  const dialogTitle = showingDetail || !passwordRequired ? '订单详情' : '安全密码验证';
  const dialogKicker = showingDetail || !passwordRequired ? 'ORDER DETAIL' : 'SECURE ORDER';
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
      className={`checkout-modal ${showingDetail || !passwordRequired ? 'order-detail-modal' : 'order-password-modal'}`}
      role="dialog"
      aria-modal="true"
      aria-labelledby="order-detail-dialog-title"
      aria-describedby={!showingDetail && passwordRequired ? 'order-password-description' : undefined}
      ref={dialogRef}
      tabIndex={-1}
    >
      <div className="modal-head order-dialog-head">
        <div><span>{dialogKicker}</span><h2 id="order-detail-dialog-title">{dialogTitle}</h2></div>
        <QueryIconButton label="关闭订单详情" onClick={onClose}><X size={17}/></QueryIconButton>
      </div>

      {!showingDetail && passwordRequired ? <form className="order-password-form" onSubmit={onSubmit}>
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
      </form> : !showingDetail ? <div className="order-detail-loading-state" aria-live="polite">
        <div className="order-detail-loading-icon">{busy ? <RefreshCw className="spin" size={22}/> : error ? <AlertCircle size={22}/> : <ReceiptText size={22}/>}</div>
        <strong>{busy ? '正在读取订单详情' : error ? '订单详情读取失败' : '准备读取订单详情'}</strong>
        <span>{error || '正在读取订单金额、状态和交付内容'}</span>
        <div className="order-dialog-foot order-detail-loading-foot">
          <span>{sessionExpired ? <TriangleAlert size={14}/> : <ShieldCheck size={14}/>} {sessionExpired ? '查询验证会话已失效' : '无需安全密码'}</span>
          <div>
            <button className="button secondary" type="button" onClick={onClose}>关闭</button>
            {sessionExpired && <button className="button primary" type="button" onClick={onRequery}><RefreshCw size={15}/>关闭并重新查询</button>}
          </div>
        </div>
      </div> : <>
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

function ComplaintFieldError({id, message}) {
  return message ? <small className="order-complaint-field-error" id={id}>{message}</small> : null;
}

function ComplaintDropZone({label, hint, multiple = false, disabled = false, invalid = false, errorId, onFiles}) {
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef(null);
  const selectFiles = files => {
    if (!disabled && files?.length) onFiles(files);
    if (inputRef.current) inputRef.current.value = '';
  };
  return <label
    className={`order-complaint-drop ${dragging ? 'dragging' : ''} ${disabled ? 'disabled' : ''}`}
    tabIndex={disabled ? -1 : 0}
    aria-disabled={disabled}
    aria-invalid={invalid}
    aria-describedby={invalid ? errorId : undefined}
    onKeyDown={event => {
      if (!disabled && (event.key === 'Enter' || event.key === ' ')) {
        event.preventDefault();
        inputRef.current?.click();
      }
    }}
    onDragEnter={event => { event.preventDefault(); if (!disabled) setDragging(true); }}
    onDragOver={event => event.preventDefault()}
    onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget)) setDragging(false); }}
    onDrop={event => { event.preventDefault(); setDragging(false); selectFiles(event.dataTransfer.files); }}
  >
    <input ref={inputRef} type="file" accept={COMPLAINT_ACCEPT} multiple={multiple} disabled={disabled} onChange={event => selectFiles(event.target.files)}/>
    <ImagePlus size={19}/><span><strong>{label}</strong><small>{hint}</small></span>
  </label>;
}

function ComplaintUploadItem({item, label, onRetry, onRemove, onPreview}) {
  return <div className={`order-complaint-upload ${item.status}`} role="listitem" aria-busy={item.status === 'uploading'}>
    {item.previewUrl
      ? <ComplaintImageButton src={item.previewUrl} alt={`${label}预览`} label={`查看${label}`} onOpen={onPreview}/>
      : <span className="complaint-image-placeholder" aria-hidden="true"><Image size={18}/></span>}
    <div><strong title={item.name}>{label}</strong><span>{item.status === 'uploading' ? '上传中' : item.status === 'done' ? '已上传' : '上传失败'}</span></div>
    {item.status === 'uploading' && <RefreshCw className="spin" size={15} role="progressbar" aria-label={`${label}正在上传`}/>}
    {item.status === 'error' && <QueryIconButton label={`重试上传${label}`} onClick={() => onRetry(item)}><RotateCcw size={14}/></QueryIconButton>}
    <QueryIconButton label={`删除${label}`} onClick={() => onRemove(item)}><Trash2 size={14}/></QueryIconButton>
    {item.error && <small role="alert">{item.error}</small>}
  </div>;
}

function readComplaintFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('无法读取图片文件'));
    reader.readAsDataURL(file);
  });
}

function ComplaintDialog({
  order, draft, errors, error, phase, uploads, snapshot, dialogRef, onChange, onFiles,
  onRetryUpload, onRemoveUpload, onEditSubmit, onConfirmSubmit, onRetryContext,
  onBack, onClose, onCopyOrder, resultMessage, submissionUnknown, onPreview,
}) {
  const submitting = phase === 'submitting';
  const phaseKnown = COMPLAINT_PHASES.has(phase);
  const title = phase === 'confirm' || submitting ? '确认售后申请' : phase === 'success' ? '售后申请已提交' : '申请售后';
  const uploadBusy = [...uploads.evidence, ...uploads.collect].some(item => item.status === 'uploading');
  return <div className="modal-backdrop order-complaint-backdrop" onMouseDown={event => event.target === event.currentTarget && !submitting && onClose()}>
    <div ref={dialogRef} className="checkout-modal order-complaint-modal" role="dialog" aria-modal="true" aria-labelledby="order-complaint-title" tabIndex={-1}>
      <div className="modal-head order-complaint-head">
        <div><span>AFTER-SALES REQUEST</span><h2 id="order-complaint-title">{title}</h2></div>
        <QueryIconButton label="关闭售后弹窗" onClick={onClose} disabled={submitting}><X size={17}/></QueryIconButton>
      </div>

      <div className="order-complaint-order">
        <div><span>当前订单</span><strong>{order.goods_name}</strong><small>{order.trade_no}</small></div>
        <QueryIconButton label={`复制订单号 ${order.trade_no}`} onClick={() => onCopyOrder(order.trade_no)}><Clipboard size={14}/></QueryIconButton>
      </div>

      {(phase === 'loading' || phase === 'error' || !phaseKnown) && <div className={`order-complaint-loading ${phase === 'error' || !phaseKnown ? 'error' : ''}`} data-complaint-phase-focus tabIndex={-1} role={phase === 'loading' ? 'status' : 'alert'} aria-busy={phase === 'loading'}>
        {phase === 'loading' && <RefreshCw className="spin" size={20}/>} {phase !== 'loading' && <TriangleAlert size={20}/>}<strong>{phase === 'loading' ? '正在读取官方售后状态' : '售后状态暂时无法确认'}</strong><span>{phase === 'loading' ? '确认订单是否仍可申请售后' : '请刷新订单状态后重试'}</span>{error && <small>{error}</small>}{(phase !== 'loading' || error) && <button className="button secondary" type="button" onClick={onRetryContext}><RotateCcw size={15}/>重试</button>}
      </div>}

      {phase === 'edit' && <form className="order-complaint-form" onSubmit={onEditSubmit} noValidate>
        <div className="order-complaint-notice"><ShieldCheck size={17}/><p>官方售后通道已确认，图片会先上传到平台，最终申请将在二次确认后提交。</p></div>
        {error && <div className="order-complaint-error" role="alert"><TriangleAlert size={15}/><span>{error}</span></div>}

        <div className="order-complaint-fields">
          <label className="order-complaint-reason">
            <span>投诉类型</span>
            <select value={draft.reason} onChange={event => onChange('reason', event.target.value)} aria-invalid={Boolean(errors.reason)} aria-describedby={errors.reason ? 'complaint-reason-error' : undefined} data-complaint-form-focus>
              <option value="">请选择投诉类型</option>
              {COMPLAINT_REASON_OPTIONS.map(reason => <option value={reason} key={reason}>{reason}</option>)}
            </select>
            <ComplaintFieldError id="complaint-reason-error" message={errors.reason}/>
          </label>

          <label className="order-complaint-contact">
            <span>通知邮箱</span>
            <input type="email" value={draft.contact} onChange={event => onChange('contact', event.target.value)} placeholder="name@example.com" autoComplete="email" spellCheck="false" aria-invalid={Boolean(errors.contact)} aria-describedby={errors.contact ? 'complaint-contact-error' : undefined}/>
            <ComplaintFieldError id="complaint-contact-error" message={errors.contact}/>
          </label>

          <label className="order-complaint-content">
            <span>补充说明 <em>{draft.content.length} / 200</em></span>
            <textarea value={draft.content} onChange={event => onChange('content', event.target.value)} placeholder="请输入补充说明及凭证" maxLength={200} rows={4} aria-invalid={Boolean(errors.content)} aria-describedby={errors.content ? 'complaint-content-error' : undefined}/>
            <ComplaintFieldError id="complaint-content-error" message={errors.content}/>
          </label>

          <label>
            <span>投诉查询密码</span>
            <input type="password" value={draft.query_pwd} onChange={event => onChange('query_pwd', event.target.value.replace(/[^0-9]/g, '').slice(0, 6))} placeholder="6 位数字" inputMode="numeric" autoComplete="new-password" pattern="[0-9]{6}" maxLength={6} aria-invalid={Boolean(errors.query_pwd)} aria-describedby={errors.query_pwd ? 'complaint-password-error' : undefined}/>
            <ComplaintFieldError id="complaint-password-error" message={errors.query_pwd}/>
          </label>

          <fieldset className="order-complaint-images">
            <legend>图片凭证 <em>选填，最多 3 张</em></legend>
            <ComplaintDropZone label="选择或拖入凭证图" hint="PNG / JPEG / WebP，单张不超过 5 MB" multiple disabled={uploads.evidence.length >= 3} invalid={Boolean(errors.images)} errorId="complaint-images-error" onFiles={files => onFiles('evidence', files)}/>
            {uploads.evidence.length > 0 && <div className="order-complaint-upload-list evidence" role="list" aria-live="polite">{uploads.evidence.map((item, index) => <ComplaintUploadItem item={item} label={`凭证图 ${index + 1}`} onRetry={value => onRetryUpload('evidence', value)} onRemove={value => onRemoveUpload('evidence', value)} onPreview={onPreview} key={item.id}/>)}</div>}
            <ComplaintFieldError id="complaint-images-error" message={errors.images}/>
          </fieldset>

          <fieldset className="order-complaint-collect-image">
            <legend>退款二维码 <em>选填，最多 1 张</em></legend>
            <ComplaintDropZone label={uploads.collect.length ? '替换退款二维码' : '选择或拖入退款二维码'} hint="PNG / JPEG / WebP，单张不超过 5 MB" invalid={Boolean(errors.collect_image)} errorId="complaint-collect-error" onFiles={files => onFiles('collect', files)}/>
            {uploads.collect.length > 0 && <div className="order-complaint-upload-list collect" role="list" aria-live="polite"><ComplaintUploadItem item={uploads.collect[0]} label="退款二维码" onRetry={value => onRetryUpload('collect', value)} onRemove={value => onRemoveUpload('collect', value)} onPreview={onPreview}/></div>}
            <ComplaintFieldError id="complaint-collect-error" message={errors.collect_image}/>
          </fieldset>
        </div>

        <div className="modal-foot order-complaint-foot">
          <span><FileUp size={14}/>{uploadBusy ? '图片正在上传' : '图片经本地服务上传'}</span>
          <div className="modal-foot-actions"><button className="button secondary" type="button" onClick={onClose}>取消</button><button className="button primary" type="submit" disabled={uploadBusy}><ShieldCheck size={15}/><span>核对并继续</span></button></div>
        </div>
      </form>}

      {(phase === 'confirm' || submitting) && <div className="order-complaint-confirm" aria-live="polite">
        <div className="order-complaint-confirm-warning" data-complaint-phase-focus tabIndex={-1}><TriangleAlert size={18}/><div><strong>提交后本订单不能再次申请</strong><span>请确认投诉类型、说明、邮箱和图片无误。</span></div></div>
        {error && <div className="order-complaint-error" role="alert"><TriangleAlert size={15}/><span>{error}</span>{submissionUnknown && <button className="button secondary" type="button" onClick={onRetryContext}><RotateCcw size={14}/>刷新状态</button>}</div>}
        <dl className="order-complaint-confirm-facts"><div><dt>投诉类型</dt><dd>{snapshot.reason}</dd></div><div><dt>通知邮箱</dt><dd>{snapshot.contact}</dd></div><div className="wide"><dt>补充说明</dt><dd>{snapshot.content}</dd></div><div><dt>图片凭证</dt><dd>{snapshot.images.length} 张</dd></div><div><dt>退款二维码</dt><dd>{snapshot.collect_image ? '已上传' : '未上传'}</dd></div></dl>
        {(uploads.evidence.length > 0 || uploads.collect.length > 0) && <div className="order-complaint-confirm-images">{[...uploads.evidence, ...uploads.collect].filter(item => item.status === 'done').map(item => <ComplaintImageButton src={item.previewUrl} alt="待提交图片" label="查看待提交图片" onOpen={onPreview} key={item.id}/>)}</div>}
        <div className="modal-foot order-complaint-foot">
          <span><ShieldCheck size={14}/>将提交到 pay.ldxp.cn</span>
          <div className="modal-foot-actions"><button className="button secondary" type="button" onClick={onBack} disabled={submitting || submissionUnknown}><ChevronLeft size={15}/>返回修改</button><button className="button primary danger" type="button" onClick={onConfirmSubmit} disabled={submitting || submissionUnknown}>{submitting ? <RefreshCw className="spin" size={15}/> : <Send size={15}/>} {submitting ? '正在提交' : '确认提交售后'}</button></div>
        </div>
      </div>}

      {phase === 'success' && <div className="order-complaint-success" aria-live="polite" data-complaint-phase-focus tabIndex={-1}><Check size={24}/><strong>售后申请已提交</strong><span>{resultMessage}</span><button className="button primary" type="button" onClick={onClose}>完成</button></div>}
    </div>
  </div>;
}

function ComplaintHistoryDialog({
  order,
  history,
  phase,
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
  onRetry,
  onRequery,
  onPreview,
}) {
  const complaint = history?.complaint;
  const messages = Array.isArray(complaint?.messages) ? complaint.messages : [];
  const evidence = Array.isArray(complaint?.images) ? complaint.images : [];
  const hasSummary = Boolean(complaint?.reason || complaint?.content || complaint?.contact || evidence.length || complaint?.collect_image);
  const waiting = phase === 'loading' || busy;
  const showPassword = phase === 'password';
  const showError = phase === 'error';
  return <div className="modal-backdrop order-history-backdrop" onMouseDown={event => event.target === event.currentTarget && !waiting && onClose()}>
    <div ref={dialogRef} className="checkout-modal order-history-modal" role="dialog" aria-modal="true" aria-labelledby="order-history-title" tabIndex={-1}>
      <div className="modal-head order-history-head">
        <div><span>AFTER-SALES HISTORY</span><h2 id="order-history-title">售后记录</h2></div>
        <QueryIconButton label="关闭售后记录" onClick={onClose} disabled={waiting}><X size={17}/></QueryIconButton>
      </div>

      <div className="order-history-order">
        <div><span>当前订单</span><strong>{order?.goods_name || '订单'}</strong><small>{order?.trade_no || history?.trade_no || '--'}</small></div>
        {complaint?.status_label && <span className={`order-status ${complaint.status_tone}`}>{complaint.status_label}</span>}
      </div>

      {waiting && <div className="order-history-state" data-history-phase-focus tabIndex={-1} role="status" aria-live="polite" aria-busy="true"><RefreshCw className="spin" size={21}/><strong>正在读取售后记录</strong><span>正在从官方售后通道获取对话和凭证</span></div>}

      {showPassword && <form className="order-history-password" onSubmit={onSubmit}>
        <div className="order-history-state compact" data-history-phase-focus tabIndex={-1}><KeyRound size={21}/><strong>需要订单安全密码</strong><span>该订单的售后记录受安全密码保护</span></div>
        {error && <div className="order-password-error" role="alert"><TriangleAlert size={15}/><span>{error}</span></div>}
        <label className="order-password-label" htmlFor="order-history-password">安全密码</label>
        <div className={`order-password-control ${error ? 'invalid' : ''}`}>
          <KeyRound size={16}/>
          <input id="order-history-password" ref={passwordInputRef} type={passwordVisible ? 'text' : 'password'} value={password} onChange={event => onPassword(event.target.value)} autoComplete="off" maxLength={160} aria-invalid={Boolean(error)} disabled={busy}/>
          <QueryIconButton label={passwordVisible ? '隐藏安全密码' : '显示安全密码'} onClick={onTogglePassword} disabled={busy}>{passwordVisible ? <EyeOff size={15}/> : <Eye size={15}/>}</QueryIconButton>
        </div>
        <div className="order-history-actions"><button className="button secondary" type="button" onClick={onClose}>取消</button><button className="button primary" type="submit" disabled={busy || !password.trim()}><ShieldCheck size={15}/>{busy ? '正在验证' : '验证并查看'}</button></div>
      </form>}

      {showError && <div className="order-history-state error" data-history-phase-focus tabIndex={-1} role="alert">
        <TriangleAlert size={21}/><strong>{sessionExpired ? '查询会话已失效' : error?.includes('接口') ? '售后记录接口不可用' : '售后记录暂时不可用'}</strong><span>{error || '售后记录读取失败，请重试'}</span>
        <div className="order-history-actions">{sessionExpired ? <button className="button primary" type="button" onClick={onRequery}><RefreshCw size={15}/>关闭并重新查询</button> : <button className="button secondary" type="button" onClick={onRetry}><RotateCcw size={15}/>重试</button>}<button className="button secondary" type="button" onClick={onClose}>关闭</button></div>
      </div>}

      {phase === 'ready' && <div className="order-history-scroll">
        {!complaint && <div className="order-history-empty" data-history-phase-focus tabIndex={-1}><MessagesSquare size={24}/><strong>暂无售后记录</strong><span>该订单当前没有可展示的售后对话或凭证</span></div>}
        {complaint && <>
          {hasSummary && <section className="order-history-summary">
            <div className="order-history-section-head"><div><span>REQUEST</span><h3>申请信息</h3></div>{complaint.created_at && <time>{formatOrderDateTime(complaint.created_at)}</time>}</div>
            <dl className="order-history-facts">
              {complaint.reason && <div><dt>投诉类型</dt><dd>{complaint.reason}</dd></div>}
              {complaint.contact && <div><dt>通知邮箱</dt><dd>{complaint.contact}</dd></div>}
              {complaint.content && <div className="wide"><dt>补充说明</dt><dd>{complaint.content}</dd></div>}
            </dl>
            {evidence.length > 0 && <div className="order-history-gallery">
              {evidence.map((src, index) => <ComplaintImageButton src={src} alt={`售后凭证 ${index + 1}`} label={`查看售后凭证 ${index + 1}`} onOpen={onPreview} key={`${src}-${index}`}/>) }
            </div>}
            <div className="order-history-refund-qr">
              <div className="order-history-refund-head"><div><span>REFUND QR</span><strong>买家退款二维码</strong></div><em>{complaint.collect_image ? '点击图片查看' : '未上传'}</em></div>
              {complaint.collect_image
                ? <ComplaintImageButton src={complaint.collect_image} alt="退款二维码" label="查看退款二维码" onOpen={onPreview} className="collect"/>
                : <div className="order-history-no-image"><Image size={18}/><span>暂无退款二维码</span></div>}
            </div>
          </section>}
          <section className="order-history-conversation" aria-live="polite">
            <div className="order-history-section-head"><div><span>CONVERSATION</span><h3>协商记录</h3></div><span className="order-history-count">{messages.length} 条</span></div>
            {messages.length > 0 ? <div className="order-history-messages">{messages.map((message, index) => <article className={`order-history-message ${message.identity || 'unknown'}`} key={`${message.created_at || 'message'}-${index}`}>
              <div className="order-history-message-meta"><strong>{message.identity_label || '协商方'}</strong>{message.created_at && <time>{formatOrderDateTime(message.created_at)}</time>}</div>
              {message.content_type === 'image' ? <ComplaintImageButton src={message.content} alt={`${message.identity_label || '协商方'}发送的图片`} label={`查看${message.identity_label || '协商方'}发送的图片`} onOpen={onPreview} className="message-image"/> : <p>{message.content}</p>}
            </article>)}</div> : <div className="order-history-empty compact"><MessagesSquare size={20}/><span>暂无文字或图片消息</span></div>}
          </section>
        </>}
      </div>}

      {phase === 'ready' && <div className="order-dialog-foot order-history-foot"><span><ShieldCheck size={14}/>记录来自 pay.ldxp.cn</span><button className="button primary" type="button" onClick={onClose}>关闭</button></div>}
    </div>
  </div>;
}

function OrderRow({order, onCopy, onOpenProtectedDetail, onComplaint, onComplaintHistory}) {
  const complaintAction = complaintActionForOrder(order);
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
      {canOpenOrderDetail(order) && <button className="button secondary order-result-link order-detail-button" type="button" onClick={event => onOpenProtectedDetail(order, event.currentTarget)}><ReceiptText size={14}/>{order.goods_action_label}</button>}
      {complaintAction.kind === 'apply' && <button className="button secondary order-complaint-button" type="button" onClick={event => onComplaint(order, event.currentTarget)}><MessageSquareWarning size={14}/>{complaintAction.label}</button>}
      {complaintAction.kind === 'history' && <button className={`button secondary order-complaint-button history ${complaintAction.status.tone}`} type="button" onClick={event => onComplaintHistory(order, event.currentTarget)}><MessagesSquare size={14}/>{complaintAction.label}</button>}
      {complaintAction.kind === 'history' && complaintAction.canReapply && <button className="button secondary order-complaint-button" type="button" onClick={event => onComplaint(order, event.currentTarget)}><MessageSquareWarning size={14}/>{complaintAction.reapplyLabel}</button>}
      {complaintAction.kind === 'status' && <span className={`order-complaint-state ${complaintAction.status.tone}`}>{complaintAction.label}</span>}
    </div></td>
  </tr>;
}

export default function OrderQueryView({request, notify, initialKeywords = '', checkoutProfile = null, onSaveCheckout = null}) {
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
  const [wafVerification, setWafVerification] = useState(null);
  const [wafBusy, setWafBusy] = useState('');
  const [hasQueried, setHasQueried] = useState(false);
  const [queriedAt, setQueriedAt] = useState(null);
  const [detailOrder, setDetailOrder] = useState(null);
  const [orderDetail, setOrderDetail] = useState(null);
  const [detailPassword, setDetailPassword] = useState('');
  const [detailPasswordVisible, setDetailPasswordVisible] = useState(false);
  const [detailRequiresPassword, setDetailRequiresPassword] = useState(false);
  const [detailBusy, setDetailBusy] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [detailSessionExpired, setDetailSessionExpired] = useState(false);
  const [profileContact, setProfileContact] = useState(checkoutProfile?.contact || initialKeywords || '');
  const [profilePassword, setProfilePassword] = useState(checkoutProfile?.query_password || '');
  const [profileStorageMode, setProfileStorageMode] = useState(checkoutProfile?.storage_mode || 'local');
  const [profileBusy, setProfileBusy] = useState(false);
  const [complaintOrder, setComplaintOrder] = useState(null);
  const [complaintDraft, setComplaintDraft] = useState(null);
  const [complaintErrors, setComplaintErrors] = useState({});
  const [complaintError, setComplaintError] = useState('');
  const [complaintPhase, setComplaintPhase] = useState('closed');
  const [complaintContext, setComplaintContext] = useState(null);
  const [complaintUploads, setComplaintUploads] = useState({evidence: [], collect: []});
  const [complaintSnapshot, setComplaintSnapshot] = useState(null);
  const [complaintResultMessage, setComplaintResultMessage] = useState('订单已进入售后待处理状态。');
  const [complaintSubmissionUnknown, setComplaintSubmissionUnknown] = useState(false);
  const [historyOrder, setHistoryOrder] = useState(null);
  const [historyData, setHistoryData] = useState(null);
  const [historyPassword, setHistoryPassword] = useState('');
  const [historyPasswordVisible, setHistoryPasswordVisible] = useState(false);
  const [historyPhase, setHistoryPhase] = useState('closed');
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyError, setHistoryError] = useState('');
  const [historySessionExpired, setHistorySessionExpired] = useState(false);
  const [imagePreview, setImagePreview] = useState(null);
  const requestVersion = useRef(0);
  const detailRequestVersion = useRef(0);
  const detailAbortController = useRef(null);
  const detailDialogRef = useRef(null);
  const detailPasswordInputRef = useRef(null);
  const detailTriggerRef = useRef(null);
  const resultContext = useRef({keywords: '', status: 999, pageSize: 10});
  const initialKeywordsApplied = useRef(false);
  const complaintTrigger = useRef(null);
  const complaintDialog = useRef(null);
  const complaintRequest = useRef(null);
  const complaintUploadControllers = useRef(new Map());
  const complaintSubmitController = useRef(null);
  const complaintGeneration = useRef(0);
  const complaintSubmitLock = useRef(false);
  const complaintUploadsRef = useRef(complaintUploads);
  const historyTrigger = useRef(null);
  const historyDialog = useRef(null);
  const historyPasswordInput = useRef(null);
  const historyRequest = useRef(null);
  const historyGeneration = useRef(0);
  const runSearchRef = useRef(null);
  const wafRequestRef = useRef(null);
  const wafAutoStartRef = useRef('');
  const wafRecoveryAttemptsRef = useRef(0);
  const imagePreviewDialog = useRef(null);
  const imagePreviewTrigger = useRef(null);
  complaintUploadsRef.current = complaintUploads;
  const complaintOpen = Boolean(complaintOrder && complaintDraft);
  const historyOpen = Boolean(historyOrder);

  const revokeComplaintUploadUrls = useCallback((uploads) => {
    [...(uploads?.evidence || []), ...(uploads?.collect || [])].forEach(item => {
      revokeComplaintPreview(item?.previewUrl);
    });
  }, []);

  const closeComplaint = useCallback(() => {
    if (complaintPhase === 'submitting') return;
    complaintGeneration.current += 1;
    complaintRequest.current?.abort();
    complaintRequest.current = null;
    complaintSubmitController.current?.abort();
    complaintSubmitController.current = null;
    complaintUploadControllers.current.forEach(controller => controller.abort());
    complaintUploadControllers.current.clear();
    revokeComplaintUploadUrls(complaintUploadsRef.current);
    setComplaintOrder(null);
    setComplaintDraft(null);
    setComplaintErrors({});
    setComplaintError('');
    setComplaintPhase('closed');
    setComplaintContext(null);
    complaintUploadsRef.current = {evidence: [], collect: []};
    setComplaintUploads({evidence: [], collect: []});
    setComplaintSnapshot(null);
    setComplaintResultMessage('订单已进入售后待处理状态。');
    setComplaintSubmissionUnknown(false);
    complaintSubmitLock.current = false;
  }, [complaintPhase, revokeComplaintUploadUrls]);

  const openImagePreview = useCallback((src, alt = '售后图片', trigger = null) => {
    if (typeof src !== 'string' || !src.trim()) return;
    imagePreviewTrigger.current = trigger || document.activeElement;
    setImagePreview({src: src.trim(), alt: String(alt || '售后图片')});
  }, []);

  const closeImagePreview = useCallback(() => {
    setImagePreview(null);
    const trigger = imagePreviewTrigger.current;
    imagePreviewTrigger.current = null;
    window.requestAnimationFrame(() => {
      if (trigger?.isConnected) trigger.focus();
    });
  }, []);

  const loadComplaintHistory = useCallback(async (order, password = '') => {
    if (!order?.trade_no) return false;
    const generation = historyGeneration.current + 1;
    historyGeneration.current = generation;
    historyRequest.current?.abort();
    const controller = new AbortController();
    historyRequest.current = controller;
    const queryPassword = String(password || '');
    setHistoryBusy(true);
    setHistoryPhase('loading');
    setHistoryError('');
    setHistorySessionExpired(false);
    if (!sessionId || !submittedKeywords) {
      if (generation === historyGeneration.current) {
        setHistoryBusy(false);
        setHistoryPhase('error');
        setHistorySessionExpired(true);
        setHistoryError('Order query session expired. Run a new order search before viewing history.');
      }
      if (historyRequest.current === controller) historyRequest.current = null;
      return false;
    }
    try {
      const response = await request('/order-query/complaints/history', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          keywords: submittedKeywords,
          session_id: sessionId,
          trade_no: order.trade_no,
          query_password: queryPassword,
        }),
        signal: controller.signal,
      });
      if (generation !== historyGeneration.current || controller.signal.aborted) return false;
      const normalized = normalizeComplaintHistoryResponse(response, {trade_no: order.trade_no});
      setHistoryData(normalized);
      if (response && response.session_id) setSessionId(String(response.session_id));
      if (response && response.expires_in !== undefined) setExpiresIn(Math.max(0, Number(response.expires_in) || 0));
      if (normalized.need_query_password && !normalized.complaint && !queryPassword.trim()) {
        setHistoryPhase('password');
        setHistoryPassword('');
        setHistoryPasswordVisible(false);
        setHistoryError('');
        window.requestAnimationFrame(() => historyPasswordInput.current?.focus());
        return false;
      }
      setHistoryPassword('');
      setHistoryPasswordVisible(false);
      setHistoryError('');
      setHistorySessionExpired(false);
      setHistoryPhase('ready');
      return true;
    } catch (requestError) {
      if (controller.signal.aborted || requestError?.name === 'AbortError' || generation !== historyGeneration.current) return false;
      const state = complaintHistoryErrorState(requestError);
      if (state.sessionExpired) {
        setSessionId('');
        setVerification(null);
        setExpiresIn(0);
      }
      if (state.passwordRequired || state.passwordInvalid) {
        setHistoryPhase('password');
        setHistorySessionExpired(false);
        setHistoryError(state.passwordInvalid ? state.message : '');
        if (state.passwordInvalid) setHistoryPassword('');
        window.requestAnimationFrame(() => historyPasswordInput.current?.focus());
      } else {
        setHistoryData(null);
        setHistoryPhase('error');
        setHistorySessionExpired(state.sessionExpired);
        setHistoryError(state.message);
      }
      return false;
    } finally {
      if (historyRequest.current === controller) historyRequest.current = null;
      if (generation === historyGeneration.current) setHistoryBusy(false);
    }
  }, [request, sessionId, submittedKeywords]);

  const closeComplaintHistory = useCallback(() => {
    historyGeneration.current += 1;
    historyRequest.current?.abort();
    historyRequest.current = null;
    setHistoryOrder(null);
    setHistoryData(null);
    setHistoryPassword('');
    setHistoryPasswordVisible(false);
    setHistoryPhase('closed');
    setHistoryBusy(false);
    setHistoryError('');
    setHistorySessionExpired(false);
    setImagePreview(null);
    imagePreviewTrigger.current = null;
  }, []);

  const openComplaintHistory = useCallback((order, trigger) => {
    if (!order || detailOrder || complaintOpen) return;
    historyGeneration.current += 1;
    historyRequest.current?.abort();
    historyRequest.current = null;
    historyTrigger.current = trigger || document.activeElement;
    setHistoryOrder(order);
    setHistoryData(null);
    setHistoryPassword('');
    setHistoryPasswordVisible(false);
    setHistoryError('');
    setHistorySessionExpired(false);
    setHistoryPhase('loading');
    setHistoryBusy(false);
    if (!sessionId || !submittedKeywords) {
      setHistoryPhase('error');
      setHistorySessionExpired(true);
      setHistoryError('Order query session expired. Run a new order search before viewing history.');
      return;
    }
    loadComplaintHistory(order, '');
  }, [complaintOpen, detailOrder, loadComplaintHistory, sessionId, submittedKeywords]);

  const submitComplaintHistoryPassword = useCallback(event => {
    event.preventDefault();
    if (!historyOrder || historyBusy || historyPhase !== 'password' || !historyPassword.trim()) return;
    loadComplaintHistory(historyOrder, historyPassword);
  }, [historyBusy, historyOrder, historyPassword, historyPhase, loadComplaintHistory]);

  const retryComplaintHistory = useCallback(() => {
    if (!historyOrder || historyBusy) return;
    loadComplaintHistory(historyOrder, historyPhase === 'password' ? historyPassword : '');
  }, [historyBusy, historyOrder, historyPassword, historyPhase, loadComplaintHistory]);

  const requeryAfterHistoryExpiry = useCallback(() => {
    const keyword = submittedKeywords;
    const page = result.pagination.page || 1;
    closeComplaintHistory();
    if (keyword) runSearchRef.current?.({keywordValue: keyword, pageValue: page, reuseSession: false});
  }, [closeComplaintHistory, result.pagination.page, submittedKeywords]);

  const closeOrderDetail = useCallback(() => {
    detailRequestVersion.current += 1;
    detailAbortController.current?.abort();
    detailAbortController.current = null;
    setDetailOrder(null);
    setOrderDetail(null);
    setDetailPassword('');
    setDetailPasswordVisible(false);
    setDetailRequiresPassword(false);
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
    complaintGeneration.current += 1;
    complaintRequest.current?.abort();
    complaintUploadControllers.current.forEach(controller => controller.abort());
    complaintUploadControllers.current.clear();
    complaintSubmitController.current?.abort();
    complaintSubmitController.current = null;
    historyGeneration.current += 1;
    historyRequest.current?.abort();
    historyRequest.current = null;
    revokeComplaintUploadUrls(complaintUploadsRef.current);
    imagePreviewTrigger.current = null;
  }, [revokeComplaintUploadUrls]);

  useEffect(() => {
    if (initialKeywordsApplied.current || !initialKeywords) return;
    initialKeywordsApplied.current = true;
    setKeywords(current => current || initialKeywords);
  }, [initialKeywords]);

  useEffect(() => {
    if (!checkoutProfile) return;
    setProfileContact(checkoutProfile.contact || '');
    setProfilePassword(checkoutProfile.query_password || '');
    setProfileStorageMode(checkoutProfile.storage_mode || 'local');
  }, [checkoutProfile]);

  useEffect(() => {
    if (!detailOrder) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const focusFrame = window.requestAnimationFrame(() => {
      if (orderDetail || !detailRequiresPassword) detailDialogRef.current?.focus();
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
  }, [closeOrderDetail, detailOrder, orderDetail, detailRequiresPassword]);

  useEffect(() => {
    if (!complaintOpen) return undefined;
    document.body.classList.add('order-complaint-open');
    const dialog = complaintDialog.current;
    const focusFrame = window.requestAnimationFrame(() => {
      const selector = complaintPhase === 'edit' ? '[data-complaint-form-focus]' : '[data-complaint-phase-focus]';
      dialog?.querySelector(selector)?.focus();
    });
    const handleDialogKey = event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        if (complaintPhase !== 'submitting') closeComplaint();
        return;
      }
      if (event.key !== 'Tab' || !dialog) return;
      const focusable = Array.from(dialog.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'));
      if (!focusable.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const focusIsOutside = !dialog.contains(document.activeElement);
      const focusIsNotTabbable = !focusable.includes(document.activeElement);
      if ((event.shiftKey && (document.activeElement === first || focusIsOutside || focusIsNotTabbable)) || (!event.shiftKey && (document.activeElement === last || focusIsOutside || focusIsNotTabbable))) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      }
    };
    window.addEventListener('keydown', handleDialogKey);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.body.classList.remove('order-complaint-open');
      window.removeEventListener('keydown', handleDialogKey);
    };
  }, [complaintOpen, complaintPhase, closeComplaint]);

  useEffect(() => {
    if (complaintOpen || !complaintTrigger.current) return;
    const trigger = complaintTrigger.current;
    complaintTrigger.current = null;
    window.requestAnimationFrame(() => {
      if (trigger?.isConnected) trigger.focus();
    });
  }, [complaintOpen]);

  useEffect(() => {
    if (!historyOpen) return undefined;
    document.body.classList.add('order-history-open');
    const dialog = historyDialog.current;
    const focusFrame = window.requestAnimationFrame(() => {
      if (historyPhase === 'password') historyPasswordInput.current?.focus();
      else dialog?.querySelector('[data-history-phase-focus]')?.focus();
    });
    const handleDialogKey = event => {
      if (imagePreview) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        if (!historyBusy) closeComplaintHistory();
        return;
      }
      if (event.key !== 'Tab' || !dialog) return;
      const focusable = Array.from(dialog.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'));
      if (!focusable.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const outside = !dialog.contains(document.activeElement);
      const notTabbable = !focusable.includes(document.activeElement);
      if ((event.shiftKey && (document.activeElement === first || outside || notTabbable)) || (!event.shiftKey && (document.activeElement === last || outside || notTabbable))) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      }
    };
    window.addEventListener('keydown', handleDialogKey);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.body.classList.remove('order-history-open');
      window.removeEventListener('keydown', handleDialogKey);
    };
  }, [closeComplaintHistory, historyBusy, historyOpen, historyPhase, imagePreview]);

  useEffect(() => {
    if (historyOpen || !historyTrigger.current) return;
    const trigger = historyTrigger.current;
    historyTrigger.current = null;
    window.requestAnimationFrame(() => {
      if (trigger?.isConnected) trigger.focus();
    });
  }, [historyOpen]);

  useEffect(() => {
    if (!imagePreview) return undefined;
    const dialog = imagePreviewDialog.current;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const focusFrame = window.requestAnimationFrame(() => dialog?.focus());
    const handleLightboxKey = event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeImagePreview();
        return;
      }
      if (event.key !== 'Tab' || !dialog) return;
      const focusable = Array.from(dialog.querySelectorAll('button:not([disabled]), [tabindex]:not([tabindex="-1"])'));
      if (!focusable.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', handleLightboxKey);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', handleLightboxKey);
    };
  }, [closeImagePreview, imagePreview]);

  const summary = useMemo(() => summarizeOrders(result.orders, result.pagination.total), [result]);
  const manualRequired = verification?.status === 'manual_required';

  const commitSearchResponse = (response, {keywords: normalizedKeywords, status, page, pageSize, quiet = false} = {}) => {
    const normalized = normalizeOrderResponse(response, {page, pageSize});
    setSessionId(String(response.session_id || ''));
    setExpiresIn(Number(response.expires_in || 0));
    setVerification(response.verification || null);
    setSubmittedKeywords(normalizedKeywords);
    setResult(normalized);
    resultContext.current = {keywords: normalizedKeywords, status: Number(status), pageSize: Number(pageSize)};
    setCaptcha(null);
    setCaptchaCode('');
    setHasQueried(true);
    setQueriedAt(new Date());
    setWafVerification(null);
    wafAutoStartRef.current = '';
    wafRecoveryAttemptsRef.current = 0;
    if (!quiet) notify({type: 'success', title: '订单查询完成', message: `已获取 ${normalized.pagination.total} 笔订单`, duration: 4200});
    return normalized;
  };

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
    autoRecovery = false,
  } = {}) => {
    const normalizedKeywords = keywordValue.trim();
    if (!normalizedKeywords) {
      notify('请输入预留联系方式或订单号', 'error');
      return false;
    }
    if (!autoRecovery) {
      wafRecoveryAttemptsRef.current = 0;
      wafAutoStartRef.current = '';
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
      setWafVerification(null);
      wafRequestRef.current = null;
      setQueriedAt(null);
    }
    wafRequestRef.current = {
      keywords: normalizedKeywords,
      status: Number(statusValue),
      page: Number(pageValue),
      page_size: Number(pageSizeValue),
    };
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

      const normalized = commitSearchResponse(response, {keywords: normalizedKeywords, status: statusValue, page: pageValue, pageSize: pageSizeValue, quiet});
      return true;
    } catch (requestError) {
      if (version !== requestVersion.current) return false;
      clearStaleResult();
      if (requestError.code === 'waf_verification_required' || requestError.payload?.code === 'waf_verification_required') {
        setError('');
        // Preserve the session created before the upstream WAF response so
        // the browser-verification button has a usable context.
        const wafPayload = requestError.payload || {};
        const responseSessionId = String(wafPayload.session_id || '');
        const responseRequest = wafPayload.waf_request;
        if (responseSessionId) setSessionId(responseSessionId);
        if (Number.isFinite(Number(wafPayload.expires_in))) setExpiresIn(Number(wafPayload.expires_in));
        if (responseSessionId && responseRequest && typeof responseRequest === 'object') {
          wafRequestRef.current = {
            keywords: normalizedKeywords,
            status: Number(responseRequest.status ?? statusValue),
            page: Number(responseRequest.page ?? pageValue),
            page_size: Number(responseRequest.page_size ?? pageSizeValue),
          };
        }
        setSubmittedKeywords(normalizedKeywords);
        setWafVerification({status: 'required', detail: requestError.message || '订单接口触发阿里云 WAF 验证'});
        notify({type: 'warning', title: '需要完成浏览器验证', message: '订单接口被阿里云 WAF 拦截，正在启动独立浏览器验证', duration: 7000});
        return false;
      }
      setError(requestError.message || '订单查询失败');
      notify(requestError.message || '订单查询失败', 'error');
      return false;
    } finally {
      if (version === requestVersion.current) setBusy('');
    }
  };

  runSearchRef.current = runSearch;

  const runOrderWafVerification = async phase => {
    const requestSpec = wafRequestRef.current;
    if (!requestSpec || !sessionId || !submittedKeywords || wafBusy || busy) {
      if (!wafBusy && !busy && (!requestSpec || !sessionId || !submittedKeywords)) {
        notify('楠岃瘉浼氳瘽宸插け鏁堬紝璇峰厛閲嶆柊鏌ヨ', 'error');
      }
      return false;
    }
    setWafBusy(phase);
    try {
      const response = await request(`/order-query/waf-verification/${phase}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...requestSpec, session_id: sessionId, keywords: submittedKeywords}),
      });
      if (response.status === 'awaiting_verification') {
        if (response.session_id) setSessionId(String(response.session_id));
        if (Number.isFinite(Number(response.expires_in))) setExpiresIn(Number(response.expires_in));
        setWafVerification({status: 'awaiting_verification', detail: response.detail || '请在 Edge 窗口完成滑块验证'});
        notify({type: 'info', title: '独立浏览器验证已打开', message: '完成滑块后页面会自动继续查询', duration: 6000});
        return false;
      }
      commitSearchResponse(response, {
        keywords: requestSpec.keywords,
        status: requestSpec.status,
        page: requestSpec.page,
        pageSize: requestSpec.page_size,
      });
      return true;
    } catch (requestError) {
      if (requestError.status === 410) {
        if (wafRecoveryAttemptsRef.current < 1) {
          wafRecoveryAttemptsRef.current += 1;
          setSessionId('');
          setVerification(null);
          setExpiresIn(0);
          setWafVerification(null);
          notify({type: 'info', title: '查询会话已刷新', message: '验证等待时间较长，正在创建新的查询会话', duration: 5000});
          window.setTimeout(() => runSearchRef.current?.({
            keywordValue: requestSpec.keywords,
            statusValue: requestSpec.status,
            pageValue: requestSpec.page,
            pageSizeValue: requestSpec.page_size,
            reuseSession: false,
            autoRecovery: true,
          }), 0);
          return false;
        }
        setWafVerification({status: 'required', detail: '订单查询会话已过期，请重新点击查询'});
        notify('订单查询会话已过期，请重新点击查询', 'error');
        return false;
      } else {
        setWafVerification({status: 'required', detail: requestError.message || '浏览器验证未完成，请重试'});
      }
      notify(requestError.message || '浏览器验证未完成', 'error');
      return false;
    } finally {
      setWafBusy('');
    }
  };

  // Start the isolated browser as soon as the upstream WAF response arrives.
  useEffect(() => {
    if (wafVerification?.status !== 'required' || busy || wafBusy || !sessionId || !submittedKeywords) return;
    const requestSpec = wafRequestRef.current;
    if (!requestSpec) return;
    const key = `${sessionId}:${requestSpec.keywords}:${requestSpec.status}:${requestSpec.page}`;
    if (wafAutoStartRef.current === key) return;
    wafAutoStartRef.current = key;
    runOrderWafVerification('start');
  }, [busy, sessionId, submittedKeywords, wafBusy, wafVerification]);

  // Poll only the local browser page. The protected order API is requested
  // once, after the browser reports that the challenge document is gone.
  useEffect(() => {
    if (wafVerification?.status !== 'awaiting_verification') return undefined;
    const requestSpec = wafRequestRef.current;
    if (!requestSpec || !sessionId || !submittedKeywords) return undefined;
    let cancelled = false;
    let attempts = 0;
    let timer = null;
    const poll = async () => {
      if (cancelled) return;
      attempts += 1;
      try {
        const response = await request('/order-query/waf-verification/status', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({...requestSpec, session_id: sessionId, keywords: submittedKeywords}),
        });
        if (cancelled) return;
        if (response.ready) {
          await runOrderWafVerification('complete');
          return;
        }
        if (attempts >= 90) {
          setWafVerification({status: 'required', detail: '浏览器验证等待超时，请完成滑块后重试'});
          notify('浏览器验证等待超时，请重试', 'error');
          return;
        }
      } catch (requestError) {
        if (cancelled) return;
        if (requestError.status === 410) {
          if (wafRecoveryAttemptsRef.current < 1) {
            wafRecoveryAttemptsRef.current += 1;
            setSessionId('');
            setVerification(null);
            setExpiresIn(0);
            setWafVerification(null);
            notify({type: 'info', title: '查询会话已刷新', message: '正在重新创建查询会话', duration: 5000});
            window.setTimeout(() => runSearchRef.current?.({
              keywordValue: requestSpec.keywords,
              statusValue: requestSpec.status,
              pageValue: requestSpec.page,
              pageSizeValue: requestSpec.page_size,
              reuseSession: false,
              autoRecovery: true,
            }), 0);
          } else {
            setWafVerification({status: 'required', detail: '订单查询会话已过期，请重新点击查询'});
            notify('订单查询会话已过期，请重新点击查询', 'error');
          }
          return;
        }
        setWafVerification({status: 'required', detail: requestError.message || '浏览器验证窗口已关闭，请重试'});
        notify(requestError.message || '浏览器验证窗口已关闭，请重试', 'error');
        return;
      }
      timer = window.setTimeout(poll, 3500);
    };
    timer = window.setTimeout(poll, 1000);
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [sessionId, submittedKeywords, wafVerification]);

  const submitSearch = event => {
    event.preventDefault();
    runSearch({keywordValue: keywords, pageValue: 1, reuseSession: !manualRequired});
  };

  const saveProfile = async event => {
    event.preventDefault();
    if (!onSaveCheckout) return;
    setProfileBusy(true);
    try {
      await onSaveCheckout({contact: profileContact, query_password: profilePassword, storage_mode: profileStorageMode});
    } catch {
      // Parent displays the persistence error.
    } finally {
      setProfileBusy(false);
    }
  };

  const submitManualCaptcha = event => {
    event.preventDefault();
    runSearch({manualCode: captchaCode.trim(), pageValue: 1});
  };

  const openProtectedOrderDetail = (order, trigger) => {
    if (complaintOpen) return;
    const sessionAvailable = Boolean(sessionId && submittedKeywords);
    const passwordRequired = canOpenProtectedOrderDetail(order);
    detailRequestVersion.current += 1;
    detailAbortController.current?.abort();
    detailAbortController.current = null;
    detailTriggerRef.current = trigger || document.activeElement;
    setDetailOrder(order);
    setOrderDetail(null);
    setDetailRequiresPassword(passwordRequired);
    setDetailPassword(checkoutProfile?.query_password || '');
    setDetailPasswordVisible(false);
    setDetailBusy(false);
    setDetailError(sessionAvailable ? '' : '订单查询会话已失效，请重新查询订单后再验证');
    setDetailSessionExpired(!sessionAvailable);
  };

  const submitOrderDetailPassword = async event => {
    event?.preventDefault();
    if (!detailOrder || detailBusy || detailSessionExpired || (detailRequiresPassword && !detailPassword.length)) return;
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
      if (!errorState.sessionExpired) window.requestAnimationFrame(() => {
        if (detailRequiresPassword) detailPasswordInputRef.current?.focus();
        else detailDialogRef.current?.focus();
      });
    } finally {
      if (detailAbortController.current === controller) detailAbortController.current = null;
      if (version === detailRequestVersion.current) setDetailBusy(false);
    }
  };

  useEffect(() => {
    if (!detailOrder || detailRequiresPassword || orderDetail || detailBusy || detailSessionExpired || detailError || !sessionId || !submittedKeywords) return;
    submitOrderDetailPassword();
  }, [detailOrder, detailRequiresPassword, orderDetail, detailBusy, detailSessionExpired, detailError, sessionId, submittedKeywords]);

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

  const complaintIdentity = useCallback((tradeNo = complaintOrder?.trade_no) => ({
    keywords: submittedKeywords,
    session_id: sessionId,
    trade_no: tradeNo,
  }), [complaintOrder?.trade_no, sessionId, submittedKeywords]);

  const setUploadItem = (kind, id, patch) => {
    const predicted = complaintUploadsRef.current;
    complaintUploadsRef.current = {
      ...predicted,
      [kind]: (predicted[kind] || []).map(item => item.id === id ? {...item, ...patch} : item),
    };
    setComplaintUploads(current => {
      const items = current[kind] || [];
      if (!items.some(item => item.id === id)) return current;
      const next = {...current, [kind]: items.map(item => item.id === id ? {...item, ...patch} : item)};
      complaintUploadsRef.current = next;
      return next;
    });
  };

  const isCurrentComplaintUpload = (kind, item) => (complaintUploadsRef.current[kind] || [])
    .some(value => value.id === item?.id && value.file === item?.file);

  const uploadComplaintItem = async (kind, item, generation = complaintGeneration.current) => {
    if (!item || generation !== complaintGeneration.current || !isCurrentComplaintUpload(kind, item)) return;
    const controller = new AbortController();
    complaintUploadControllers.current.get(item.id)?.abort();
    complaintUploadControllers.current.set(item.id, controller);
    setUploadItem(kind, item.id, {status: 'uploading', error: ''});
    try {
      const dataUrl = await readComplaintFile(item.file);
      if (generation !== complaintGeneration.current || controller.signal.aborted || !isCurrentComplaintUpload(kind, item)) return;
      const response = await request('/order-query/complaints/upload', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...complaintIdentity(), name: item.name, mime_type: item.mimeType, data_url: dataUrl}),
        signal: controller.signal,
      });
      if (generation !== complaintGeneration.current || controller.signal.aborted || !isCurrentComplaintUpload(kind, item)) return;
      if (!response?.url) throw new Error('上传响应缺少图片地址');
      setUploadItem(kind, item.id, {status: 'done', url: String(response.url), error: ''});
      setComplaintError('');
      setComplaintDraft(current => {
        if (!current) return current;
        if (kind === 'evidence') {
          const nextImages = [...current.images].filter(Boolean);
          nextImages.push(String(response.url));
          return {...current, images: nextImages.slice(0, 3)};
        }
        return {...current, collect_image: String(response.url)};
      });
      setComplaintErrors(current => {
        const next = {...current};
        delete next[kind === 'evidence' ? 'images' : 'collect_image'];
        return next;
      });
    } catch (requestError) {
      if (controller.signal.aborted || requestError?.name === 'AbortError' || generation !== complaintGeneration.current || !isCurrentComplaintUpload(kind, item)) return;
      setUploadItem(kind, item.id, {status: 'error', error: requestError.message || '图片上传失败'});
      setComplaintError(requestError.message || '图片上传失败');
    } finally {
      if (complaintUploadControllers.current.get(item.id) === controller) complaintUploadControllers.current.delete(item.id);
    }
  };

  const removeComplaintUpload = (kind, item) => {
    if (!item || !isCurrentComplaintUpload(kind, item)) return;
    complaintUploadControllers.current.get(item.id)?.abort();
    complaintUploadControllers.current.delete(item.id);
    revokeComplaintPreview(item.previewUrl);
    const currentUploads = complaintUploadsRef.current;
    const nextUploads = {
      ...currentUploads,
      [kind]: (currentUploads[kind] || []).filter(value => value.id !== item.id),
    };
    complaintUploadsRef.current = nextUploads;
    setComplaintUploads(current => {
      const next = {...current, [kind]: (current[kind] || []).filter(value => value.id !== item.id)};
      complaintUploadsRef.current = next;
      return next;
    });
    setComplaintDraft(current => {
      if (!current) return current;
      if (kind === 'evidence') return {...current, images: current.images.filter(value => value !== item.url)};
      return {...current, collect_image: ''};
    });
    if (item.url && sessionId && submittedKeywords) {
      request('/order-query/complaints/upload/remove', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...complaintIdentity(), url: item.url}),
      }).catch(() => {});
    }
    if (!nextUploads[kind].some(value => value.status === 'error')) {
      setComplaintErrors(current => {
        const next = {...current};
        delete next[kind === 'evidence' ? 'images' : 'collect_image'];
        return next;
      });
    }
    setComplaintError('');
  };

  const onComplaintFiles = (kind, files) => {
    if (!complaintDraft || complaintPhase !== 'edit') return;
    const currentUploads = complaintUploadsRef.current;
    const currentCount = kind === 'evidence' ? (currentUploads.evidence || []).length : 0;
    const selection = selectComplaintImageFiles(files, {currentCount, limit: kind === 'evidence' ? 3 : 1});
    if (selection.errors.length) {
      const message = selection.errors.join('；');
      const field = kind === 'evidence' ? 'images' : 'collect_image';
      setComplaintErrors(current => ({...current, [field]: message}));
      setComplaintError(message);
    }
    if (!selection.accepted.length) return;
    const generation = complaintGeneration.current;
    if (kind === 'collect' && currentUploads.collect?.[0]) removeComplaintUpload('collect', currentUploads.collect[0]);
    const items = selection.accepted.map(file => ({
      id: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`,
      file,
      name: String(file.name || 'image'),
      mimeType: String(file.type || '').toLowerCase(),
      previewUrl: complaintPreviewUrl(file),
      status: 'queued',
      url: '',
      error: '',
    }));
    const latestUploads = complaintUploadsRef.current;
    const nextUploads = {
      ...latestUploads,
      [kind]: kind === 'collect' ? items.slice(0, 1) : [...(latestUploads.evidence || []), ...items].slice(0, 3),
    };
    complaintUploadsRef.current = nextUploads;
    setComplaintUploads(nextUploads);
    if (kind === 'collect') setComplaintDraft(current => current ? {...current, collect_image: ''} : current);
    items.forEach(item => uploadComplaintItem(kind, item, generation));
  };

  const openComplaint = (order, trigger) => {
    if (detailOrder) return;
    complaintGeneration.current += 1;
    const generation = complaintGeneration.current;
    complaintRequest.current?.abort();
    complaintRequest.current = null;
    complaintSubmitController.current?.abort();
    complaintSubmitController.current = null;
    complaintUploadControllers.current.forEach(controller => controller.abort());
    complaintUploadControllers.current.clear();
    revokeComplaintUploadUrls(complaintUploadsRef.current);
    complaintSubmitLock.current = false;
    const contact = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(submittedKeywords.trim()) ? submittedKeywords.trim() : '';
    complaintTrigger.current = trigger;
    setComplaintOrder(order);
    setComplaintDraft(createComplaintDraft(order, contact));
    setComplaintErrors({});
    setComplaintError('');
    setComplaintPhase('loading');
    setComplaintContext(null);
    complaintUploadsRef.current = {evidence: [], collect: []};
    setComplaintUploads({evidence: [], collect: []});
    setComplaintSnapshot(null);
    setComplaintResultMessage('订单已进入售后待处理状态。');
    setComplaintSubmissionUnknown(false);
    if (!sessionId || !submittedKeywords) {
      setComplaintPhase('error');
      setComplaintError('订单查询会话已失效，请重新查询后再申请售后');
      return;
    }
    const controller = new AbortController();
    complaintRequest.current = controller;
    request('/order-query/complaints/context', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({keywords: submittedKeywords, session_id: sessionId, trade_no: order.trade_no}),
      signal: controller.signal,
    }).then(response => {
      if (generation !== complaintGeneration.current || controller.signal.aborted) return;
      if (!response || typeof response !== 'object') throw new Error('售后配置响应格式无效');
      const rawStatus = response.complaint_status;
      const status = (typeof rawStatus === 'number' && Number.isInteger(rawStatus))
        || (typeof rawStatus === 'string' && /^-?\d+$/.test(rawStatus.trim()))
        ? Number(rawStatus)
        : NaN;
      const rawCanComplaint = response.can_complaint;
      const canComplaint = rawCanComplaint === true || rawCanComplaint === 1 || rawCanComplaint === '1';
      // A sentinel -1 is actionable only when the upstream explicitly
      // confirms that the complaint status is known.
      const complaintStatusKnown = response.complaint_status_known === true;
      if (!Number.isInteger(status) || ![-1, 0, 1].includes(status) || !complaintStatusKnown) {
        setComplaintPhase('error');
        setComplaintError('官方售后状态无法确认，请重试');
        return;
      }
      setSessionId(String(response.session_id || sessionId));
      setExpiresIn(Number(response.expires_in || expiresIn));
      const context = {
        ...response,
        can_complaint: canComplaint,
        complaint_status: status,
        complaint_status_known: complaintStatusKnown,
      };
      setComplaintContext(context);
      if (!canComplaint || status !== -1) {
        setComplaintPhase('error');
        setComplaintError(status !== -1 ? '该订单已有售后记录，不能重复申请' : '当前订单已超过售后期限');
        return;
      }
      setComplaintPhase('edit');
    }).catch(requestError => {
      if (controller.signal.aborted || requestError?.name === 'AbortError' || generation !== complaintGeneration.current) return;
      setComplaintPhase('error');
      setComplaintError(requestError.message || '读取官方售后配置失败');
    }).finally(() => {
      if (complaintRequest.current === controller) complaintRequest.current = null;
    });
  };

  const retryComplaintContext = () => {
    if (!complaintOrder) return;
    const order = complaintOrder;
    const trigger = complaintTrigger.current;
    openComplaint(order, trigger);
  };

  const updateComplaintField = (field, value) => {
    setComplaintDraft(current => current ? {...current, [field]: value} : current);
    setComplaintErrors(current => {
      if (!current[field]) return current;
      const next = {...current};
      delete next[field];
      return next;
    });
    setComplaintError('');
  };

  const submitComplaintEdit = event => {
    event.preventDefault();
    if (!complaintDraft || !complaintContext || complaintPhase !== 'edit') return;
    const evidencePending = complaintUploads.evidence.some(item => item.status !== 'done');
    const collectPending = complaintUploads.collect.some(item => item.status !== 'done');
    const nextDraft = {
      ...complaintDraft,
      images: complaintUploads.evidence.filter(item => item.status === 'done' && item.url).map(item => item.url),
      collect_image: complaintUploads.collect.find(item => item.status === 'done' && item.url)?.url || '',
    };
    const validation = validateComplaintPayload(nextDraft);
    const errors = {...validation.errors};
    if (evidencePending) errors.images = '请等待凭证图上传完成，失败图片可重试';
    if (collectPending) errors.collect_image = '请等待退款二维码上传完成，失败图片可重试';
    setComplaintErrors(errors);
    setComplaintError('');
    if (Object.keys(errors).length) {
      setComplaintError('请检查标记字段后再继续');
      window.requestAnimationFrame(() => document.querySelector('.order-complaint-modal [aria-invalid="true"]')?.focus());
      return;
    }
    setComplaintDraft(nextDraft);
    setComplaintSnapshot(validation.payload);
    setComplaintSubmissionUnknown(false);
    setComplaintPhase('confirm');
  };

  const confirmComplaintSubmit = async () => {
    if (!complaintSnapshot || !complaintOrder || complaintPhase !== 'confirm' || complaintSubmitLock.current || complaintSubmissionUnknown || !sessionId || !submittedKeywords) return;
    complaintSubmitLock.current = true;
    const generation = complaintGeneration.current;
    const controller = new AbortController();
    complaintSubmitController.current = controller;
    setComplaintError('');
    setComplaintPhase('submitting');
    try {
      const response = await request('/order-query/complaints/submit', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...complaintSnapshot, ...complaintIdentity()}),
        signal: controller.signal,
      });
      if (response?.submitted !== true) {
        setComplaintError('提交结果暂时无法确认，请刷新订单状态后再决定是否重试');
        setComplaintSubmissionUnknown(true);
        setComplaintPhase('confirm');
        return;
      }
      setComplaintResultMessage(response.message || '订单已进入售后待处理状态。');
      setComplaintPhase('success');
      setResult(current => ({...current, orders: current.orders.map(order => order.trade_no === complaintOrder.trade_no ? {...order, complaint_status: 0, can_complaint: false} : order)}));
      notify({type: 'success', title: '售后申请已提交', message: response.message || '订单已进入售后待处理状态', duration: 5200});
    } catch (requestError) {
      if (controller.signal.aborted || requestError?.name === 'AbortError') {
        if (generation === complaintGeneration.current) {
          complaintSubmitLock.current = false;
          setComplaintPhase('confirm');
        }
        return;
      }
      if (!requestError?.code || ['order_complaint_submission_unknown', 'order_complaint_submission_conflict'].includes(requestError.code)) {
        setComplaintError(requestError?.code === 'order_complaint_submission_conflict'
          ? '该订单可能已经提交售后，请刷新订单状态确认'
          : '提交结果暂时无法确认，请刷新订单状态后再决定是否重试');
        setComplaintSubmissionUnknown(true);
        setComplaintPhase('confirm');
        return;
      }
      setComplaintError(requestError.message || '售后提交失败，可检查后重试');
      setComplaintPhase('confirm');
      complaintSubmitLock.current = false;
    } finally {
      if (complaintSubmitController.current === controller) complaintSubmitController.current = null;
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
        <div className="order-section-heading"><div><span>LDXP ORDER LOOKUP</span><h2>链动小铺订单查询</h2><p>使用购买时预留的联系方式或订单号查询</p></div><span className="order-query-badge"><ShieldCheck size={14}/>安全查询</span></div>
        <form className="order-search-form" onSubmit={submitSearch}>
          <label><span>联系方式 / 订单号</span><div><Search size={16}/><input value={keywords} onChange={event => setKeywords(event.target.value)} placeholder="邮箱、手机号、QQ 或订单号" autoComplete="off" spellCheck="false"/></div></label>
          <button className="button primary" type="submit" disabled={Boolean(busy) || !keywords.trim()}><ShieldCheck size={16}/>{busy === 'search' ? '正在查询' : '自动验证并查询'}</button>
        </form>
        <form className="order-profile-form" onSubmit={saveProfile}>
          <div><span>购买配置</span><input value={profileContact} onChange={event => setProfileContact(event.target.value)} placeholder="联系方式" autoComplete="email"/><input type="password" value={profilePassword} onChange={event => setProfilePassword(event.target.value)} placeholder="安全密码（可选）" autoComplete="off"/><select value={profileStorageMode} onChange={event => setProfileStorageMode(event.target.value)}><option value="local">本机数据库</option><option value="browser">浏览器缓存</option></select><button className="button secondary" type="submit" disabled={profileBusy || !profileContact.trim()}><Save size={14}/>{profileBusy ? '正在保存' : '保存配置'}</button></div>
          <small>保存后可在商品购买页面直接复用</small>
        </form>
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
        waf={wafVerification}
        wafBusy={wafBusy}
        onStartWaf={() => runOrderWafVerification('start')}
        onCompleteWaf={() => runOrderWafVerification('complete')}
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
          <tbody>{result.orders.length ? result.orders.map((order, index) => <OrderRow order={order} onCopy={copyOrderNumber} onOpenProtectedDetail={openProtectedOrderDetail} onComplaint={openComplaint} onComplaintHistory={openComplaintHistory} key={order.trade_no || `${order.goods_key}-${index}`}/>) : <tr className="order-empty-row"><td colSpan="6"><div className="order-empty-state">{emptyMessage.icon}<strong>{emptyMessage.title}</strong><span>{emptyMessage.detail}</span></div></td></tr>}</tbody>
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
      passwordRequired={detailRequiresPassword}
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
    {complaintOpen && <ComplaintDialog
      order={complaintOrder}
      draft={complaintDraft}
      errors={complaintErrors}
      error={complaintError}
      phase={complaintPhase}
      uploads={complaintUploads}
      snapshot={complaintSnapshot || complaintDraft}
      resultMessage={complaintResultMessage}
      submissionUnknown={complaintSubmissionUnknown}
      dialogRef={complaintDialog}
      onChange={updateComplaintField}
      onFiles={onComplaintFiles}
      onRetryUpload={uploadComplaintItem}
      onRemoveUpload={removeComplaintUpload}
      onEditSubmit={submitComplaintEdit}
      onConfirmSubmit={confirmComplaintSubmit}
      onRetryContext={retryComplaintContext}
      onBack={() => { setComplaintPhase('edit'); setComplaintError(''); }}
       onClose={closeComplaint}
       onCopyOrder={copyOrderNumber}
       onPreview={openImagePreview}
     />}
    {historyOpen && <ComplaintHistoryDialog
      order={historyOrder}
      history={historyData}
      phase={historyPhase}
      password={historyPassword}
      passwordVisible={historyPasswordVisible}
      busy={historyBusy}
      error={historyError}
      sessionExpired={historySessionExpired}
      dialogRef={historyDialog}
      passwordInputRef={historyPasswordInput}
      onPassword={value => {
        setHistoryPassword(value);
        if (historyError) setHistoryError('');
      }}
      onTogglePassword={() => setHistoryPasswordVisible(current => !current)}
      onSubmit={submitComplaintHistoryPassword}
      onClose={closeComplaintHistory}
      onRetry={retryComplaintHistory}
      onRequery={requeryAfterHistoryExpiry}
      onPreview={openImagePreview}
    />}
    {imagePreview && <div className="modal-backdrop order-image-preview-backdrop" onMouseDown={event => event.target === event.currentTarget && closeImagePreview()}>
      <div ref={imagePreviewDialog} className="order-image-preview-dialog" role="dialog" aria-modal="true" aria-label="图片预览" tabIndex={-1}>
        <QueryIconButton label="关闭图片预览" onClick={closeImagePreview}><X size={17}/></QueryIconButton>
        <img src={imagePreview.src} alt={imagePreview.alt}/>
      </div>
    </div>}
   </section>;
}
