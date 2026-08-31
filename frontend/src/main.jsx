import React, {useEffect, useMemo, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {
  Activity,
  AlertCircle,
  ArrowUpRight,
  BadgePercent,
  BellRing,
  BarChart3,
  CalendarDays,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleDollarSign,
  Clipboard,
  Clock3,
  Database,
  Eye,
  EyeOff,
  Filter,
  History,
  Link2,
  ListChecks,
  LoaderCircle,
  LogIn,
  LogOut,
  Moon,
  Minus,
  Package,
  PanelRight,
  Plus,
  ReceiptText,
  RefreshCw,
  Save,
  Search,
  SlidersHorizontal,
  Settings2,
  KeyRound,
  Sun,
  Tag,
  TimerReset,
  TriangleAlert,
  Upload,
  UserCircle,
  Wrench,
  ShieldCheck,
  ShoppingBag,
  Store,
  Trash2,
  X,
  Zap,
} from 'lucide-react';
import './style.css';
import {buildSub2ApiAutomationSaveNotice, buildSub2ApiImportNotice, sub2ApiHistoryDeleteErrorMessage} from './sub2apiNotices.js';
import {
  CARD_RECLAIM_POLL_TIMEOUT_MS,
  CARD_RECLAIM_POLL_TIMEOUT_SECONDS,
  pollForReclaimDownloads,
  reclaimPollingFailureMessage,
} from './sub2apiCardPolling.js';
import {
  normalizeSub2ApiRecoveryResult,
  recoveryResultForProgress,
} from './sub2apiRecoveryModel.js';
import {InstallWizard, LoginView} from './AuthGate.jsx';
import SettingsView from './SettingsView.jsx';
import {
  AUTH_MODES,
  canManageWorkspace,
  canAccessView,
  defaultViewFor,
  isAdmin,
  normalizeInstallStatus,
  normalizeMode,
  normalizeSession,
  requiresLogin,
  roleLabel,
} from './authModel.js';

const API = '/api';
const CHECKOUT_PROFILE_KEY = 'ldxp-checkout-profile-v1';
const AUTH_TOKEN_KEY = 'ldxp-auth-token-v1';
let apiAuthToken = '';

function setApiAuthToken(value) {
  apiAuthToken = String(value || '').trim();
}
const PriceHistoryChart = React.lazy(() => import('./PriceHistoryChart.jsx'));
const ProductDetailPriceChart = React.lazy(() => import('./PriceHistoryChart.jsx').then(module => ({default: module.ProductDetailPriceChart})));
const loadOrderQueryView = () => import('./OrderQueryView.jsx');
const loadReclaimView = () => import('./ReclaimView.jsx');
const loadSub2ApiView = () => import('./Sub2ApiView.jsx');
const OrderQueryView = React.lazy(() => loadOrderQueryView());
const ReclaimView = React.lazy(() => loadReclaimView());
const Sub2ApiView = React.lazy(() => loadSub2ApiView());
const FEATURE_MODULE_LOADERS = {orders: loadOrderQueryView, reclaim: loadReclaimView, sub2api: loadSub2ApiView};
const DEFAULT_SUB2API_AUTOMATION = {
  enabled: false,
  interval_seconds: 300,
  auto_import: false,
  max_reclaim_attempts: 3,
};
const EMPTY_CARD_IMPORT_HISTORY = {
  items: [],
  total: 0,
  page: 1,
  page_size: 10,
  pages: 1,
  status: 'all',
  summary: {total: 0, success: 0, failed: 0, pending: 0, successful_accounts: 0, failed_accounts: 0},
};
const MONITOR_INTERVAL_OPTIONS = [1, 3, 5, 10, 30, 60, 300, 900, 1800];
const SHOP_GOODS_TYPES = [
  {value: 'card', label: '卡密商品'},
  {value: 'article', label: '文章商品'},
  {value: 'resource', label: '资源商品'},
  {value: 'equity', label: '权益商品'},
];

function getVisitorId() {
  const cached = window.localStorage.getItem('visitorId');
  if (cached) return cached;
  const value = Math.random().toString(36).slice(2, 11);
  window.localStorage.setItem('visitorId', value);
  return value;
}

function needsBrowserVerification(shop) {
  return /WAF|无法解析|HTML 页面|HTML页面/.test(shop.last_attempt?.error || '');
}

async function request(path, options = {}, {auth = true} = {}) {
  const headers = new Headers(options.headers || {});
  if (auth && apiAuthToken && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${apiAuthToken}`);
  const requestOptions = {...options, headers, credentials: options.credentials || 'include'};
  const response = await fetch(`${API}${path}`, requestOptions);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.detail || '请求失败');
    error.status = response.status;
    error.payload = payload;
    error.code = payload.code || '';
    error.retryable = payload.retryable === true;
    if (auth && response.status === 401 && typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('ldxp-auth-expired', {detail: payload}));
    }
    throw error;
  }
  return payload;
}

function money(value) {
  if (value === null || value === undefined || value === '') return '--';
  const number = Number(value);
  return Number.isFinite(number) ? `¥${number.toFixed(2)}` : '--';
}

function compactTime(value) {
  if (!value) return '尚未抓取';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date);
}

function cardImportVerificationPatch(result) {
  const verification = result?.import_verification || {};
  const expected = Math.max(0, Number(verification.expected || 0));
  const matched = Math.max(0, Number(verification.matched || 0));
  const failed = Math.max(Number(verification.failed || 0), expected - matched, 0);
  const confirmed = Boolean(verification.confirmed);
  return {
    status: confirmed ? 'success' : 'failed',
    stage: confirmed ? 'done' : 'ready',
    success_count: matched,
    failed_count: failed,
    message: confirmed ? `推送并核验成功，共 ${matched} 个账号` : `推送后仅核验 ${matched}/${expected} 个账号`,
    details: {
      new_account_ids: Array.isArray(verification.new_account_ids) ? verification.new_account_ids : [],
      missing_accounts: Array.isArray(verification.missing) ? verification.missing : [],
    },
  };
}

function intervalLabel(value) {
  const seconds = Number(value || 0);
  if (seconds < 60) return `${seconds}秒`;
  if (seconds % 60 === 0) return `${seconds / 60}分钟`;
  return `${seconds}秒`;
}

function intervalOptionLabel(value) {
  const seconds = Number(value || 0);
  return seconds < 60 ? `每 ${seconds} 秒` : `每 ${seconds / 60} 分钟`;
}

function MonitorIntervalSelect({value, onChange, label, disabled = false, caption = ''}) {
  return <label className={`monitor-interval-control ${disabled ? 'disabled' : ''}`} onClick={event => event.stopPropagation()}>
    {caption && <span>{caption}</span>}
    <div><TimerReset size={13}/><select value={Number(value)} onChange={event => onChange(Number(event.target.value))} aria-label={label} disabled={disabled}>
      {MONITOR_INTERVAL_OPTIONS.map(seconds => <option value={seconds} key={seconds}>{intervalOptionLabel(seconds)}</option>)}
    </select></div>
  </label>;
}

function ProductImage({item, size = 'normal'}) {
  const [failed, setFailed] = useState(false);
  if (!item?.latest?.image || failed) {
    return <div className={`product-image fallback ${size}`}><Package size={size === 'large' ? 30 : 19}/></div>;
  }
  return <img className={`product-image ${size}`} src={item.latest.image} alt="" onError={() => setFailed(true)}/>;
}

function StatusPill({item}) {
  if (itemIsUnlisted(item)) {
    return <span className="pill paused"><span className="dot"/>未上架</span>;
  }
  if (item.last_attempt?.status === 'error') {
    return <span className="pill error"><AlertCircle size={12}/>抓取异常</span>;
  }
  if (!item.latest) return <span className="pill neutral"><Clock3 size={12}/>等待数据</span>;
  if (item.latest.sale_status === 'on_sale') return <span className="pill live"><span className="dot"/>在售</span>;
  return <span className="pill neutral"><Clock3 size={12}/>状态未知</span>;
}

function historyStockMeta(point) {
  const raw = point?.stock;
  if (raw === null || raw === undefined || raw === '' || !Number.isFinite(Number(raw))) {
    return {key: 'unknown', label: point?.stock_label || '库存未知', value: null};
  }
  const value = Number(raw);
  return value > 0
    ? {key: 'in', label: '有货', value}
    : {key: 'out', label: '缺货', value: 0};
}

function historyStatusLabel(point) {
  return point?.status === 'success' ? '抓取成功' : point?.error || '抓取失败';
}

function signedMoney(value) {
  if (value === null || value === undefined || value === '') return '--';
  const number = Number(value);
  if (!Number.isFinite(number)) return '--';
  if (number === 0) return '持平';
  return `${number > 0 ? '+' : '-'}${money(Math.abs(number))}`;
}

function signedPercent(value) {
  if (value === null || value === undefined || value === '') return '--';
  const number = Number(value);
  if (!Number.isFinite(number)) return '--';
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}%`;
}

function Tooltip({label, children, placement = 'top'}) {
  if (!label) return children;
  const child = React.Children.only(children);
  const className = [child.props.className, 'tooltip-anchor'].filter(Boolean).join(' ');
  return React.cloneElement(child, {
    className,
    title: undefined,
    'data-tooltip': label,
    'data-tooltip-placement': placement,
  });
}

function IconButton({label, children, tone = '', ...props}) {
  return <Tooltip label={label}>
    <button className={`icon-button ${tone}`} aria-label={label} {...props}>{children}</button>
  </Tooltip>;
}

function FeatureLoadingState({feature, state, onRetry}) {
  const failed = state?.status === 'error';
  return <section className={`feature-loading-state ${failed ? 'error' : ''}`} role={failed ? 'alert' : 'status'} aria-live="polite">
    <span className="feature-loading-icon">{failed ? <AlertCircle size={24}/> : <LoaderCircle size={24} className="spin"/>}</span>
    <div><strong>{failed ? `${feature}加载失败` : `正在加载${feature}`}</strong><small>{failed ? state.error || '请稍后重试' : '仅在进入此功能时读取所需数据'}</small></div>
    {failed && <button className="button secondary" onClick={onRetry}><RefreshCw size={15}/>重新加载</button>}
  </section>;
}

function itemSpecValue(item, matcher) {
  const specs = item?.latest?.specs;
  if (!specs || typeof specs !== 'object') return '';
  const key = Object.keys(specs).find(value => matcher.test(value));
  return key ? String(specs[key] ?? '') : '';
}

function itemCategory(item) {
  return itemSpecValue(item, /分类|category/i) || '未分类';
}

function itemShopName(item) {
  return item?.shops?.[0]?.name || item?.shops?.[0]?.token || itemSpecValue(item, /店铺|seller|shop/i) || '独立商品';
}

function shopGoodsTypeLabel(value) {
  return SHOP_GOODS_TYPES.find(option => option.value === value)?.label || value || '卡密商品';
}

function shopCategoryLabel(shop) {
  if (!shop?.category_id) return '全部分类';
  return shop.category_name ? `${shop.category_name} · ID ${shop.category_id}` : `ID ${shop.category_id}`;
}

function itemPrice(item) {
  const raw = item?.latest?.price;
  if (raw === null || raw === undefined || raw === '') return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

const UNLISTED_ERROR_MARKERS = ['商品未上架', '商品不存在', '已下架', '已不在店铺列表'];

function itemIsUnlisted(item) {
  if (item?.latest?.sale_status === 'off_sale') return true;
  const error = String(item?.last_attempt?.error || '');
  return item?.last_attempt?.status === 'error' && UNLISTED_ERROR_MARKERS.some(marker => error.includes(marker));
}

function itemStock(item) {
  if (itemIsUnlisted(item)) return {key: 'off', label: '未上架', value: null};
  const raw = item?.latest?.stock;
  if (raw === null || raw === undefined || raw === '') return {key: 'unknown', label: '库存未知', value: null};
  const value = Number(raw);
  if (!Number.isFinite(value)) return {key: 'unknown', label: item?.latest?.stock_label || '库存未知', value: null};
  return value > 0 ? {key: 'in', label: '有货', value} : {key: 'out', label: '缺货', value: 0};
}

function itemPurchasable(item) {
  const stock = itemStock(item);
  return !itemIsUnlisted(item) && item?.latest?.sale_status === 'on_sale' && stock.key !== 'out';
}

function itemStockLabel(item) {
  const stock = itemStock(item);
  return `${stock.label}${stock.value !== null ? ` · ${stock.value}` : ''}`;
}

function RadarStockPill({item}) {
  const stock = itemStock(item);
  return <span className={`radar-stock ${stock.key}`}><span className="radar-stock-dot"/>{itemStockLabel(item)}</span>;
}

// Keep the catalog order independent from mutable sync timestamps.
function useStableItemOrder(items) {
  const orderRef = useRef([]);
  return useMemo(() => {
    const present = new Set(items.map(item => item.id));
    const next = orderRef.current.filter(id => present.has(id));
    const seen = new Set(next);
    items.forEach(item => {
      if (!seen.has(item.id)) {
        next.push(item.id);
        seen.add(item.id);
      }
    });
    orderRef.current = next;
    return new Map(next.map((id, index) => [id, index]));
  }, [items]);
}

function ProductOverviewView({items, shops, stableOrder, busy = {}, selectedId, onSelect, onOpenDetail, onBuy, onAdd, onDirect, onRefresh}) {
  const [query, setQuery] = useState('');
  const [stockFilter, setStockFilter] = useState('all');
  const [categoryFilter, setCategoryFilter] = useState('all');
  const [shopChoice, setShopChoice] = useState('all');
  const [priceFilter, setPriceFilter] = useState('all');
  const [sort, setSort] = useState('stable');

  const categories = useMemo(() => [...new Set(items.map(itemCategory))].sort((a, b) => a.localeCompare(b, 'zh-CN')), [items]);
  const filteredItems = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const result = items.filter(item => {
      const stock = itemStock(item);
      const haystack = [item.latest?.title, item.name, item.url, itemShopName(item), itemCategory(item)].filter(Boolean).join(' ').toLowerCase();
      if (needle && !haystack.includes(needle)) return false;
      if (stockFilter !== 'all' && stock.key !== stockFilter) return false;
      if (categoryFilter !== 'all' && itemCategory(item) !== categoryFilter) return false;
      if (shopChoice === 'standalone' && item.shops?.length) return false;
      if (shopChoice !== 'all' && shopChoice !== 'standalone' && !item.shops?.some(shop => String(shop.id) === shopChoice)) return false;
      if (priceFilter === 'quoted' && itemPrice(item) === null) return false;
      if (priceFilter === 'unquoted' && itemPrice(item) !== null) return false;
      return true;
    });
    return result.sort((left, right) => {
      if (sort === 'stable') return (stableOrder.get(left.id) ?? Number.MAX_SAFE_INTEGER) - (stableOrder.get(right.id) ?? Number.MAX_SAFE_INTEGER);
      if (sort === 'price-asc' || sort === 'price-desc') {
        const a = itemPrice(left); const b = itemPrice(right);
        if (a === null && b === null) return 0;
        if (a === null) return 1;
        if (b === null) return -1;
        return sort === 'price-asc' ? a - b : b - a;
      }
      if (sort === 'name') return (left.latest?.title || left.name || '').localeCompare(right.latest?.title || right.name || '', 'zh-CN');
      return new Date(right.last_attempt?.fetched_at || right.last_run || 0).getTime() - new Date(left.last_attempt?.fetched_at || left.last_run || 0).getTime();
    });
  }, [categoryFilter, items, priceFilter, query, shopChoice, sort, stableOrder, stockFilter]);
  const categoryMinimums = useMemo(() => {
    const result = new Map();
    items.forEach(item => {
      const price = itemPrice(item);
      const category = itemCategory(item);
      if (price !== null && itemStock(item).key !== 'off' && (!result.has(category) || price < result.get(category))) result.set(category, price);
    });
    return result;
  }, [items]);
  const quotedItems = filteredItems.filter(item => itemPrice(item) !== null);
  const lowestPriceItems = quotedItems.filter(item => itemStock(item).key !== 'off');
  const lowestPrice = lowestPriceItems.length ? Math.min(...lowestPriceItems.map(itemPrice)) : null;
  const inStock = filteredItems.filter(item => itemStock(item).key === 'in').length;
  const outOfStock = filteredItems.filter(item => itemStock(item).key === 'out').length;
  const clearFilters = () => {
    setQuery('');
    setStockFilter('all');
    setCategoryFilter('all');
    setShopChoice('all');
    setPriceFilter('all');
    setSort('stable');
  };
  const hasFilters = query || stockFilter !== 'all' || categoryFilter !== 'all' || shopChoice !== 'all' || priceFilter !== 'all' || sort !== 'stable';

  return <section className="product-overview-page" aria-label="商品总览">
    <div className="overview-stat-strip">
      <div><span>监控商品</span><strong>{filteredItems.length}</strong><small>全部 {items.length} 项</small></div>
      <div><span>有效报价</span><strong>{quotedItems.length}</strong><small>覆盖 {categories.length} 个分类</small></div>
      <div><span>当前有货</span><strong className="positive">{inStock}</strong><small>可直接进入购买流程</small></div>
      <div><span>缺货 / 未上架</span><strong className="negative">{outOfStock + filteredItems.filter(item => itemStock(item).key === 'off').length}</strong><small>持续监控库存变化</small></div>
      <div><span>全局最低报价</span><strong>{money(lowestPrice)}</strong><small>按当前筛选结果</small></div>
    </div>

    <div className="overview-category-bar" aria-label="商品分类">
      <button className={categoryFilter === 'all' ? 'active' : ''} onClick={() => setCategoryFilter('all')}><Package size={16}/>全部分类<span>{items.length}</span></button>
      {categories.map(category => {
        const count = items.filter(item => itemCategory(item) === category).length;
        return <button className={categoryFilter === category ? 'active' : ''} key={category} onClick={() => setCategoryFilter(category)}><Tag size={15}/>{category}<span>{count}</span></button>;
      })}
    </div>

    <div className="overview-layout">
      <aside className="overview-facets">
        <div className="overview-panel-heading"><div><span>MONITORED SHOPS</span><h2>监控店铺</h2></div><strong>{shops.length}</strong></div>
        <div className="overview-shop-list">
          <button className={shopChoice === 'all' ? 'active' : ''} onClick={() => setShopChoice('all')}>
            <span className="overview-shop-icon"><Store size={17}/></span>
            <span><strong>全部来源</strong><small>全部监控商品</small></span>
            <em>{items.length}</em>
          </button>
          {shops.map(shop => {
            const shopItems = items.filter(item => item.shops?.some(link => link.id === shop.id));
            const shopStock = shopItems.filter(item => itemStock(item).key === 'in').length;
            return <button className={shopChoice === String(shop.id) ? 'active' : ''} key={shop.id} onClick={() => setShopChoice(String(shop.id))}>
              <span className="overview-shop-icon"><Store size={17}/></span>
              <span><strong>{shop.name || shop.token}</strong><small>{shopStock} 项有货 · {shop.token}</small></span>
              <em>{shopItems.length}</em>
            </button>;
          })}
          {items.some(item => !item.shops?.length) && <button className={shopChoice === 'standalone' ? 'active' : ''} onClick={() => setShopChoice('standalone')}>
            <span className="overview-shop-icon"><Package size={17}/></span>
            <span><strong>独立监控</strong><small>未通过整店同步关联</small></span>
            <em>{items.filter(item => !item.shops?.length).length}</em>
          </button>}
        </div>
        <div className="overview-facet-note"><CircleDollarSign size={17}/><div><strong>报价口径</strong><span>价格取自最近一次本地监控快照，同类最低价按商品分类计算。</span></div></div>
      </aside>

      <section className="overview-board">
        <div className="overview-board-head">
          <div><span>PRICE RADAR</span><h2>一体化价格雷达</h2><p>集中比较店铺报价、同类最低价、库存与最近同步状态</p></div>
          <span className="overview-result-count">{filteredItems.length} 项结果</span>
        </div>
        <div className="overview-toolbar">
          <label className="overview-search"><Search size={17}/><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索商品、店铺、分类或链接"/></label>
          <label><span>库存状态</span><select aria-label="库存状态" value={stockFilter} onChange={event => setStockFilter(event.target.value)}><option value="all">全部库存</option><option value="in">仅看有货</option><option value="out">仅看缺货</option><option value="unknown">库存未知</option><option value="off">未上架</option></select></label>
          <label><span>报价状态</span><select aria-label="报价状态" value={priceFilter} onChange={event => setPriceFilter(event.target.value)}><option value="all">全部报价</option><option value="quoted">仅看有报价</option><option value="unquoted">暂无报价</option></select></label>
          <label><span>排序方式</span><select aria-label="排序方式" value={sort} onChange={event => setSort(event.target.value)}><option value="stable">当前顺序</option><option value="updated">最近同步</option><option value="price-asc">价格从低到高</option><option value="price-desc">价格从高到低</option><option value="name">商品名称</option></select></label>
          <button className="overview-reset" onClick={clearFilters} disabled={!hasFilters}><X size={15}/>重置</button>
        </div>

        <div className="overview-table" role="table" aria-label="商品报价列表">
          <div className="overview-table-head" role="row">
            <span>商品 / 分类</span><span>店铺</span><span>当前报价</span><span>同类最低</span><span>库存</span><span>最近同步</span><span>操作</span>
          </div>
          <div className="overview-table-body">
            {!filteredItems.length ? <div className="overview-empty"><CircleDollarSign size={28}/><strong>暂无匹配商品</strong><span>调整筛选条件后重试</span><button className="button secondary" onClick={clearFilters}>清除筛选</button></div> : filteredItems.map(item => {
              const price = itemPrice(item);
              const floor = categoryMinimums.get(itemCategory(item));
              const unlisted = itemStock(item).key === 'off';
              const isLowest = price !== null && floor !== undefined && price === floor && !unlisted;
              return <article className={`overview-product-row ${selectedId === item.id ? 'selected' : ''}`} key={item.id} role="row">
                <button type="button" className="overview-product-main" onClick={() => { onSelect(item.id); onOpenDetail(item.id); }} aria-label={`查看商品详情：${item.latest?.title || item.name || '等待商品数据'}`}>
                  <ProductImage item={item}/>
                  <span><strong>{item.latest?.title || item.name || '等待商品数据'}</strong><small><Tag size={12}/>{itemCategory(item)}{item.price_changed && <em>价格变化</em>}</small></span>
                </button>
                <div className="overview-shop-cell"><strong>{itemShopName(item)}</strong><small>{item.latest?.goods_key || '独立监控'}</small></div>
                <div className="overview-price-cell"><strong>{money(price)}</strong><small>{Number(item.latest?.market_price) > 0 ? `参考 ${money(item.latest.market_price)}` : '实时监控价'}</small></div>
                <div className="overview-floor-cell"><strong>{!unlisted && floor !== undefined ? money(floor) : '--'}</strong>{isLowest ? <span>当前最低</span> : <small>{unlisted ? '未上架' : price !== null && floor !== undefined ? `高 ${money(price - floor)}` : '暂无比较'}</small>}</div>
                <div><RadarStockPill item={item}/></div>
                <div className="overview-time-cell"><strong>{compactTime(item.last_attempt?.fetched_at)}</strong><small>{itemIsUnlisted(item) ? '未上架' : item.last_attempt?.status === 'error' ? '抓取异常' : intervalLabel(item.interval_seconds)}</small></div>
                <div className="overview-row-actions">
                  {onRefresh && <IconButton label="刷新商品" onClick={() => onRefresh(item.id)} disabled={busy[`fetch-${item.id}`]}><RefreshCw size={15} className={busy[`fetch-${item.id}`] ? 'spin' : ''}/></IconButton>}
                  <IconButton label="直达商品页" onClick={() => onDirect(item)}><ArrowUpRight size={15}/></IconButton>
                  <button className="overview-buy-button" onClick={() => onBuy(item)} disabled={!itemPurchasable(item) || busy[`buy-${item.id}`]}><Zap size={15}/>{busy[`buy-${item.id}`] ? '准备中' : '购买'}</button>
                  <IconButton label="加入购买清单" onClick={() => onAdd(item)} disabled={!itemPurchasable(item)}><ShoppingBag size={15}/></IconButton>
                </div>
              </article>;
            })}
          </div>
        </div>
      </section>
    </div>
  </section>;
}

function ProductOverviewDrawer({id, open, items, shops, stableOrder, busy = {}, selectedId, shopFilter, onClose, onSelect, onOpenDetail, onBuy, onAdd, onDirect, onRefresh, onShopFilter}) {
  const [query, setQuery] = useState('');
  const [stockFilter, setStockFilter] = useState('all');
  const [categoryFilter, setCategoryFilter] = useState('all');
  const [shopChoice, setShopChoice] = useState('all');
  const [priceFilter, setPriceFilter] = useState('all');
  const [sort, setSort] = useState('stable');

  const categories = useMemo(() => [...new Set(items.map(itemCategory))].sort((a, b) => a.localeCompare(b, 'zh-CN')), [items]);
  const filteredItems = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const result = items.filter(item => {
      const stock = itemStock(item);
      const haystack = [item.latest?.title, item.name, item.url, itemShopName(item), itemCategory(item)].filter(Boolean).join(' ').toLowerCase();
      if (needle && !haystack.includes(needle)) return false;
      if (stockFilter !== 'all' && stock.key !== stockFilter) return false;
      if (categoryFilter !== 'all' && itemCategory(item) !== categoryFilter) return false;
      if (shopChoice === 'standalone' && item.shops?.length) return false;
      if (shopChoice !== 'all' && shopChoice !== 'standalone' && !item.shops?.some(shop => String(shop.id) === shopChoice)) return false;
      if (priceFilter === 'quoted' && itemPrice(item) === null) return false;
      if (priceFilter === 'unquoted' && itemPrice(item) !== null) return false;
      return true;
    });
    return result.sort((left, right) => {
      if (sort === 'stable') return (stableOrder.get(left.id) ?? Number.MAX_SAFE_INTEGER) - (stableOrder.get(right.id) ?? Number.MAX_SAFE_INTEGER);
      if (sort === 'price-asc' || sort === 'price-desc') {
        const a = itemPrice(left); const b = itemPrice(right);
        if (a === null && b === null) return 0;
        if (a === null) return 1;
        if (b === null) return -1;
        return sort === 'price-asc' ? a - b : b - a;
      }
      if (sort === 'name') return (left.latest?.title || left.name || '').localeCompare(right.latest?.title || right.name || '', 'zh-CN');
      return new Date(right.last_attempt?.fetched_at || right.last_run || 0).getTime() - new Date(left.last_attempt?.fetched_at || left.last_run || 0).getTime();
    });
  }, [categoryFilter, items, priceFilter, query, shopChoice, sort, stableOrder, stockFilter]);
  const quotedItems = filteredItems.filter(item => itemPrice(item) !== null);
  const inStock = filteredItems.filter(item => itemStock(item).key === 'in').length;
  const outOfStock = filteredItems.filter(item => itemStock(item).key === 'out').length;
  const lowestPriceItems = quotedItems.filter(item => itemStock(item).key !== 'off');
  const lowestPrice = lowestPriceItems.length ? Math.min(...lowestPriceItems.map(itemPrice)) : null;
  const categoryMinimums = useMemo(() => {
    const result = new Map();
    items.forEach(item => {
      const price = itemPrice(item);
      const category = itemCategory(item);
      if (price !== null && itemStock(item).key !== 'off' && (!result.has(category) || price < result.get(category))) result.set(category, price);
    });
    return result;
  }, [items]);

  useEffect(() => {
    setShopChoice(shopFilter === null ? 'all' : String(shopFilter));
  }, [shopFilter]);

  const chooseShop = value => {
    setShopChoice(value === null ? 'all' : String(value));
    onShopFilter(value === null ? null : Number(value));
  };

  return <>
    <div className={`drawer-backdrop ${open ? 'open' : ''}`} onClick={onClose} aria-hidden="true" />
    <aside id={id} className={`product-overview-drawer ${open ? 'open' : ''}`} aria-label="商品总览" aria-hidden={!open} inert={!open ? true : undefined}>
      <div className="drawer-header">
        <div><span className="drawer-kicker">PRICE RADAR</span><h2>商品总览</h2><p>报价、库存和店铺状态</p></div>
        <IconButton label="关闭商品总览" onClick={onClose}><X size={17}/></IconButton>
      </div>
      <div className="radar-summary">
        <div><span>监控商品</span><strong>{filteredItems.length}</strong></div>
        <div><span>有货</span><strong className="positive">{inStock}</strong></div>
        <div><span>缺货 / 未上架</span><strong className="negative">{outOfStock + filteredItems.filter(item => itemStock(item).key === 'off').length}</strong></div>
        <div><span>最低报价</span><strong>{money(lowestPrice)}</strong></div>
      </div>
      <div className="drawer-section shop-radar-section">
        <div className="drawer-section-head"><div><span className="drawer-kicker">MONITORED SHOPS</span><h3>监控店铺</h3></div><span className="drawer-count">{shops.length}</span></div>
        <div className="radar-shop-list">
          <button className={`radar-shop-chip ${shopChoice === 'all' && shopFilter === null ? 'active' : ''}`} onClick={() => chooseShop(null)}><Store size={14}/><span>全部店铺</span><strong>{items.length}</strong></button>
          {shops.map(shop => {
            const count = items.filter(item => item.shops?.some(link => link.id === shop.id)).length;
            return <button className={`radar-shop-chip ${String(shop.id) === shopChoice ? 'active' : ''}`} key={shop.id} onClick={() => chooseShop(shop.id)}><Store size={14}/><span>{shop.name || shop.token}</span><strong>{count}</strong></button>;
          })}
          {items.some(item => !item.shops?.length) && <button className={`radar-shop-chip ${shopChoice === 'standalone' ? 'active' : ''}`} onClick={() => { setShopChoice('standalone'); onShopFilter(null); }}><Package size={14}/><span>独立监控</span><strong>{items.filter(item => !item.shops?.length).length}</strong></button>}
        </div>
      </div>
      <div className="drawer-section radar-filter-section">
        <div className="drawer-section-head"><div><span className="drawer-kicker">FILTERS</span><h3>筛选与排序</h3></div><Filter size={16}/></div>
        <label className="radar-search"><Search size={15}/><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索商品、店铺或分类"/></label>
        <div className="radar-filter-grid">
          <label><span>库存</span><select value={stockFilter} onChange={event => setStockFilter(event.target.value)}><option value="all">全部库存</option><option value="in">仅看有货</option><option value="out">仅看缺货</option><option value="unknown">库存未知</option><option value="off">未上架</option></select></label>
          <label><span>分类</span><select value={categoryFilter} onChange={event => setCategoryFilter(event.target.value)}><option value="all">全部分类</option>{categories.map(category => <option value={category} key={category}>{category}</option>)}</select></label>
          <label><span>店铺</span><select value={shopChoice} onChange={event => chooseShop(event.target.value === 'all' ? null : event.target.value)}><option value="all">全部店铺</option>{shops.map(shop => <option value={shop.id} key={shop.id}>{shop.name || shop.token}</option>)}<option value="standalone">独立监控</option></select></label>
          <label><span>报价</span><select value={priceFilter} onChange={event => setPriceFilter(event.target.value)}><option value="all">全部报价</option><option value="quoted">仅看有报价</option><option value="unquoted">暂无报价</option></select></label>
          <label><span>排序</span><select value={sort} onChange={event => setSort(event.target.value)}><option value="stable">当前顺序</option><option value="updated">最近同步</option><option value="price-asc">报价从低到高</option><option value="price-desc">报价从高到低</option><option value="name">商品名称</option></select></label>
        </div>
      </div>
      <div className="drawer-section radar-list-section">
        <div className="drawer-section-head"><div><span className="drawer-kicker">QUOTE BOARD</span><h3>价格雷达</h3></div><span className="drawer-count">{quotedItems.length} 报价</span></div>
        <div className="radar-list">
          {!filteredItems.length ? <div className="radar-empty"><CircleDollarSign size={24}/><strong>暂无匹配商品</strong><span>调整筛选条件后重试</span></div> : filteredItems.map(item => {
            const price = itemPrice(item);
            const floor = categoryMinimums.get(itemCategory(item));
            return <article className={`radar-item ${selectedId === item.id ? 'selected' : ''}`} key={item.id}>
              <button type="button" className="radar-item-main" onClick={() => { onSelect(item.id); onOpenDetail(item.id); }} aria-label={`查看商品详情：${item.latest?.title || item.name || '等待商品数据'}`}>
                <ProductImage item={item}/>
                <div className="radar-copy"><strong title={item.latest?.title || item.name}>{item.latest?.title || item.name || '等待商品数据'}</strong><span>{itemShopName(item)} · {itemCategory(item)}</span><RadarStockPill item={item}/></div>
                <div className="radar-quote"><strong>{money(price)}</strong><small>{floor !== undefined ? `最低 ${money(floor)}` : '暂无最低价'}</small>{item.price_changed && <em>变价</em>}</div>
              </button>
              <div className="radar-actions">{onRefresh && <IconButton label="刷新商品" onClick={() => onRefresh(item.id)} disabled={busy[`fetch-${item.id}`]}><RefreshCw size={14} className={busy[`fetch-${item.id}`] ? 'spin' : ''}/></IconButton>}<IconButton label="直达商品页" onClick={() => onDirect(item)}><ArrowUpRight size={14}/></IconButton><IconButton label="一键购买" tone="buy" onClick={() => onBuy(item)} disabled={!itemPurchasable(item) || busy[`buy-${item.id}`]}><Zap size={14} className={busy[`buy-${item.id}`] ? 'spin' : ''}/></IconButton><IconButton label="加入购买清单" onClick={() => onAdd(item)} disabled={!itemPurchasable(item)}><ShoppingBag size={14}/></IconButton></div>
            </article>;
          })}
        </div>
      </div>
      <div className="drawer-foot"><Tag size={14}/><span>报价为本地监控快照，最低价按全部监控商品分类计算</span></div>
    </aside>
  </>;
}


function ProductDetailDrawer({open, item, history, trend, historyTotal, priceDelta, lowestPrice, historyBusy = false, busy = {}, onClose, onBuy, onAdd, onRefresh, onDirect}) {
  const latest = item?.latest;
  const stock = itemStock(item);
  const specs = latest?.specs && typeof latest.specs === 'object' ? Object.entries(latest.specs) : [];
  const commerceTags = Array.isArray(latest?.commerce_tags) ? latest.commerce_tags : [];
  const sourceUrl = latest?.source_url || item?.url;
  const currentPrice = itemPrice(item);
  const marketPriceValue = Number(latest?.market_price);
  const marketPrice = Number.isFinite(marketPriceValue) && marketPriceValue > 0 ? marketPriceValue : null;
  const successfulHistory = history.filter(point => point.status === 'success');
  const successfulTrend = (trend || []).filter(point => point.status === 'success');
  const priceValues = successfulTrend
    .map(point => Number(point.price))
    .filter(Number.isFinite);
  const priceMinimum = priceValues.length ? Math.min(...priceValues) : currentPrice;
  const priceMaximum = priceValues.length ? Math.max(...priceValues) : currentPrice;
  const priceAverage = priceValues.length ? priceValues.reduce((sum, value) => sum + value, 0) / priceValues.length : currentPrice;
  const syncRate = history.length ? Math.round((successfulHistory.length / history.length) * 100) : null;
  const discountRate = currentPrice !== null && marketPrice !== null ? ((marketPrice - currentPrice) / marketPrice) * 100 : null;
  const lowestGap = currentPrice !== null && lowestPrice !== null && lowestPrice !== undefined ? currentPrice - Number(lowestPrice) : null;
  const recentHistory = [...history]
    .sort((left, right) => new Date(left.fetched_at).getTime() - new Date(right.fetched_at).getTime())
    .slice(-24);
  const recentSlots = Array.from({length: 24}, (_, index) => recentHistory[index - (24 - recentHistory.length)] || null);
  const saleStatus = itemIsUnlisted(item) ? '未上架' : latest?.sale_status === 'on_sale' ? '在售' : '状态未知';
  const priceChangeLabel = priceDelta === null || priceDelta === undefined
    ? '暂无对比'
    : priceDelta === 0 ? '价格稳定' : `${priceDelta > 0 ? '+' : '-'}${money(Math.abs(priceDelta))}`;

  return <>
    <div className={`drawer-backdrop detail-backdrop ${open ? 'open' : ''}`} onClick={onClose} aria-hidden="true"/>
    <aside className={`product-detail-drawer ${open ? 'open' : ''}`} aria-label="商品详情" aria-hidden={!open} inert={!open ? true : undefined}>
      <div className="drawer-header">
        <div><span className="drawer-kicker">PRODUCT DETAIL</span><h2>商品详情</h2><p>{item ? itemShopName(item) : '未选择商品'}</p></div>
        <IconButton label="关闭商品详情" onClick={onClose}><X size={17}/></IconButton>
      </div>
      {item && <nav className="detail-drawer-tabs" aria-label="商品详情分区">
        <a href="#detail-overview">概览</a>
        <a href="#detail-quality">监控质量</a>
        <a href="#detail-price-history">价格走势</a>
        <a href="#detail-product-info">商品信息</a>
      </nav>}
      {!item ? <div className="drawer-empty"><Package size={30}/><strong>请选择商品</strong><span>从价格雷达或商品目录打开详情</span></div> : <>
        <div className="detail-drawer-body">
          <section className="detail-section-anchor" id="detail-overview">
            <div className="detail-drawer-hero">
              <ProductImage item={item} size="large"/>
              <div className="detail-hero-copy">
                <span className="detail-kicker">{itemCategory(item)}</span>
                <h3>{latest?.title || item.name || '等待商品数据'}</h3>
                {!!commerceTags.length && <div className="detail-commerce-tags" aria-label="商品交易标签">
                  {commerceTags.map(tag => <span className={`detail-commerce-tag ${tag.tone || 'neutral'}`} title={tag.detail || tag.label} key={tag.key}>{tag.label}</span>)}
                </div>}
                <div className="detail-hero-meta"><RadarStockPill item={item}/><span><Store size={13}/>{itemShopName(item)}</span><span><Package size={13}/>{latest?.goods_key || `商品 #${item.id}`}</span></div>
              </div>
              <div className="detail-hero-price"><span>当前报价</span><strong>{money(currentPrice)}</strong><small className={priceDelta > 0 ? 'negative' : priceDelta < 0 ? 'positive' : ''}>{priceChangeLabel}</small></div>
            </div>

            {!!commerceTags.length && <section className="detail-commerce-summary" aria-label="交易权益">
              <div className="detail-commerce-heading"><span><BadgePercent size={16}/></span><div><strong>交易权益</strong><small>链动小铺接口实时同步</small></div></div>
              <div className="detail-commerce-list">
                {commerceTags.map(tag => <div className="detail-commerce-item" key={tag.key}>
                  <span className={`detail-commerce-tag ${tag.tone || 'neutral'}`}>{tag.label}</span>
                  <small>{tag.detail || '以结算页面为准'}</small>
                </div>)}
              </div>
            </section>}

            <div className="detail-price-board" aria-label="价格指标">
              <div><span><CircleDollarSign size={14}/>当前报价</span><strong>{money(currentPrice)}</strong><small>{lowestGap === null || !Number.isFinite(lowestGap) ? '暂无同类比较' : lowestGap <= 0 ? '当前同类最低' : `高于最低 ${money(lowestGap)}`}</small></div>
              <div><span><Tag size={14}/>同类最低</span><strong>{money(lowestPrice)}</strong><small>{itemCategory(item)} 分类</small></div>
              <div><span><Activity size={14}/>参考价格</span><strong>{money(marketPrice)}</strong><small>{discountRate === null ? '未提供参考价' : discountRate >= 0 ? `较参考价低 ${Math.abs(discountRate).toFixed(1)}%` : `较参考价高 ${Math.abs(discountRate).toFixed(1)}%`}</small></div>
              <div><span><History size={14}/>历史均价</span><strong>{money(priceAverage)}</strong><small>{priceValues.length} 个有效价格样本</small></div>
            </div>

            <div className="detail-state-grid" aria-label="库存与销售状态">
              <div><span>库存状态</span><strong className={`state-text ${stock.key}`}>{itemStockLabel(item)}</strong></div>
              <div><span>销售状态</span><strong>{saleStatus}</strong></div>
              <div><span>最低起购</span><strong>{latest?.limit_count || 1} 件</strong></div>
              <div><span>查询密码</span><strong>{latest?.query_password_required ? '下单时需要' : '无需密码'}</strong></div>
              <div><span>自动监控</span><strong>{item.enabled ? `运行中 · ${intervalLabel(item.interval_seconds)}` : '已暂停'}</strong></div>
              <div><span>最近同步</span><strong>{compactTime(item.last_attempt?.fetched_at)}</strong></div>
            </div>
          </section>

          <div className="detail-information-grid detail-section-anchor" id="detail-quality">
            <section className="detail-information-panel detail-quality-panel">
              <div className="detail-panel-heading"><div><span className="drawer-kicker">MONITOR QUALITY</span><h3>监控质量</h3></div><ShieldCheck size={18}/></div>
              <div className="detail-quality-overview">
                <div className={`detail-quality-score ${syncRate !== null && syncRate < 80 ? 'warning' : ''}`}><strong>{syncRate === null ? '--' : `${syncRate}%`}</strong><span>抓取成功率</span></div>
                <dl><div><dt>成功记录</dt><dd>{successfulHistory.length}</dd></div><div><dt>异常记录</dt><dd>{history.length - successfulHistory.length}</dd></div><div><dt>监控样本</dt><dd>{history.length}</dd></div><div><dt>首次记录</dt><dd>{compactTime(history[0]?.fetched_at)}</dd></div></dl>
              </div>
              <div className="detail-sync-header"><span>最近 24 次同步</span><small>由早到晚</small></div>
              <div className="detail-sync-strip" aria-label="最近 24 次同步结果">
                {recentSlots.map((point, index) => <span className={point ? point.status === 'success' ? 'success' : 'error' : 'empty'} title={point ? `${compactTime(point.fetched_at)} · ${point.status === 'success' ? '成功' : point.error || '失败'}` : '暂无记录'} key={`${point?.id || 'empty'}-${index}`}/>) }
              </div>
              <div className="detail-sync-legend"><span><i className="success"/>成功</span><span><i className="error"/>异常</span><span><i className="empty"/>无记录</span></div>
            </section>

            <section className="detail-information-panel">
              <div className="detail-panel-heading"><div><span className="drawer-kicker">SOURCE & RULES</span><h3>来源与规则</h3></div><Store size={18}/></div>
              <div className="detail-source-list">
                <dl><dt>所属店铺</dt><dd>{itemShopName(item)}</dd></dl>
                <dl><dt>商品标识</dt><dd>{latest?.goods_key || '--'}</dd></dl>
                <dl><dt>商品分类</dt><dd>{itemCategory(item)}</dd></dl>
                <dl><dt>数据来源</dt><dd>{item.shops?.length ? '店铺同步' : '独立监控'}</dd></dl>
                <dl><dt>抓取状态</dt><dd className={item.last_attempt?.status === 'error' ? 'negative' : 'positive'}>{item.last_attempt?.status === 'error' ? '最近一次异常' : '最近一次成功'}</dd></dl>
                <dl><dt>监控编号</dt><dd>#{item.id}</dd></dl>
              </div>
              <a className="detail-source-link" href={sourceUrl} target="_blank" rel="noreferrer"><Link2 size={14}/><span>{sourceUrl}</span><ArrowUpRight size={14}/></a>
            </section>
          </div>

          <section className="detail-information-panel detail-price-history detail-section-anchor" id="detail-price-history">
            <div className="detail-panel-heading"><div><span className="drawer-kicker">PRICE HISTORY</span><h3>价格走势</h3></div><span>{Number(historyTotal || trend?.length || history.length).toLocaleString('zh-CN')} 次监控记录</span></div>
            <div className="detail-history-summary"><div><span>历史最低</span><strong>{money(priceMinimum)}</strong></div><div><span>历史最高</span><strong>{money(priceMaximum)}</strong></div><div><span>历史均价</span><strong>{money(priceAverage)}</strong></div><div><span>最近变动</span><strong className={priceDelta > 0 ? 'negative' : priceDelta < 0 ? 'positive' : ''}>{priceChangeLabel}</strong></div></div>
            {historyBusy ? <div className="detail-price-chart-loading"><RefreshCw size={17} className="spin"/><span>正在读取该商品的价格记录…</span></div> : <React.Suspense fallback={<div className="detail-price-chart-loading"><RefreshCw size={17} className="spin"/><span>正在加载价格走势组件…</span></div>}>
              <ProductDetailPriceChart points={trend}/>
            </React.Suspense>}
          </section>

          <section className="detail-product-information detail-section-anchor" id="detail-product-info">
            <div className="detail-description"><span className="drawer-kicker">DESCRIPTION</span><h3>商品说明</h3><p>{latest?.description || '暂无商品描述'}</p></div>
            <div className="detail-drawer-specs"><span className="drawer-kicker">SPECIFICATIONS</span><h3>规格参数</h3>{specs.length ? <div>{specs.map(([key, value]) => <dl key={key}><dt>{key}</dt><dd>{String(value)}</dd></dl>)}</div> : <p>暂无规格参数</p>}</div>
          </section>

          {item.last_attempt?.status === 'error' && <div className="inline-error detail-error"><AlertCircle size={16}/><span>{item.last_attempt.error}</span></div>}
        </div>

        <div className="detail-action-bar">
          <div><span>当前商品</span><strong>{itemShopName(item)}</strong></div>
          {onRefresh && <button className="button secondary" onClick={() => onRefresh(item.id)} disabled={busy[`fetch-${item.id}`]}><RefreshCw size={15} className={busy[`fetch-${item.id}`] ? 'spin' : ''}/>刷新</button>}
          <button className="button secondary" onClick={() => onDirect(item)}><ArrowUpRight size={15}/>商品页</button>
          <button className="button secondary" onClick={() => onAdd(item)} disabled={!itemPurchasable(item)}><ShoppingBag size={15}/>加入清单</button>
          <button className="button primary" onClick={() => onBuy(item)} disabled={!itemPurchasable(item) || busy[`buy-${item.id}`]}><Zap size={15}/>{busy[`buy-${item.id}`] ? '正在准备' : '立即购买'}</button>
        </div>
      </>}
    </aside>
  </>;
}

function HistoryView({
  items,
  selectedId,
  onSelect,
  history,
  trend,
  meta,
  filters,
  onFiltersChange,
  onResetFilters,
  page,
  pageSize,
  onPageChange,
  onPageSizeChange,
  busy,
}) {
  const [itemQuery, setItemQuery] = useState('');
  const [chartMode, setChartMode] = useState('combined');
  const [comparison, setComparison] = useState('average');
  const [rangePreset, setRangePreset] = useState('all');
  const selected = items.find(item => item.id === selectedId) || null;
  const stats = meta?.stats || {};
  const total = Number(meta?.total || 0);
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const pageStart = total ? (page - 1) * pageSize + 1 : 0;
  const pageEnd = Math.min(page * pageSize, total);
  const visibleItems = useMemo(() => {
    const needle = itemQuery.trim().toLowerCase();
    if (!needle) return items;
    return items.filter(item => [item.latest?.title, item.name, item.url, item.latest?.goods_key].filter(Boolean).join(' ').toLowerCase().includes(needle));
  }, [itemQuery, items]);
  const updateFilter = (key, value) => {
    if (key === 'startDate' || key === 'endDate') setRangePreset('custom');
    onFiltersChange({...filters, [key]: value});
    onPageChange(1);
  };
  const clearFilters = () => {
    setRangePreset('all');
    onResetFilters();
    onPageChange(1);
  };
  const applyRangePreset = preset => {
    setRangePreset(preset);
    if (preset === 'all') {
      onFiltersChange({...filters, startDate: '', endDate: ''});
    } else {
      const days = {day: 1, week: 7, month: 30, quarter: 90}[preset];
      const end = new Date();
      const start = new Date(end);
      start.setDate(start.getDate() - days);
      const dateValue = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
      onFiltersChange({...filters, startDate: dateValue(start), endDate: dateValue(end)});
    }
    onPageChange(1);
  };
  const successRate = total ? `${Math.round((Number(stats.success_count || 0) / total) * 100)}%` : '--';
  const rows = history || [];
  const rowDelta = index => {
    const currentRaw = rows[index]?.price;
    const previousRow = rows.slice(index + 1).find(row => row.status === 'success' && row.price !== null && row.price !== undefined && row.price !== '');
    const previousRaw = previousRow?.price;
    if (currentRaw === null || currentRaw === undefined || currentRaw === '' || previousRaw === null || previousRaw === undefined || previousRaw === '') return null;
    const current = Number(currentRaw);
    const previous = Number(previousRaw);
    if (!Number.isFinite(current) || !Number.isFinite(previous)) return null;
    return current - previous;
  };
  const numericStat = value => value === null || value === undefined || value === '' ? Number.NaN : Number(value);
  const latestChange = numericStat(stats.latest_change);
  const intervalChange = numericStat(stats.price_change);
  const availabilityRate = numericStat(stats.availability_rate);
  const volatility = numericStat(stats.volatility_percent);

  return <section className="history-view history-view-enhanced">
    <aside className="history-sidebar">
      <div className="history-sidebar-head">
        <div><span className="history-kicker">MONITORED PRODUCTS</span><h2>商品记录</h2></div>
        <strong>{items.length}</strong>
      </div>
      <label className="history-product-search"><Search size={15}/><input value={itemQuery} onChange={event => setItemQuery(event.target.value)} placeholder="搜索商品" aria-label="搜索商品"/></label>
      <div className="history-product-list">
        {!visibleItems.length ? <div className="history-sidebar-empty"><Database size={19}/><span>没有匹配商品</span></div> : visibleItems.map(item => <button className={item.id === selectedId ? 'active' : ''} key={item.id} onClick={() => { onSelect(item.id); onPageChange(1); }}>
          <ProductImage item={item}/>
          <span><strong>{item.latest?.title || item.name || '等待商品数据'}</strong><small>{money(item.latest?.price)} · {item.latest?.stock_label || '库存未知'}</small></span>
          <ChevronRight size={15}/>
        </button>)}
      </div>
    </aside>

    <div className="history-main history-main-enhanced">
      <div className="history-top history-top-enhanced">
        <div><span className="history-kicker">PRICE HISTORY / DATA EXPLORER</span><h2>{selected?.latest?.title || '请选择商品'}</h2><p>{selected ? `${total.toLocaleString('zh-CN')} 条历史记录 · 服务端分页加载` : '从左侧选择商品查看价格、库存和抓取结果'}</p></div>
        <div className="history-current-price"><span>当前价格</span><strong>{money(stats.latest_price ?? selected?.latest?.price)}</strong><small className={latestChange > 0 ? 'negative' : latestChange < 0 ? 'positive' : ''}>{Number.isFinite(latestChange) ? `较上次 ${signedMoney(latestChange)} · ${signedPercent(stats.latest_change_percent)}` : selected?.last_attempt?.fetched_at ? `同步于 ${compactTime(selected.last_attempt.fetched_at)}` : '尚未同步'}</small></div>
      </div>

      <div className="history-filter-panel">
        <div className="history-filter-title"><SlidersHorizontal size={16}/><span>精细筛选</span><small>按日期、结果和库存缩小数据范围</small></div>
        <div className="history-filter-fields">
          <label className="history-filter-search"><span>记录关键词</span><div><Search size={15}/><input value={filters.query} onChange={event => updateFilter('query', event.target.value)} placeholder="价格、库存、错误信息"/></div></label>
          <label><span>开始日期</span><div className="history-date-input"><CalendarDays size={15}/><input type="date" value={filters.startDate} onChange={event => updateFilter('startDate', event.target.value)}/></div></label>
          <label><span>结束日期</span><div className="history-date-input"><CalendarDays size={15}/><input type="date" value={filters.endDate} onChange={event => updateFilter('endDate', event.target.value)}/></div></label>
          <label><span>抓取结果</span><select value={filters.status} onChange={event => updateFilter('status', event.target.value)}><option value="all">全部结果</option><option value="success">仅成功</option><option value="error">仅失败</option></select></label>
          <label><span>库存状态</span><select value={filters.stock} onChange={event => updateFilter('stock', event.target.value)}><option value="all">全部库存</option><option value="in">有货</option><option value="out">缺货</option><option value="unknown">库存未知</option></select></label>
          <button className="history-filter-reset" onClick={clearFilters} disabled={!filters.query && !filters.startDate && !filters.endDate && filters.status === 'all' && filters.stock === 'all'}><X size={15}/>清除筛选</button>
        </div>
      </div>

      <div className="history-summary-grid">
        <Tooltip label="筛选区间内最后一笔有效报价，相对该区间第一笔报价的变化"><div tabIndex={0}><span>区间变化</span><strong className={intervalChange > 0 ? 'negative' : intervalChange < 0 ? 'positive' : ''}>{Number.isFinite(intervalChange) ? signedMoney(intervalChange) : '--'}</strong><small>{signedPercent(stats.price_change_percent)} · 首价 {money(stats.first_price)}</small></div></Tooltip>
        <Tooltip label="筛选区间内的最低价、最高价与算术平均价"><div tabIndex={0}><span>价格区间</span><strong>{money(stats.min_price)} <em>至</em> {money(stats.max_price)}</strong><small>均价 {money(stats.average_price)}</small></div></Tooltip>
        <Tooltip label="有货率只统计返回了明确库存数量的成功快照"><div tabIndex={0}><span>库存可用率</span><strong className={Number.isFinite(availabilityRate) && availabilityRate < 50 ? 'negative' : 'positive'}>{Number.isFinite(availabilityRate) ? `${availabilityRate.toFixed(1)}%` : '--'}</strong><small>{Number(stats.in_stock_count || 0)} 有货 · {Number(stats.out_stock_count || 0)} 缺货</small></div></Tooltip>
        <Tooltip label="价格波动率为区间价格标准差占平均价格的比例"><div tabIndex={0}><span>价格波动率</span><strong>{Number.isFinite(volatility) ? `${volatility.toFixed(2)}%` : '--'}</strong><small>{Number(stats.price_change_count || 0)} 次价格变化</small></div></Tooltip>
        <Tooltip label="库存从无货变为有货记为补货，从有货变为无货记为缺货"><div tabIndex={0}><span>库存事件</span><strong>{Number(stats.restock_count || 0)} <em>补货</em> / {Number(stats.sold_out_count || 0)} <em>缺货</em></strong><small>{Number(stats.unknown_stock_count || 0)} 条库存未知</small></div></Tooltip>
        <Tooltip label="筛选后的抓取覆盖情况与失败数量"><div tabIndex={0}><span>数据质量</span><strong>{Number(stats.quoted_count || 0).toLocaleString('zh-CN')}</strong><small>成功率 {successRate} · <span className={Number(stats.error_count || 0) ? 'negative' : ''}>{Number(stats.error_count || 0)} 异常</span></small></div></Tooltip>
      </div>

      <section className="history-chart-card">
        <div className="history-card-head"><div><span className="history-kicker">TREND ANALYSIS</span><h3>价格与库存走势</h3></div><span className="history-record-total">按真实抓取时间绘制 · 最多 720 个节点</span></div>
        <div className="history-chart-toolbar">
          <div className="history-chart-control"><span>时间范围</span><div className="history-segmented history-range-segmented" role="group" aria-label="趋势时间范围">{[
            ['day', '24小时'], ['week', '7天'], ['month', '30天'], ['quarter', '90天'], ['all', '全部'],
          ].map(([value, label]) => <button key={value} className={rangePreset === value ? 'active' : ''} aria-pressed={rangePreset === value} onClick={() => applyRangePreset(value)}>{label}</button>)}</div></div>
          <div className="history-chart-control"><span>图表模式</span><div className="history-segmented" role="group" aria-label="图表显示模式">{[
            ['combined', '价格 + 库存'], ['price', '仅价格'], ['inventory', '仅库存'],
          ].map(([value, label]) => <button key={value} className={chartMode === value ? 'active' : ''} aria-pressed={chartMode === value} onClick={() => setChartMode(value)}>{label}</button>)}</div></div>
          <label className="history-comparison-select"><span>价格对比</span><select value={comparison} onChange={event => setComparison(event.target.value)} disabled={chartMode === 'inventory'}><option value="none">无基准</option><option value="previous">上次价格</option><option value="average">区间均价</option><option value="lowest">区间最低</option><option value="highest">区间最高</option></select></label>
        </div>
        {busy ? <div className="history-loading"><RefreshCw size={18} className="spin"/><span>正在读取趋势数据…</span></div> : <React.Suspense fallback={<div className="history-loading"><RefreshCw size={18} className="spin"/><span>正在加载趋势图组件…</span></div>}>
          <PriceHistoryChart points={trend} stats={stats} mode={chartMode} comparison={comparison}/>
        </React.Suspense>}
      </section>

      <section className="history-record-card">
        <div className="history-card-head"><div><span className="history-kicker">AUDIT LOG</span><h3>抓取明细</h3></div><span className="history-record-total">{total ? `${pageStart.toLocaleString('zh-CN')}-${pageEnd.toLocaleString('zh-CN')} / ${total.toLocaleString('zh-CN')}` : '暂无记录'}</span></div>
        <div className="history-table-scroll">
          <div className="history-row history-row-enhanced head"><span>抓取时间</span><span>价格</span><span>变化</span><span>库存</span><span>销售状态</span><span>抓取结果</span></div>
          {busy ? <div className="history-loading"><RefreshCw size={18} className="spin"/><span>正在加载历史记录…</span></div> : !rows.length ? <div className="history-loading"><Database size={22}/><span>当前筛选条件下没有记录</span><button className="button secondary" onClick={clearFilters}>清除筛选</button></div> : rows.map((point, index) => {
            const stockMeta = historyStockMeta(point);
            const delta = rowDelta(index);
            return <div className="history-row history-row-enhanced" key={`${point.id}-${index}`}>
              <span className="history-time-cell"><time dateTime={point.fetched_at}>{compactTime(point.fetched_at)}</time><small>{point.id ? `记录 #${point.id}` : '本地快照'}</small></span>
              <strong className="history-price-cell">{money(point.price)}</strong>
              <span className={delta > 0 ? 'negative history-delta' : delta < 0 ? 'positive history-delta' : 'history-delta'}>{delta === null ? '--' : delta === 0 ? '持平' : `${delta > 0 ? '+' : '-'}${money(Math.abs(delta))}`}</span>
              <span className={`history-stock-pill ${stockMeta.key}`}><i/>{stockMeta.label}{stockMeta.value !== null ? ` · ${stockMeta.value}` : ''}</span>
              <span className={point.sale_status === 'on_sale' ? 'positive' : point.sale_status === 'off_sale' ? 'negative' : ''}>{point.sale_status === 'on_sale' ? '在售' : point.sale_status === 'off_sale' ? '已下架' : '未知'}</span>
              <span className={`history-result ${point.status === 'success' ? 'positive' : 'negative'}`} title={historyStatusLabel(point)}>{historyStatusLabel(point)}</span>
            </div>;
          })}
        </div>
        <div className="history-pagination">
          <span>每页 <select value={pageSize} onChange={event => onPageSizeChange(Number(event.target.value))}><option value={15}>15 条</option><option value={25}>25 条</option><option value={50}>50 条</option><option value={100}>100 条</option></select></span>
          <div><button aria-label="上一页" title="上一页" onClick={() => onPageChange(Math.max(1, page - 1))} disabled={page <= 1 || busy}><ChevronLeft size={15}/></button><strong>第 {page} / {totalPages} 页</strong><button aria-label="下一页" title="下一页" onClick={() => onPageChange(Math.min(totalPages, page + 1))} disabled={page >= totalPages || busy}><ChevronRight size={15}/></button></div>
        </div>
      </section>
    </div>
  </section>;
}

function WorkspaceApp({sessionUser = null, authMode = AUTH_MODES.SELF_USE, accessPolicy = {}, onLogout, onLogin, onModeChange}) {
  const admin = isAdmin(sessionUser);
  const canManageMonitor = canManageWorkspace(sessionUser, authMode);
  const canUseReclaim = canAccessView('reclaim', sessionUser, authMode, accessPolicy);
  const canUseSub2Api = canAccessView('sub2api', sessionUser, authMode, accessPolicy);
  const readOnly = !canManageMonitor;
  const [items, setItems] = useState([]);
  const [shops, setShops] = useState([]);
  const [preorders, setPreorders] = useState([]);
  const [preorderDraft, setPreorderDraft] = useState(null);
  const [sourceMode, setSourceMode] = useState('shop');
  const [url, setUrl] = useState('');
  const [name, setName] = useState('');
  const [intervalSeconds, setIntervalSeconds] = useState(300);
  const [categoryId, setCategoryId] = useState('');
  const [goodsType, setGoodsType] = useState('card');
  const [shopCategoryState, setShopCategoryState] = useState({token: '', goodsType: 'card', categories: [], loading: false, error: ''});
  const [shopCategoryRetry, setShopCategoryRetry] = useState(0);
  const [shopFilter, setShopFilter] = useState(null);
  const [shopQuery, setShopQuery] = useState('');
  const [shopStatusFilter, setShopStatusFilter] = useState('all');
  const [productQuery, setProductQuery] = useState('');
  const [productStatusFilter, setProductStatusFilter] = useState('all');
  const [checkedIds, setCheckedIds] = useState([]);
  const [checkedShopIds, setCheckedShopIds] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [theme, setTheme] = useState(() => window.localStorage.getItem('ldxp-theme') === 'dark' ? 'dark' : 'light');
  const [overviewOpen, setOverviewOpen] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  const [history, setHistory] = useState([]);
  const [historyTrend, setHistoryTrend] = useState([]);
  const [historyMeta, setHistoryMeta] = useState({total: 0, page: 1, page_size: 25, stats: {}});
  const [historyFilters, setHistoryFilters] = useState({query: '', startDate: '', endDate: '', status: 'all', stock: 'all'});
  const [historyRequestFilters, setHistoryRequestFilters] = useState(historyFilters);
  const [historyPage, setHistoryPage] = useState(1);
  const [historyPageSize, setHistoryPageSize] = useState(25);
  const [historyBusy, setHistoryBusy] = useState(false);
  const historyRequestVersion = useRef(0);
  const [cart, setCart] = useState([]);
  const [contact, setContact] = useState({contact: '', note: ''});
  const [queryPassword, setQueryPassword] = useState('');
  const [savedCheckout, setSavedCheckout] = useState({contact: '', note: '', query_password: '', channel_id: 1, coupon_code: '', storage_mode: 'local'});
  const [couponCode, setCouponCode] = useState('');
  const [checkoutStorageMode, setCheckoutStorageMode] = useState('local');
  const [checkoutPrompt, setCheckoutPrompt] = useState(null);
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [review, setReview] = useState(null);
  const [officialOrder, setOfficialOrder] = useState(null);
  const [paymentChannel, setPaymentChannel] = useState(1);
  const [paymentChannels, setPaymentChannels] = useState([{id: 1, name: '支付宝'}]);
  const paymentWindow = useRef(null);
  const [activeView, setActiveView] = useState('products');
  const [redeemConfig, setRedeemConfig] = useState({base_url: 'https://30d.team'});
  const [cardCodes, setCardCodes] = useState('');
  const [reclaimResult, setReclaimResult] = useState(null);
  const [reclaimBusy, setReclaimBusy] = useState(false);
  const [reclaimPayload, setReclaimPayload] = useState(null);
  const [sub2apiConfig, setSub2apiConfig] = useState({base_url: 'http://127.0.0.1:8080'});
  const [sub2apiAdminKey, setSub2apiAdminKey] = useState('');
  const [sub2apiPayload, setSub2apiPayload] = useState(null);
  const [sub2apiFileName, setSub2apiFileName] = useState('');
  const [sub2apiResult, setSub2apiResult] = useState(null);
  const [sub2apiBusy, setSub2apiBusy] = useState(false);
  const [sub2apiOptionsBusy, setSub2apiOptionsBusy] = useState(false);
  const [sub2apiOptions, setSub2apiOptions] = useState({loaded: false, proxies: [], groups: [], proxy_count: 0, group_count: 0, monitor: null});
  const [sub2apiProxyChoice, setSub2apiProxyChoice] = useState('json');
  const [sub2apiGroupIds, setSub2apiGroupIds] = useState([]);
  const [sub2apiCodexFingerprintMode, setSub2apiCodexFingerprintMode] = useState('off');
  const [sub2apiReclaimBusy, setSub2apiReclaimBusy] = useState(false);
  const [sub2apiReclaimResult, setSub2apiReclaimResult] = useState(null);
  const [sub2apiRetryBusy, setSub2apiRetryBusy] = useState(false);
  const [sub2apiAutomationRetryResult, setSub2apiAutomationRetryResult] = useState(null);
  const [sub2apiReclaimOrderNos, setSub2apiReclaimOrderNos] = useState([]);
  const [sub2apiCardCodes, setSub2apiCardCodes] = useState('');
  const [sub2apiCardMode, setSub2apiCardMode] = useState('manual');
  const [sub2apiCardBusy, setSub2apiCardBusy] = useState(false);
  const [sub2apiCardFlow, setSub2apiCardFlow] = useState({stage: 'idle'});
  const [sub2apiCardHistory, setSub2apiCardHistory] = useState(EMPTY_CARD_IMPORT_HISTORY);
  const [sub2apiCardHistoryFilter, setSub2apiCardHistoryFilter] = useState('all');
  const [sub2apiCardHistoryPage, setSub2apiCardHistoryPage] = useState(1);
  const [sub2apiCardHistoryPageSize, setSub2apiCardHistoryPageSize] = useState(10);
  const [sub2apiCardHistoryBusy, setSub2apiCardHistoryBusy] = useState(false);
  const [sub2apiCardHistoryActions, setSub2apiCardHistoryActions] = useState({});
  const sub2apiCardHistoryFilterRef = useRef('all');
  const sub2apiCardHistoryPageRef = useRef(1);
  const sub2apiCardHistoryPageSizeRef = useRef(10);
  const [sub2apiAccounts, setSub2apiAccounts] = useState({items: [], total: 0, page: 1, page_size: 12, pages: 1, usage: {}, usage_errors: {}});
  const [sub2apiAccountFilters, setSub2apiAccountFilters] = useState({search: '', status: '', platform: ''});
  const [sub2apiAccountBusy, setSub2apiAccountBusy] = useState(false);
  const [sub2apiAccountError, setSub2apiAccountError] = useState('');
  const [sub2apiAccountActions, setSub2apiAccountActions] = useState({});
  const [sub2apiTestedAccounts, setSub2apiTestedAccounts] = useState({});
  const [sub2apiAccountRefresh, setSub2apiAccountRefresh] = useState({running: false, phase: 'idle', completed: 0, total: 0, failed: 0});
  const sub2apiAccountRefreshRef = useRef(false);
  const [sub2apiAutomation, setSub2apiAutomation] = useState(DEFAULT_SUB2API_AUTOMATION);
  const [sub2apiAutomationState, setSub2apiAutomationState] = useState(null);
  const [sub2apiAutomationBusy, setSub2apiAutomationBusy] = useState(false);
  const [busy, setBusy] = useState({});
  const [verificationShopId, setVerificationShopId] = useState(null);
  const [serviceOnline, setServiceOnline] = useState(false);
  const [featureLoadState, setFeatureLoadState] = useState({
    monitor: {status: 'loading', error: ''},
    reclaim: {status: 'idle', error: ''},
    sub2api: {status: 'idle', error: ''},
  });
  const [featureLoadAttempt, setFeatureLoadAttempt] = useState({monitor: 0, reclaim: 0, sub2api: 0});
  const [toast, setToast] = useState(null);
  const toastTimer = useRef(null);
  const preorderLoaded = useRef(false);
  const knownTriggeredPreorders = useRef(new Set());
  const monitorLastLoadedAt = useRef(0);
  const redeemConfigLoaded = useRef(false);
  const stableItemOrder = useStableItemOrder(items);

  useEffect(() => {
    if (!canAccessView(activeView, sessionUser, authMode, accessPolicy)) {
      setActiveView(defaultViewFor(sessionUser, authMode));
    }
  }, [activeView, authMode, sessionUser, accessPolicy]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem('ldxp-theme', theme);
  }, [theme]);

  useEffect(() => {
    let parsed;
    try {
      parsed = new URL(url);
    } catch {
      setShopCategoryState({token: '', goodsType, categories: [], loading: false, error: ''});
      return undefined;
    }
    const match = parsed.pathname.match(/^\/shop\/([A-Za-z0-9_-]{3,80})\/?$/);
    if (sourceMode !== 'shop' || parsed.protocol !== 'https:' || parsed.hostname !== 'pay.ldxp.cn' || !match) {
      setShopCategoryState({token: '', goodsType, categories: [], loading: false, error: ''});
      return undefined;
    }

    const token = match[1];
    let cancelled = false;
    setCategoryId('');
    setShopCategoryState({token, goodsType, categories: [], loading: true, error: ''});
    const timer = window.setTimeout(async () => {
      try {
        const result = await request('/shops/categories', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({url, goods_type: goodsType}),
        });
        if (!cancelled) setShopCategoryState({token: result.token || token, goodsType, categories: result.categories || [], loading: false, error: ''});
      } catch (error) {
        if (!cancelled) setShopCategoryState({token, goodsType, categories: [], loading: false, error: error.message});
      }
    }, 320);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [goodsType, shopCategoryRetry, sourceMode, url]);

  useEffect(() => () => window.clearTimeout(toastTimer.current), []);

  useEffect(() => {
    const syncTooltips = () => {
      const titleNodes = document.querySelectorAll('[title]');
      titleNodes.forEach(node => {
        const label = node.getAttribute('title');
        if (!label) {
          node.removeAttribute('title');
          return;
        }
        node.dataset.tooltip = label;
        node.dataset.tooltipPlacement = node.dataset.tooltipPlacement || 'top';
        node.classList.add('tooltip-anchor');
        node.removeAttribute('title');
      });

      document.querySelectorAll('.nav-list button, .topbar-actions button').forEach(node => {
        if (node.dataset.tooltip) return;
        const label = node.getAttribute('aria-label') || node.textContent.trim();
        if (!label) return;
        node.dataset.tooltip = label;
        node.dataset.tooltipPlacement = 'top';
        node.classList.add('tooltip-anchor');
      });
    };

    syncTooltips();
    const observer = new MutationObserver(syncTooltips);
    observer.observe(document.body, {attributes: true, attributeFilter: ['title'], childList: true, subtree: true});
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const drawerOpen = overviewOpen || detailOpen;
    document.body.classList.toggle('drawer-open', drawerOpen);
    const closeOnEscape = event => {
      if (event.key !== 'Escape') return;
      if (detailOpen) setDetailOpen(false);
      else if (overviewOpen) setOverviewOpen(false);
    };
    if (drawerOpen) window.addEventListener('keydown', closeOnEscape);
    return () => {
      document.body.classList.remove('drawer-open');
      window.removeEventListener('keydown', closeOnEscape);
    };
  }, [detailOpen, overviewOpen]);

  const notify = (content, type = 'info') => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    const normalizedContent = typeof content === 'string'
      ? content.replace(/娴嬭瘯/g, '刷新状态').replace(/閫氳繃/g, '成功').replace(/澶辫触/g, '失败')
      : content;
    const notice = typeof normalizedContent === 'string'
      ? {message: normalizedContent, type, duration: 3200}
      : {...normalizedContent, type: normalizedContent?.type || type, duration: normalizedContent?.duration || 6000};
    setToast({id, ...notice});
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => {
      setToast(current => current?.id === id ? null : current);
    }, notice.duration);
  };

  const dismissToast = () => {
    window.clearTimeout(toastTimer.current);
    toastTimer.current = null;
    setToast(null);
  };

  const setFeatureStatus = (feature, status, error = '') => {
    setFeatureLoadState(current => ({...current, [feature]: {status, error}}));
  };

  const retryFeature = feature => {
    setFeatureStatus(feature, 'idle');
    setFeatureLoadAttempt(current => ({...current, [feature]: current[feature] + 1}));
  };

  const loadSub2ApiAutomation = async ({syncSettings = false, quiet = true} = {}) => {
    try {
      const result = await request('/sub2api/automation');
      setSub2apiAutomationState(result.state || null);
      if (syncSettings && result.settings) {
        setSub2apiAutomation(result.settings);
        setSub2apiProxyChoice(result.settings.proxy_id ? `proxy:${result.settings.proxy_id}` : 'none');
        setSub2apiGroupIds(Array.isArray(result.settings.group_ids) ? result.settings.group_ids : []);
        setSub2apiCodexFingerprintMode(result.settings.codex_fingerprint_mode || 'off');
      }
      return result;
    } catch (error) {
      if (!quiet) notify(error.message, 'error');
      return null;
    }
  };

  const loadItems = async ({quiet = false} = {}) => {
    try {
      // Ordinary users can browse public monitor data, but only administrators
      // need the preorder management feed. Keep the read-only workspace from
      // depending on an admin-only endpoint when an older backend is used.
      const [data, shopData, preorderData] = await Promise.all([
        request('/watches'),
        request('/shops'),
        canManageMonitor ? request('/preorders') : Promise.resolve([]),
      ]);
      setItems(data);
      setShops(shopData);
      setCheckedShopIds(current => current.filter(id => shopData.some(shop => shop.id === id)));
      const triggeredIds = new Set(preorderData.filter(entry => entry.status === 'triggered').map(entry => entry.id));
      if (preorderLoaded.current) {
        const triggered = preorderData.find(entry => entry.status === 'triggered' && !knownTriggeredPreorders.current.has(entry.id));
        if (triggered) notify(`${triggered.title} 已创建支付链接`);
      }
      knownTriggeredPreorders.current = triggeredIds;
      preorderLoaded.current = true;
      setPreorders(preorderData);
      setServiceOnline(true);
      setSelectedId(current => current ?? data[0]?.id ?? null);
      monitorLastLoadedAt.current = Date.now();
      return {ok: true};
    } catch (error) {
      setServiceOnline(false);
      if (!quiet) notify(error.message, 'error');
      return {ok: false, error: error.message};
    }
  };

  const loadHistory = async id => {
    const requestVersion = ++historyRequestVersion.current;
    if (!id) {
      setHistory([]);
      setHistoryTrend([]);
      setHistoryMeta({total: 0, page: 1, page_size: historyPageSize, stats: {}});
      setHistoryBusy(false);
      return;
    }
    setHistoryBusy(true);
    try {
      const params = new URLSearchParams({
        limit: String(historyPageSize),
        offset: String((historyPage - 1) * historyPageSize),
        status: historyRequestFilters.status,
        stock: historyRequestFilters.stock,
      });
      if (historyRequestFilters.query.trim()) params.set('query', historyRequestFilters.query.trim());
      if (historyRequestFilters.startDate) params.set('start_date', historyRequestFilters.startDate);
      if (historyRequestFilters.endDate) params.set('end_date', historyRequestFilters.endDate);
      const payload = await request(`/watches/${id}/history?${params.toString()}`);
      if (requestVersion !== historyRequestVersion.current) return;
      const result = Array.isArray(payload)
        ? {items: payload, trend: payload, total: payload.length, page: 1, page_size: payload.length || historyPageSize, stats: {}}
        : payload;
      setHistory(result.items || []);
      setHistoryTrend(result.trend || result.items || []);
      setHistoryMeta({total: Number(result.total || 0), page: Number(result.page || historyPage), page_size: Number(result.page_size || historyPageSize), stats: result.stats || {}});
    } catch {
      if (requestVersion !== historyRequestVersion.current) return;
      setHistory([]);
      setHistoryTrend([]);
      setHistoryMeta({total: 0, page: historyPage, page_size: historyPageSize, stats: {}});
    } finally {
      if (requestVersion === historyRequestVersion.current) setHistoryBusy(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    setFeatureStatus('monitor', 'loading');
    const loadCheckout = async () => {
      try {
        const config = await request('/settings/checkout');
        if (cancelled) return;
        let browserConfig = null;
        try {
          const raw = window.localStorage.getItem(CHECKOUT_PROFILE_KEY);
          browserConfig = raw ? JSON.parse(raw) : null;
        } catch {
          browserConfig = null;
        }
        const selected = browserConfig?.storage_mode === 'browser' ? browserConfig : config;
        const profile = {
          contact: selected.contact || '',
          note: selected.note || '',
          query_password: selected.query_password || '',
          channel_id: Number(selected.channel_id || 1),
          coupon_code: selected.coupon_code || '',
          storage_mode: selected.storage_mode === 'browser' ? 'browser' : 'local',
        };
        setContact({contact: profile.contact, note: profile.note});
        setQueryPassword(profile.query_password);
        setPaymentChannel(profile.channel_id);
        setCouponCode(profile.coupon_code);
        setCheckoutStorageMode(profile.storage_mode);
        setSavedCheckout(profile);
      } catch {
        // Checkout settings are optional for browsing the monitoring workspace.
      }
    };
    Promise.all([loadItems(), loadCheckout()]).then(([result]) => {
      if (!cancelled) setFeatureStatus('monitor', result.ok ? 'ready' : 'error', result.error || '');
    });
    return () => { cancelled = true; };
  }, [canManageMonitor, featureLoadAttempt.monitor]);

  useEffect(() => {
    const feature = activeView === 'reclaim' || activeView === 'sub2api' ? activeView : null;
    if (!feature || featureLoadState[feature].status !== 'idle') return undefined;
    // A permission change can render this effect once before the navigation
    // guard switches away from a now-inaccessible view. Avoid issuing a
    // request that would immediately expire the user's session.
    if ((feature === 'reclaim' && !canUseReclaim) || (feature === 'sub2api' && !canUseSub2Api)) {
      return undefined;
    }
    FEATURE_MODULE_LOADERS[feature]();
    setFeatureStatus(feature, 'loading');
    const loadFeatureData = async () => {
      try {
        if (feature === 'reclaim') {
          if (!redeemConfigLoaded.current) {
            const config = await request('/redeem/config');
            setRedeemConfig(config);
            redeemConfigLoaded.current = true;
          }
        } else {
          const [redeem, config, automationResult] = await Promise.all([
            canUseReclaim && !redeemConfigLoaded.current ? request('/redeem/config') : Promise.resolve(null),
            // External ordinary users can import through the server-side
            // configuration, but must not depend on the admin configuration
            // endpoint just to render the import surface.
            canManageMonitor ? request('/sub2api/config') : Promise.resolve(null),
            canManageMonitor ? request('/sub2api/automation') : Promise.resolve({state: null}),
          ]);
          if (redeem) {
            setRedeemConfig(redeem);
            redeemConfigLoaded.current = true;
          }
          if (config) setSub2apiConfig(config);
          setSub2apiAutomationState(automationResult.state || null);
          if (automationResult.settings) {
            setSub2apiAutomation(automationResult.settings);
            setSub2apiProxyChoice(automationResult.settings.proxy_id ? `proxy:${automationResult.settings.proxy_id}` : 'none');
            setSub2apiGroupIds(Array.isArray(automationResult.settings.group_ids) ? automationResult.settings.group_ids : []);
            setSub2apiCodexFingerprintMode(automationResult.settings.codex_fingerprint_mode || 'off');
          }
        }
        setFeatureStatus(feature, 'ready');
      } catch (error) {
        setFeatureStatus(feature, 'error', error.message);
      }
    };
    loadFeatureData();
    return undefined;
  }, [activeView, featureLoadAttempt.reclaim, featureLoadAttempt.sub2api, canUseReclaim, canUseSub2Api, canManageMonitor]);

  useEffect(() => {
    sub2apiCardHistoryFilterRef.current = sub2apiCardHistoryFilter;
    sub2apiCardHistoryPageRef.current = sub2apiCardHistoryPage;
    sub2apiCardHistoryPageSizeRef.current = sub2apiCardHistoryPageSize;
    if (activeView === 'sub2api' && featureLoadState.sub2api.status === 'ready') {
      loadSub2ApiCardHistory({
        status: sub2apiCardHistoryFilter,
        page: sub2apiCardHistoryPage,
        pageSize: sub2apiCardHistoryPageSize,
      });
    }
  }, [activeView, featureLoadState.sub2api.status, sub2apiCardHistoryFilter, sub2apiCardHistoryPage, sub2apiCardHistoryPageSize]);

  useEffect(() => {
    if (activeView !== 'sub2api' || featureLoadState.sub2api.status !== 'ready') return undefined;
    if (sub2apiConfig.admin_key_set) loadSub2ApiOptions({quiet: true});
    const stateTimer = canManageMonitor ? window.setInterval(() => loadSub2ApiAutomation(), 5000) : null;
    const monitorTimer = window.setInterval(() => {
      if (canManageMonitor && sub2apiConfig.admin_key_set) {
        loadSub2ApiOptions({quiet: true});
        loadSub2ApiAccounts({quiet: true});
      }
    }, 60000);
    return () => {
      if (stateTimer) window.clearInterval(stateTimer);
      window.clearInterval(monitorTimer);
    };
  }, [activeView, featureLoadState.sub2api.status, sub2apiConfig.base_url, sub2apiConfig.admin_key_set, canManageMonitor]);

  useEffect(() => {
    if (canManageMonitor && activeView === 'sub2api' && featureLoadState.sub2api.status === 'ready' && sub2apiConfig.admin_key_set) {
      loadSub2ApiAccounts({page: 1, quiet: true});
    }
  }, [canManageMonitor, activeView, featureLoadState.sub2api.status, sub2apiConfig.admin_key_set, sub2apiAccountFilters.search, sub2apiAccountFilters.status, sub2apiAccountFilters.platform]);

  const fastestInterval = [...items, ...shops, ...preorders.filter(entry => entry.enabled)]
    .filter(item => item.enabled)
    .reduce((fastest, item) => Math.min(fastest, Number(item.interval_seconds || 60)), 60);
  const dashboardRefreshMs = fastestInterval <= 5 ? 1000 : fastestInterval <= 30 ? 3000 : 5000;
  const monitorSurfaceActive = ['products', 'monitor', 'history'].includes(activeView) || detailOpen || overviewOpen;

  useEffect(() => {
    if (featureLoadState.monitor.status !== 'ready' || !monitorSurfaceActive) return undefined;
    if (Date.now() - monitorLastLoadedAt.current > 1000) loadItems({quiet: true});
    const timer = window.setInterval(() => loadItems({quiet: true}), dashboardRefreshMs);
    return () => window.clearInterval(timer);
  }, [dashboardRefreshMs, featureLoadState.monitor.status, monitorSurfaceActive]);

  useEffect(() => {
    const timer = window.setTimeout(() => setHistoryRequestFilters(historyFilters), 250);
    return () => window.clearTimeout(timer);
  }, [historyFilters]);

  useEffect(() => {
    const shouldLoadHistory = featureLoadState.monitor.status === 'ready' && (activeView === 'history' || detailOpen);
    if (!shouldLoadHistory) {
      historyRequestVersion.current += 1;
      setHistoryBusy(false);
      return;
    }
    loadHistory(selectedId);
  }, [activeView, detailOpen, featureLoadState.monitor.status, selectedId, items.find(item => item.id === selectedId)?.last_run, historyRequestFilters, historyPage, historyPageSize]);

  const selected = items.find(item => item.id === selectedId) || null;
  const latest = selected?.latest;
  const enabledShopIds = new Set(shops.filter(shop => shop.enabled).map(shop => shop.id));
  const itemMonitoringEnabled = item => item.shops?.length
    ? item.shops.some(shop => enabledShopIds.has(shop.id))
    : item.enabled;
  const monitored = items.filter(itemMonitoringEnabled).length;
  const saleCount = items.filter(item => item.latest?.sale_status === 'on_sale').length;
  const changes = items.filter(item => item.price_changed).length;
  const failures = items.filter(item => item.last_attempt?.status === 'error' && !itemIsUnlisted(item)).length;
  const shopFailures = shops.filter(shop => shop.last_attempt?.status === 'error').length;
  const normalizedShopQuery = shopQuery.trim().toLocaleLowerCase('zh-CN');
  const filteredShops = shops.filter(shop => {
    const matchesQuery = !normalizedShopQuery || [shop.name, shop.token, shop.category_id, shop.category_name, shop.goods_type, shopGoodsTypeLabel(shop.goods_type)]
      .some(value => String(value || '').toLocaleLowerCase('zh-CN').includes(normalizedShopQuery));
    const matchesStatus = shopStatusFilter === 'all'
      || (shopStatusFilter === 'enabled' && shop.enabled)
      || (shopStatusFilter === 'paused' && !shop.enabled)
      || (shopStatusFilter === 'error' && shop.last_attempt?.status === 'error');
    return matchesQuery && matchesStatus;
  });
  const normalizedProductQuery = productQuery.trim().toLocaleLowerCase('zh-CN');
  const shopScopedItems = shopFilter
    ? items.filter(item => item.shops?.some(shop => shop.id === shopFilter))
    : items;
  const visibleItems = shopScopedItems.filter(item => {
    const matchesQuery = !normalizedProductQuery || [
      item.latest?.title,
      item.name,
      item.url,
      ...(item.shops || []).flatMap(shop => [shop.name, shop.token]),
    ].some(value => String(value || '').toLocaleLowerCase('zh-CN').includes(normalizedProductQuery));
    const stock = itemStock(item);
    const matchesStatus = productStatusFilter === 'all'
      || (productStatusFilter === 'enabled' && itemMonitoringEnabled(item))
      || (productStatusFilter === 'paused' && !itemMonitoringEnabled(item))
      || (productStatusFilter === 'error' && item.last_attempt?.status === 'error')
      || (productStatusFilter === 'in_stock' && stock.key === 'in')
      || (productStatusFilter === 'out_of_stock' && stock.key === 'out');
    return matchesQuery && matchesStatus;
  });
  const totalCart = cart.reduce((sum, entry) => sum + entry.quantity, 0);
  const estimatedTotal = cart.reduce((sum, entry) => {
    const item = items.find(candidate => candidate.id === entry.watch_id);
    const unitPrice = Number(item?.latest?.price);
    return sum + (Number.isFinite(unitPrice) ? unitPrice : 0) * entry.quantity;
  }, 0);
  const visibleCheckedIds = visibleItems.filter(item => checkedIds.includes(item.id)).map(item => item.id);
  const allVisibleChecked = visibleItems.length > 0 && visibleCheckedIds.length === visibleItems.length;
  const validCheckedShopIds = shops.filter(shop => checkedShopIds.includes(shop.id)).map(shop => shop.id);
  const visibleCheckedShopIds = filteredShops.filter(shop => checkedShopIds.includes(shop.id)).map(shop => shop.id);
  const allVisibleShopsChecked = filteredShops.length > 0 && visibleCheckedShopIds.length === filteredShops.length;
  const preorderByWatch = new Map(preorders.map(entry => [entry.watch_id, entry]));
  const displayedPreorders = preorders.filter(entry => entry.status !== 'cancelled');

  const addWatch = async event => {
    event.preventDefault();
    setBusy(value => ({...value, add: true}));
    try {
      let isShop = sourceMode === 'shop';
      try {
        const path = new URL(url).pathname;
        if (path.startsWith('/shop/')) isShop = true;
        if (path.startsWith('/item/')) isShop = false;
      } catch {
        // The backend returns the precise URL validation error.
      }
      const result = await request(isShop ? '/shops' : '/watches', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          url,
          name,
          interval_seconds: intervalSeconds,
          category_id: isShop ? categoryId : undefined,
          category_name: isShop ? shopCategoryState.categories.find(category => String(category.id) === String(categoryId))?.name || '' : undefined,
          goods_type: isShop ? goodsType : undefined,
        }),
      });
      setName('');
      setUrl('');
      await loadItems({quiet: true});
      if (!isShop) setSelectedId(result.id);
      notify(isShop
        ? (result.summary?.status === 'success' ? `店铺已同步，导入 ${result.summary.product_count} 个商品` : '店铺已加入，首次同步暂未成功')
        : (result.snapshot?.status === 'success' ? '商品已加入并完成首次抓取' : '商品已加入，首次抓取暂未成功'));
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, add: false}));
    }
  };

  const changeSourceMode = mode => {
    setSourceMode(mode);
    setUrl('');
    if (mode === 'shop') {
      setIntervalSeconds(300);
      setCategoryId('');
    } else {
      setIntervalSeconds(60);
    }
  };

  const changeSourceUrl = value => {
    setUrl(value);
    try {
      const path = new URL(value).pathname;
      if (path.startsWith('/shop/') && sourceMode !== 'shop') {
        setSourceMode('shop');
        setIntervalSeconds(300);
        setCategoryId('');
      } else if (path.startsWith('/item/') && sourceMode !== 'item') {
        setSourceMode('item');
        setIntervalSeconds(60);
      }
    } catch {
      // Keep typing without switching modes until a complete URL is available.
    }
  };

  const fetchShop = async id => {
    setBusy(value => ({...value, [`shop-${id}`]: true}));
    try {
      const result = await request(`/shops/${id}/fetch`, {method: 'POST'});
      await loadItems({quiet: true});
      notify(`店铺同步完成，共 ${result.product_count} 个商品`);
    } catch (error) {
      await loadItems({quiet: true});
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, [`shop-${id}`]: false}));
    }
  };

  const updateShop = async (shop, patch) => {
    const next = {...shop, ...patch};
    const intervalChanged = Number(next.interval_seconds) !== Number(shop.interval_seconds);
    setShops(current => current.map(value => value.id === shop.id ? next : value));
    if (intervalChanged) {
      setItems(current => current.map(item => item.shops?.some(link => link.id === shop.id)
        ? {...item, interval_seconds: next.interval_seconds}
        : item));
    }
    setBusy(value => ({...value, [`shop-save-${shop.id}`]: true}));
    try {
      const result = await request(`/shops/${shop.id}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(next),
      });
      if (intervalChanged) {
        await loadItems({quiet: true});
        notify(`店铺频率已更新为每 ${intervalLabel(result.interval_seconds)}，同步 ${result.synced_product_count} 个商品`);
      }
    } catch (error) {
      await loadItems({quiet: true});
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, [`shop-save-${shop.id}`]: false}));
    }
  };

  const removeShop = async shop => {
    if (!window.confirm(`停止监控店铺“${shop.name || shop.token}”？已导入的商品记录会保留。`)) return;
    try {
      await request(`/shops/${shop.id}`, {method: 'DELETE'});
      if (shopFilter === shop.id) setShopFilter(null);
      setCheckedShopIds(current => current.filter(id => id !== shop.id));
      await loadItems({quiet: true});
      notify('店铺监控已删除');
    } catch (error) {
      notify(error.message, 'error');
    }
  };

  const toggleShopChecked = id => {
    setCheckedShopIds(current => current.includes(id) ? current.filter(value => value !== id) : [...current, id]);
  };

  const toggleAllVisibleShops = () => {
    const visibleIds = filteredShops.map(shop => shop.id);
    setCheckedShopIds(current => allVisibleShopsChecked
      ? current.filter(id => !visibleIds.includes(id))
      : [...new Set([...current, ...visibleIds])]);
  };

  const removeCheckedShops = async () => {
    if (!validCheckedShopIds.length) return;
    if (!window.confirm(`删除 ${validCheckedShopIds.length} 个店铺监控？已导入的商品和价格记录会保留。`)) return;
    setBusy(value => ({...value, shopBatchDelete: true}));
    try {
      const result = await request('/shops/batch-delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ids: validCheckedShopIds}),
      });
      if (validCheckedShopIds.includes(shopFilter)) setShopFilter(null);
      setCheckedShopIds([]);
      await loadItems({quiet: true});
      notify(`已删除 ${result.deleted_count} 个店铺监控，商品记录已保留`);
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, shopBatchDelete: false}));
    }
  };

  const fetchOne = async id => {
    setBusy(value => ({...value, [`fetch-${id}`]: true}));
    try {
      const result = await request(`/watches/${id}/fetch`, {method: 'POST'});
      await loadItems({quiet: true});
      await loadHistory(id);
      notify(result.price_changed
        ? `价格发生变化：${money(result.previous_price)} → ${money(result.price)}`
        : result.refresh_source === 'shop' ? `所属店铺已同步，当前库存 ${result.stock_label}` : '商品信息已更新');
    } catch (error) {
      await loadItems({quiet: true});
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, [`fetch-${id}`]: false}));
    }
  };

  const fetchAll = async () => {
    setBusy(value => ({...value, fetchAll: true}));
    try {
      const [watchResult, shopResult] = await Promise.all([
        request('/watches/fetch-all', {method: 'POST'}),
        request('/shops/fetch-all', {method: 'POST'}),
      ]);
      await loadItems({quiet: true});
      const failed = [...watchResult.results, ...shopResult.results].filter(entry => !entry.ok).length;
      notify(failed ? `刷新完成，${failed} 项抓取失败` : '店铺与商品已全部刷新', failed ? 'error' : 'info');
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, fetchAll: false}));
    }
  };

  const updateWatch = async (item, patch) => {
    const next = {...item, ...patch};
    const intervalChanged = Number(next.interval_seconds) !== Number(item.interval_seconds);
    setItems(current => current.map(candidate => candidate.id === item.id ? next : candidate));
    setBusy(value => ({...value, [`watch-save-${item.id}`]: true}));
    try {
      const result = await request(`/watches/${item.id}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: next.name, enabled: next.enabled, interval_seconds: next.interval_seconds}),
      });
      if (intervalChanged) notify(`商品频率已更新为每 ${intervalLabel(result.interval_seconds)}`);
    } catch (error) {
      await loadItems({quiet: true});
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, [`watch-save-${item.id}`]: false}));
    }
  };

  const removeWatch = async item => {
    if (!window.confirm(`停止监控并删除“${item.latest?.title || item.name || item.url}”？`)) return;
    try {
      await request(`/watches/${item.id}`, {method: 'DELETE'});
      setCart(current => current.filter(entry => entry.watch_id !== item.id));
      setCheckedIds(current => current.filter(id => id !== item.id));
      if (selectedId === item.id) {
        setSelectedId(null);
        setDetailOpen(false);
      }
      await loadItems({quiet: true});
      notify('监控商品已删除');
    } catch (error) {
      notify(error.message, 'error');
    }
  };

  const toggleChecked = id => {
    setCheckedIds(current => current.includes(id) ? current.filter(value => value !== id) : [...current, id]);
  };

  const toggleAllVisible = () => {
    const visibleIds = visibleItems.map(item => item.id);
    setCheckedIds(current => allVisibleChecked
      ? current.filter(id => !visibleIds.includes(id))
      : [...new Set([...current, ...visibleIds])]);
  };

  const copyLinks = async selectedItems => {
    if (!selectedItems.length) return;
    try {
      await navigator.clipboard.writeText(selectedItems.map(item => item.url).join('\n'));
      notify(selectedItems.length === 1 ? '商品链接已复制' : `已复制 ${selectedItems.length} 个商品链接`);
    } catch {
      notify('无法访问剪贴板，请检查浏览器权限', 'error');
    }
  };

  const removeChecked = async () => {
    if (!visibleCheckedIds.length) return;
    if (!window.confirm(`从本地监控目录移除 ${visibleCheckedIds.length} 个商品？店铺中的真实商品不会受影响。`)) return;
    setBusy(value => ({...value, batchDelete: true}));
    try {
      const result = await request('/watches/batch-delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ids: visibleCheckedIds}),
      });
      setCart(current => current.filter(entry => !visibleCheckedIds.includes(entry.watch_id)));
      if (visibleCheckedIds.includes(selectedId)) {
        setSelectedId(null);
        setDetailOpen(false);
      }
      setCheckedIds(current => current.filter(id => !visibleCheckedIds.includes(id)));
      await loadItems({quiet: true});
      notify(`已从本地目录移除 ${result.deleted_count} 个商品`);
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, batchDelete: false}));
    }
  };

  const openPreorder = async () => {
    const selectedItems = visibleItems.filter(item => visibleCheckedIds.includes(item.id));
    if (!selectedItems.length) return;
    setBusy(value => ({...value, preorderRefresh: true}));
    try {
      const refreshed = await request('/watches/inventory-refresh', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ids: selectedItems.map(item => item.id)}),
      });
      const byId = new Map(refreshed.results.map(entry => [entry.id, entry]));
      const failures = refreshed.results.filter(entry => !entry.ok);
      if (failures.length === refreshed.results.length) throw new Error(failures[0]?.error || '库存刷新失败');
      setPreorderDraft({
        enabled: false,
        interval_seconds: 1,
        items: selectedItems.map(item => {
          const entry = byId.get(item.id);
          const product = entry?.ok ? entry.data : null;
          const unlisted = itemIsUnlisted(item)
            || product?.sale_status === 'off_sale'
            || UNLISTED_ERROR_MARKERS.some(marker => String(entry?.error || '').includes(marker));
          const minimum = Math.max(1, Number(product?.limit_count || item.latest?.limit_count || 1));
          return {
            watch_id: item.id,
            title: product?.title || item.latest?.title || item.name || `商品 ${item.id}`,
            stock: unlisted ? null : product?.stock ?? null,
            stock_label: unlisted ? '未上架' : product?.stock_label || (entry?.error ? '库存刷新失败' : '数量待获取'),
            sale_status: unlisted ? 'off_sale' : product?.sale_status || item.latest?.sale_status,
            minimum,
            quantity: minimum,
          };
        }),
      });
      await loadItems({quiet: true});
      notify(failures.length ? `库存同步完成，${failures.length} 个商品刷新失败` : '已通过店铺同步最新库存', failures.length ? 'error' : 'info');
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, preorderRefresh: false}));
    }
  };

  const updatePreorderQuantity = (watchId, quantity) => {
    setPreorderDraft(current => ({
      ...current,
      items: current.items.map(entry => entry.watch_id === watchId
        ? {...entry, quantity: Math.max(entry.minimum, Math.min(99, Number(quantity) || entry.minimum))}
        : entry),
    }));
  };

  const savePreorders = async () => {
    const eligible = preorderDraft.items.filter(entry => entry.sale_status === 'on_sale' && Number(entry.stock) === 0 && entry.stock !== null);
    if (!preorderDraft.enabled) return notify('请先勾选启用自动预购', 'error');
    if (!eligible.length) return notify('所选商品没有可预购的缺货商品', 'error');
    setBusy(value => ({...value, preorder: true}));
    try {
      await request('/preorders', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          enabled: true,
          interval_seconds: preorderDraft.interval_seconds,
          items: eligible.map(entry => ({watch_id: entry.watch_id, quantity: entry.quantity})),
          ...(checkoutStorageMode === 'browser' ? {checkout_profile: {...savedCheckout, contact: contact.contact || savedCheckout.contact, query_password: queryPassword || savedCheckout.query_password, channel_id: paymentChannel}} : {}),
        }),
      });
      setPreorderDraft(null);
      setCheckedIds([]);
      await loadItems({quiet: true});
      notify(`已启用 ${eligible.length} 个自动预购监控`);
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, preorder: false}));
    }
  };

  const cancelPreorder = async preorder => {
    if (!window.confirm(`停止“${preorder.title}”的自动预购？`)) return;
    try {
      await request(`/preorders/${preorder.id}`, {method: 'DELETE'});
      await loadItems({quiet: true});
      notify('自动预购已停止');
    } catch (error) {
      notify(error.message, 'error');
    }
  };

  const addToCart = item => {
    if (!itemPurchasable(item)) return notify(itemStock(item).key === 'out' ? '该商品当前缺货，可设置自动预购' : '该商品当前不可加入清单', 'error');
    const minimum = Math.max(1, Number(item.latest.limit_count || 1));
    setCart(current => {
      const exists = current.find(entry => entry.watch_id === item.id);
      if (exists) return current;
      return [...current, {watch_id: item.id, quantity: minimum}];
    });
    notify('已加入购买清单');
  };

  const setQuantity = (watchId, quantity) => {
    const item = items.find(candidate => candidate.id === watchId);
    const minimum = Math.max(1, Number(item?.latest?.limit_count || 1));
    setCart(current => current.map(entry => entry.watch_id === watchId
      ? {...entry, quantity: Math.max(minimum, Math.min(99, Number(quantity) || minimum))}
      : entry));
  };

  const startBrowserVerification = async shop => {
    setBusy(value => ({...value, [`verify-${shop.id}`]: true}));
    try {
      const result = await request(`/shops/${shop.id}/browser-verification/start`, {method: 'POST'});
      if (result.status === 'success') {
        setVerificationShopId(null);
        await loadItems({quiet: true});
        notify(`浏览器会话同步完成，共 ${result.summary.product_count} 个商品`);
      } else {
        setVerificationShopId(shop.id);
        notify(result.detail);
      }
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, [`verify-${shop.id}`]: false}));
    }
  };

  const completeBrowserVerification = async shop => {
    setBusy(value => ({...value, [`verify-${shop.id}`]: true}));
    try {
      const result = await request(`/shops/${shop.id}/browser-verification/complete`, {method: 'POST'});
      if (result.status === 'awaiting_verification') {
        return notify(result.detail, 'error');
      }
      setVerificationShopId(null);
      await loadItems({quiet: true});
      notify(`验证通过并同步完成，共 ${result.summary.product_count} 个商品`);
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, [`verify-${shop.id}`]: false}));
    }
  };

  const persistCheckout = async (overrides = {}) => {
    const profile = {
      contact: String(overrides.contact ?? contact.contact ?? '').trim(),
      note: String(overrides.note ?? contact.note ?? '').trim(),
      query_password: String(overrides.query_password ?? queryPassword ?? ''),
      channel_id: Number(overrides.channel_id ?? paymentChannel ?? 1),
      coupon_code: String(overrides.coupon_code ?? couponCode ?? '').trim(),
      storage_mode: (overrides.storage_mode !== undefined ? overrides.storage_mode : checkoutStorageMode) === 'browser' ? 'browser' : 'local',
    };
    if (!profile.contact) throw new Error('请先填写联系方式');
    if (profile.storage_mode === 'browser') {
      window.localStorage.setItem(CHECKOUT_PROFILE_KEY, JSON.stringify(profile));
    } else {
      await request('/settings/checkout', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(profile),
      });
      window.localStorage.removeItem(CHECKOUT_PROFILE_KEY);
    }
    setContact({contact: profile.contact, note: profile.note});
    setQueryPassword(profile.query_password);
    setPaymentChannel(profile.channel_id);
    setCouponCode(profile.coupon_code);
    setCheckoutStorageMode(profile.storage_mode);
    setSavedCheckout(profile);
    return profile;
  };

  const saveCheckout = async () => {
    try {
      await persistCheckout();
      notify(checkoutStorageMode === 'browser' ? '购买配置已保存到浏览器缓存' : '购买配置已保存到本机');
    } catch (error) {
      notify(error.message, 'error');
    }
  };

  const requiresCheckoutPassword = target => {
    if (target?.latest?.query_password_required) return true;
    if (target?.items) return target.items.some(entry => entry.query_password_required);
    return cart.some(entry => items.find(item => item.id === entry.watch_id)?.latest?.query_password_required);
  };

  const ensureCheckoutProfile = target => {
    const profile = {
      ...savedCheckout,
      contact: contact.contact || savedCheckout.contact,
      note: contact.note || savedCheckout.note,
      query_password: queryPassword || savedCheckout.query_password,
      channel_id: paymentChannel || savedCheckout.channel_id,
      coupon_code: couponCode || savedCheckout.coupon_code,
    };
    if (!profile.contact || (requiresCheckoutPassword(target) && !profile.query_password)) {
      setCheckoutPrompt({target, requiresPassword: requiresCheckoutPassword(target)});
      return false;
    }
    return true;
  };

  const prepareCheckout = async (profileOverride = null) => {
    if (!cart.length) return notify('请先加入商品', 'error');
    if (!profileOverride && !ensureCheckoutProfile({items: cart.map(entry => ({...entry, query_password_required: items.find(item => item.id === entry.watch_id)?.latest?.query_password_required}))})) return;
    setBusy(value => ({...value, checkout: true}));
    try {
      const result = await request('/checkout/prepare', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({items: cart}),
      });
      setOfficialOrder(null);
      setReview(result);
      const token = result.items?.[0]?.shop_token;
      if (token) {
        request(`/pay/channels?token=${encodeURIComponent(token)}`)
          .then(channels => {
            if (Array.isArray(channels) && channels.length) {
              setPaymentChannels(channels);
              const preferred = channels.some(channel => Number(channel.id) === Number(paymentChannel))
                ? paymentChannel
                : Number(channels[0].id);
              setPaymentChannel(preferred);
            }
          })
          .catch(() => {});
      }
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, checkout: false}));
    }
  };

  const placeOfficialOrder = async (checkout, config, openedWindow) => {
    if (!checkout?.items?.length || checkout.items.length !== 1) throw new Error('官方支付一次仅支持一个商品');
    const item = checkout.items[0];
    if (!item.shop_token) throw new Error('未找到店铺 Token，请先从店铺同步商品');
    if (!config.contact) throw new Error('请先填写并保存联系方式');
    if (item.query_password_required && !config.query_password) throw new Error('该商品需要查询密码，请先填写并保存');
    const identity = await request(`/pay/juuid?token=${encodeURIComponent(item.shop_token)}`);
    const result = await request('/pay/order', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        items: [{goods_key: item.goods_key || String(item.watch_id), quantity: item.quantity}],
        channel_id: config.channel_id,
        contact: config.contact,
        query_password: item.query_password_required ? config.query_password : '',
        coupon_code: config.coupon_code || '',
        juuid: identity.juuid,
        referer: item.official_url,
        visitor_id: getVisitorId(),
      }),
    });
    setOfficialOrder({...result, identity_source: identity.source});
    if (openedWindow && !openedWindow.closed) {
      openedWindow.location.href = result.payment_url;
      openedWindow.focus();
    } else {
      window.open(result.payment_url, '_blank', 'noopener,noreferrer');
    }
    notify(`官方订单 ${result.trade_no} 已创建，支付页已打开`);
    return result;
  };

  const createOfficialOrder = async () => {
    if (!review?.items?.length) return;
    paymentWindow.current = window.open('', '_blank');
    if (paymentWindow.current) paymentWindow.current.opener = null;
    setBusy(value => ({...value, officialOrder: true}));
    try {
      await placeOfficialOrder(review, {
        ...contact,
        query_password: queryPassword,
        coupon_code: couponCode,
        channel_id: paymentChannel,
      }, paymentWindow.current);
    } catch (error) {
      if (paymentWindow.current && !paymentWindow.current.closed) paymentWindow.current.close();
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, officialOrder: false}));
    }
  };

  const oneClickBuy = async (item, profileOverride = null) => {
    if (!itemPurchasable(item)) return notify(itemStock(item).key === 'out' ? '该商品当前缺货，可设置自动预购' : '该商品当前不可购买', 'error');
    if (!profileOverride && !ensureCheckoutProfile(item)) return;
    const minimum = Math.max(1, Number(item.latest?.limit_count || 1));
    const openedWindow = window.open('', '_blank');
    if (openedWindow) openedWindow.opener = null;
    setBusy(value => ({...value, [`buy-${item.id}`]: true}));
    try {
      const checkout = await request('/checkout/prepare', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({items: [{watch_id: item.id, quantity: minimum}]}),
      });
      const channels = await request(`/pay/channels?token=${encodeURIComponent(checkout.items[0].shop_token)}`);
      const channelId = channels.some(channel => Number(channel.id) === Number(savedCheckout.channel_id))
        ? Number(savedCheckout.channel_id)
        : Number(channels[0]?.id || 1);
      setReview(checkout);
      setPaymentChannels(channels);
      setPaymentChannel(channelId);
      const profile = profileOverride || {...savedCheckout, contact: contact.contact || savedCheckout.contact, query_password: queryPassword || savedCheckout.query_password, coupon_code: couponCode || savedCheckout.coupon_code};
      await placeOfficialOrder(checkout, {...profile, channel_id: channelId}, openedWindow);
    } catch (error) {
      if (openedWindow && !openedWindow.closed) openedWindow.close();
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, [`buy-${item.id}`]: false}));
    }
  };

  const submitCheckoutPrompt = async () => {
    try {
      const profile = await persistCheckout();
      const target = checkoutPrompt?.target;
      setCheckoutPrompt(null);
      if (target?.id) {
        await oneClickBuy(target, profile);
      } else if (cart.length) {
        await prepareCheckout(profile);
      }
      return profile;
    } catch (error) {
      notify(error.message, 'error');
      return null;
    }
  };

  const openOfficial = async item => {
    window.open(item.official_url, '_blank', 'noopener,noreferrer');
    const lines = [
      contact.contact && `联系方式：${contact.contact}`,
      queryPassword && `查询密码：${queryPassword}`,
      contact.note && `备注：${contact.note}`,
      `购买数量：${item.quantity}`,
    ].filter(Boolean);
    if (lines.length) {
      try {
        await navigator.clipboard.writeText(lines.join('\n'));
        notify('购买资料已复制，请在官方页面核对');
      } catch {
        notify('无法访问剪贴板，请手动填写购买资料', 'error');
      }
    }
  };

  const copyPaymentLink = async item => {
    try {
      await navigator.clipboard.writeText(item.official_url);
      notify('官方支付链接已复制');
    } catch {
      notify('无法访问剪贴板，请手动复制链接', 'error');
    }
  };

  const normalizedCardCodes = () => [...new Set(cardCodes.split(/[\s,，]+/).map(value => value.trim()).filter(Boolean))];

  const runReclaim = async (action, overrideCodes = null) => {
    const codes = Array.isArray(overrideCodes)
      ? [...new Set(overrideCodes.map(value => String(value).trim()).filter(Boolean))]
      : normalizedCardCodes();
    if (!codes.length) return notify('请先输入卡密，每行一个', 'error');
    if (codes.length > 100) return notify('一次最多检测 100 个卡密', 'error');
    setReclaimBusy(true);
    try {
      const path = action === 'health' ? '/redeem/health-check' : action === 'reclaim' ? '/redeem/reclaim' : '/redeem/progress';
      const result = await request(path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({card_codes: codes, mode: '401'}),
      });
      setReclaimResult({...result, reclaim_action: action});
      if (action === 'reclaim') notify('401 找回任务已提交');
      else if (action === 'progress') notify('找回进度已刷新');
      else notify(`检测完成：${result.need_reclaim || 0} 个需要找回`);
    } catch (error) {
      const failed = {
        ...reclaimErrorResult(error, action === 'reclaim' ? reclaimResult || {} : {}),
        reclaim_action: action,
      };
      setReclaimResult(failed);
      notify(failed.recovery_message, 'error');
    } finally {
      setReclaimBusy(false);
    }
  };

  const stageRecoveredPayloads = (payloads, {download = true} = {}) => {
    const usable = (payloads || []).filter(item => item?.data && Array.isArray(item.data.accounts) && item.data.accounts.length);
    if (!usable.length) return null;
    const merged = {
      type: 'sub2api-data',
      version: 1,
      accounts: usable.flatMap(item => item.data.accounts),
      proxies: usable.flatMap(item => Array.isArray(item.data.proxies) ? item.data.proxies : []),
    };
    const filename = usable.length === 1
      ? (usable[0].filename || '找回结果.json')
      : `找回结果-${new Date().toISOString().replace(/[:.]/g, '-')}.json`;
    setReclaimPayload(merged);
    setSub2apiPayload(merged);
    setSub2apiFileName(filename);
    setSub2apiResult(null);
    const orderNos = [...new Set(usable.map(item => item?.task?.order_no).filter(Boolean).map(String))].slice(0, 100);
    setSub2apiReclaimOrderNos(orderNos);
    if (download) {
      const original = usable.length === 1 && usable[0].content_base64
        ? Uint8Array.from(window.atob(usable[0].content_base64), value => value.charCodeAt(0))
        : JSON.stringify(merged, null, 2);
      const blob = new Blob([original], {type: 'application/json'});
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    }
    return {accounts: merged.accounts.length, files: usable.length, filename, payload: merged, orderNos};
  };

  const downloadReclaimed = async task => {
    try {
      const result = await request('/redeem/download', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({order_no: task.order_no, download_token: task.download_token}),
      });
      const staged = stageRecoveredPayloads([result]);
      if (!staged) throw new Error('找回文件不包含可导入账号');
      notify(`找回 JSON 已下载，${staged.accounts} 个账号已放入一键导入区`);
    } catch (error) {
      notify(error.message, 'error');
    }
  };

  const saveRedeemConfig = async () => {
    try {
      const saved = await request('/redeem/config', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(redeemConfig),
      });
      setRedeemConfig(saved);
      notify('401 服务地址已保存');
    } catch (error) {
      notify(error.message, 'error');
    }
  };

  const loadSub2ApiFiles = async fileList => {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    if (files.length > 50) return notify('一次最多导入 50 个 JSON 文件', 'error');
    if (files.some(file => file.size > 8 * 1024 * 1024)) return notify('单个 JSON 文件不能超过 8 MB', 'error');
    if (files.reduce((total, file) => total + file.size, 0) > 24 * 1024 * 1024) return notify('JSON 文件总大小不能超过 24 MB', 'error');
    try {
      const parsedFiles = await Promise.all(files.map(async file => {
        const parsed = JSON.parse(await file.text());
        if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.accounts)) throw new Error(`${file.name} 必须包含 accounts 数组`);
        if (!parsed.accounts.length) throw new Error(`${file.name} 的 accounts 不能为空`);
        if (parsed.proxies !== undefined && !Array.isArray(parsed.proxies)) throw new Error(`${file.name} 的 proxies 必须是数组`);
        return parsed;
      }));
      const merged = {
        type: 'sub2api-data',
        version: 1,
        accounts: parsedFiles.flatMap(item => item.accounts),
        proxies: parsedFiles.flatMap(item => Array.isArray(item.proxies) ? item.proxies : []),
      };
      if (merged.accounts.length > 5000 || merged.proxies.length > 5000) throw new Error('合并后的账号或代理数量不能超过 5000 个');
      setSub2apiPayload(merged);
      setSub2apiFileName(files.length === 1 ? files[0].name : `${files.length} 个 JSON 文件`);
      setSub2apiResult(null);
      setSub2apiReclaimOrderNos([]);
      notify(`已合并 ${files.length} 个文件、${merged.accounts.length} 个账号`);
    } catch (error) {
      setSub2apiPayload(null);
      setSub2apiFileName('');
      notify(error.message || 'JSON 文件解析失败', 'error');
    }
  };

  const parseSub2ApiFile = event => {
    loadSub2ApiFiles(event.target.files);
    event.target.value = '';
  };

  const saveSub2ApiConfig = async () => {
    try {
      const saved = await request('/sub2api/config', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({base_url: sub2apiConfig.base_url, admin_key: sub2apiAdminKey}),
      });
      setSub2apiConfig(saved);
      setSub2apiAdminKey('');
      notify('Sub2API 配置已保存');
      await loadSub2ApiOptions({quiet: true});
    } catch (error) {
      notify(error.message, 'error');
    }
  };

  const loadSub2ApiOptions = async ({quiet = false} = {}) => {
    setSub2apiOptionsBusy(true);
    try {
      const options = await request('/sub2api/options');
      setSub2apiOptions({...options, loaded: true});
      setSub2apiGroupIds(current => current.filter(id => options.groups.some(group => group.id === id)));
      setSub2apiProxyChoice(current => {
        if (current === 'json' || current === 'none') return current;
        return options.proxies.some(proxy => `proxy:${proxy.id}` === current) ? current : 'none';
      });
      if (!quiet) notify(`已加载 ${options.proxy_count} 个代理、${options.group_count} 个分组`);
      return options;
    } catch (error) {
      setSub2apiOptions(current => ({...current, loaded: false}));
      if (!quiet) notify(error.message, 'error');
      return null;
    } finally {
      setSub2apiOptionsBusy(false);
    }
  };

  const loadSub2ApiAccounts = async ({page = 1, quiet = false} = {}) => {
    if (!sub2apiConfig.admin_key_set) return null;
    setSub2apiAccountBusy(true);
    setSub2apiAccountError('');
    try {
      const params = new URLSearchParams({
        page: String(page),
        page_size: String(sub2apiAccounts.page_size || 12),
        search: sub2apiAccountFilters.search.trim(),
        status: sub2apiAccountFilters.status,
        platform: sub2apiAccountFilters.platform,
      });
      const result = await request(`/sub2api/accounts?${params.toString()}`);
      setSub2apiAccounts(result);
      return result;
    } catch (error) {
      const message = error.status === 404
        ? '账号列表接口未加载，请重启 LDXP 服务后刷新页面'
        : error.message;
      setSub2apiAccountError(message);
      if (!quiet) notify(message, 'error');
      return null;
    } finally {
      if (!sub2apiAccountRefreshRef.current) setSub2apiAccountBusy(false);
    }
  };

  const testSub2ApiAccount = async account => {
    if (sub2apiAccountRefreshRef.current) return;
    const id = Number(account?.id);
    if (!Number.isInteger(id) || id < 1) return;
    setSub2apiAccountActions(current => ({...current, [id]: 'test'}));
    try {
      const tested = await request(`/sub2api/accounts/${id}/test`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
      setSub2apiTestedAccounts(current => ({...current, [id]: {...tested, tested_at: new Date().toISOString()}}));
      notify(tested.ok
        ? `${account.name || `账号 ${id}`} 测试通过，状态已同步`
        : `${account.name || `账号 ${id}`} 测试失败：${tested.message || 'Sub2API 未返回原因'}`,
      tested.ok ? 'info' : 'error');
    } catch (error) {
      setSub2apiTestedAccounts(current => ({...current, [id]: {ok: false, message: error.message, tested_at: new Date().toISOString()}}));
      notify(error.message, 'error');
    } finally {
      await loadSub2ApiAccounts({page: sub2apiAccounts.page, quiet: true});
      setSub2apiAccountActions(current => ({...current, [id]: null}));
    }
  };

  const refreshAllSub2ApiAccounts = async () => {
    if (!sub2apiConfig.admin_key_set || sub2apiAccountRefreshRef.current) return;
    sub2apiAccountRefreshRef.current = true;
    const currentPage = sub2apiAccounts.page || 1;
    setSub2apiAccountRefresh({running: true, phase: 'collecting', completed: 0, total: 0, failed: 0});
    setSub2apiAccountBusy(true);
    setSub2apiAccountError('');
    try {
      const accountIds = new Set();
      let page = 1;
      let pages = 1;
      while (page <= pages) {
        const params = new URLSearchParams({page: String(page), page_size: '100'});
        const result = await request(`/sub2api/accounts?${params.toString()}`);
        (Array.isArray(result?.items) ? result.items : []).forEach(account => {
          const id = Number(account?.id);
          if (Number.isInteger(id) && id > 0) accountIds.add(id);
        });
        pages = Math.max(1, Number(result?.pages) || 1);
        page += 1;
      }
      const ids = [...accountIds];
      let completed = 0;
      let failed = 0;
      setSub2apiAccountRefresh({running: true, phase: 'refreshing', completed: 0, total: ids.length, failed: 0});
      for (const id of ids) {
        setSub2apiAccountActions(current => ({...current, [id]: 'test'}));
        try {
          const tested = await request(`/sub2api/accounts/${id}/test`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
          setSub2apiTestedAccounts(current => ({...current, [id]: {...tested, tested_at: new Date().toISOString()}}));
          if (!tested.ok) failed += 1;
        } catch (error) {
          failed += 1;
          setSub2apiTestedAccounts(current => ({...current, [id]: {ok: false, message: error.message, tested_at: new Date().toISOString()}}));
        }
        setSub2apiAccountActions(current => ({...current, [id]: null}));
        completed += 1;
        setSub2apiAccountRefresh({running: true, phase: 'refreshing', completed, total: ids.length, failed});
        if (completed < ids.length) await new Promise(resolve => window.setTimeout(resolve, 120));
      }
      await loadSub2ApiAccounts({page: currentPage, quiet: true});
      setSub2apiAccountRefresh({running: false, phase: 'complete', completed, total: ids.length, failed});
      notify(ids.length ? `已刷新 ${ids.length} 个账号状态${failed ? `，${failed} 个失败` : ''}` : '暂无可刷新的账号');
    } catch (error) {
      setSub2apiAccountRefresh({running: false, phase: 'error', completed: 0, total: 0, failed: 0});
      setSub2apiAccountError(error.message);
      notify(error.message, 'error');
    } finally {
      sub2apiAccountRefreshRef.current = false;
      setSub2apiAccountBusy(false);
    }
  };

  const copySub2ApiAccountName = async account => {
    const name = String(account?.name || `账号 ${account?.id || ''}`).trim();
    try {
      await navigator.clipboard.writeText(name);
      notify(`已复制账号名称：${name}`);
    } catch (error) {
      notify('无法访问剪贴板，请检查浏览器权限', 'error');
    }
  };

  const deleteSub2ApiAccount = async account => {
    if (sub2apiAccountRefreshRef.current) return;
    const id = Number(account?.id);
    if (!Number.isInteger(id) || id < 1) return;
    if (!window.confirm(`确定删除账号“${account.name || id}”？`)) return;
    setSub2apiAccountActions(current => ({...current, [id]: 'delete'}));
    try {
      await request(`/sub2api/accounts/${id}`, {method: 'DELETE'});
      notify('Sub2API 账号已删除');
      await Promise.all([loadSub2ApiAccounts({page: sub2apiAccounts.page, quiet: true}), loadSub2ApiOptions({quiet: true})]);
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setSub2apiAccountActions(current => ({...current, [id]: null}));
    }
  };

  const testSub2Api = async () => {
    setSub2apiBusy(true);
    try {
      const result = await request('/sub2api/test', {method: 'POST'});
      setSub2apiResult(result);
      notify(result.ok ? 'Sub2API 连接成功' : `Sub2API 返回 HTTP ${result.upstream_status}`, result.ok ? 'info' : 'error');
      if (result.ok) await loadSub2ApiOptions({quiet: true});
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setSub2apiBusy(false);
    }
  };

  const reclaimErrorResult = (error, previous = {}) => {
    const payload = error?.payload && typeof error.payload === 'object' ? error.payload : {};
    const message = payload.recovery_message || payload.detail || error?.message || '401 找回请求失败';
    return {
      ...previous,
      ...payload,
      ok: false,
      outcome: 'error',
      recovery_status: 'error',
      recovery_ok: false,
      recovery_message: message,
      detail: message,
      retryable_card_codes: Array.isArray(payload.retryable_card_codes)
        ? payload.retryable_card_codes
        : Array.isArray(previous.retryable_card_codes) ? previous.retryable_card_codes : [],
      retry_available: typeof payload.retry_available === 'boolean'
        ? payload.retry_available
        : Array.isArray(payload.retryable_card_codes)
          ? payload.retryable_card_codes.length > 0
          : Boolean(previous.retry_available),
    };
  };

  const retryLegacyReclaim = async sourceResult => {
    const model = sourceResult && Array.isArray(sourceResult.retryCodes)
      ? sourceResult
      : normalizeSub2ApiRecoveryResult(sourceResult || reclaimResult);
    if (!model.retryAvailable || !model.retryCodes.length) {
      notify('当前没有可重新找回的项目', 'info');
      return;
    }
    await runReclaim('reclaim', model.retryCodes);
  };

  const pollSub2ApiReclaim = async (initial, codes, {excludeOrderNos = [], onResult = () => {}} = {}) => {
    const progressBody = {card_codes: codes};
    if (Array.isArray(excludeOrderNos) && excludeOrderNos.length) progressBody.exclude_order_nos = excludeOrderNos;
    const polling = await pollForReclaimDownloads({
      initialResponse: initial,
      requestProgress: () => request('/sub2api/reclaim-progress', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(progressBody),
      }),
      onSnapshot: snapshot => {
        const merged = recoveryResultForProgress(initial, snapshot.response || snapshot.progress, snapshot);
        onResult(merged);
      },
    });
    const final = recoveryResultForProgress(initial, polling.response || polling.progress, {
      ...polling,
      downloads: polling.downloads,
      timeoutMs: CARD_RECLAIM_POLL_TIMEOUT_MS,
    });
    return {polling, final};
  };

  const reclaimSub2Api401 = async () => {
    setSub2apiReclaimBusy(true);
    setSub2apiReclaimResult(null);
    try {
      const initial = await request('/sub2api/reclaim-401', {method: 'POST'});
      setSub2apiReclaimResult(initial);
      const initialModel = normalizeSub2ApiRecoveryResult(initial);
      const allCodes = Array.isArray(initial.reclaim_card_codes)
        ? [...new Set(initial.reclaim_card_codes.map(value => String(value).trim()).filter(Boolean))]
        : [];
      const active = Number(initialModel.summary.active || 0);
      const needsDownload = Number(initialModel.summary.done || 0) > Number(initialModel.summary.downloaded || 0);
      const codes = active > 0
        ? allCodes
        : initialModel.retryCodes.length
          ? initialModel.retryCodes
            : needsDownload
              ? allCodes
              : [];
      const initialDownloads = Array.isArray(initial.downloaded_payloads) ? initial.downloaded_payloads : [];
      if (!initial.accounts_401) {
        notify(initialModel.recoveryMessage || '扫描完成，没有发现明确的 401 账号', initialModel.outcome === 'error' ? 'error' : 'info');
        return;
      }
      if (!codes.length) {
        const staged = stageRecoveredPayloads(initialDownloads);
        if (staged) {
          notify(`找回完成并下载 JSON，${staged.accounts} 个账号已放入一键导入区`);
          return;
        }
        notify(initialModel.recoveryMessage || `发现 ${initial.accounts_401} 个 401 账号，但没有可用卡密`, 'error');
        return;
      }

      const {polling, final} = await pollSub2ApiReclaim(initial, codes, {
        excludeOrderNos: sub2apiReclaimOrderNos,
        onResult: setSub2apiReclaimResult,
      });
      setSub2apiReclaimResult(final);
      const staged = polling.completed ? stageRecoveredPayloads(polling.downloads) : null;
      if (staged) {
        notify(`找回完成并下载 JSON，${staged.accounts} 个账号已放入一键导入区`);
      } else {
        const finalModel = normalizeSub2ApiRecoveryResult(final);
        notify(finalModel.recoveryMessage || reclaimPollingFailureMessage(polling), finalModel.retryAvailable ? 'error' : 'info');
      }
    } catch (error) {
      const failed = reclaimErrorResult(error);
      setSub2apiReclaimResult(failed);
      notify(failed.recovery_message, 'error');
    } finally {
      setSub2apiReclaimBusy(false);
    }
  };

  const retrySub2ApiReclaim = async (sourceResult, target = 'direct') => {
    const model = normalizeSub2ApiRecoveryResult(sourceResult);
    if (!model.retryAvailable || !model.retryCodes.length) {
      notify('当前没有可重新找回的项目', 'info');
      return;
    }
    setSub2apiRetryBusy(true);
    const setResult = target === 'automation' ? setSub2apiAutomationRetryResult : setSub2apiReclaimResult;
    try {
      const body = {card_codes: model.retryCodes};
      if (target === 'automation') body.persist_automation = true;
      if (sub2apiReclaimOrderNos.length) body.exclude_order_nos = sub2apiReclaimOrderNos;
      const initial = await request('/sub2api/reclaim-401/retry', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      setResult(initial);
      const {polling, final} = await pollSub2ApiReclaim(initial, model.retryCodes, {
        excludeOrderNos: sub2apiReclaimOrderNos,
        onResult: setResult,
      });
      let completed = final;
      const staged = polling.completed ? stageRecoveredPayloads(polling.downloads) : null;
      if (staged && target === 'automation' && sub2apiAutomation.auto_import) {
        try {
          const imported = await importSub2Api(staged.payload, {
            reclaimOrderNos: staged.orderNos,
            throwOnError: true,
          });
          const verification = imported?.import_verification;
          completed = {
            ...completed,
            import_status: verification?.confirmed ? 'confirmed' : 'unconfirmed',
            import_attempted: Boolean(imported),
            imported: Boolean(verification?.confirmed),
            import_result: imported || null,
            import_error: verification?.confirmed ? '' : 'Sub2API 未完全确认导入结果',
          };
        } catch (importError) {
          completed = {
            ...completed,
            import_status: 'failed',
            import_attempted: true,
            imported: false,
            import_error: importError.message,
          };
        }
      }
      setResult(completed);
      const completedModel = normalizeSub2ApiRecoveryResult(completed);
      const importSuffix = completedModel.importStatus === 'failed'
        ? `；${completedModel.importMeta.label}${completedModel.importError ? `：${completedModel.importError}` : ''}`
        : completedModel.importStatus === 'unconfirmed' ? '；自动导入未完全确认' : '';
      notify(staged
        ? `${target === 'automation' && sub2apiAutomation.auto_import ? '重新找回完成' : '重新找回完成'}，${staged.accounts} 个账号已准备就绪${importSuffix}`
        : completedModel.recoveryMessage || reclaimPollingFailureMessage(polling),
      staged && completedModel.importStatus !== 'failed' ? 'info' : completedModel.retryAvailable || completedModel.importStatus === 'failed' ? 'error' : 'info');
      if (target === 'automation') await loadSub2ApiAutomation({quiet: true});
    } catch (error) {
      const failed = reclaimErrorResult(error, sourceResult);
      setResult(failed);
      notify(failed.recovery_message, 'error');
    } finally {
      setSub2apiRetryBusy(false);
    }
  };

  const retrySub2Api401 = () => retrySub2ApiReclaim(sub2apiReclaimResult, 'direct');
  const retrySub2ApiAutomation = () => retrySub2ApiReclaim(sub2apiAutomationRetryResult || sub2apiAutomationState?.last_result, 'automation');
  const manualSub2ApiAccountReclaim = async account => {
    const directCode = String(account?.card_code || '').trim();
    const name = String(account?.name || '').trim();
    const match = name.match(/(?:^|\s)([A-Za-z0-9][A-Za-z0-9-]{3,127})$/);
    const cardCode = directCode || (match ? match[1] : '');
    if (!cardCode) {
      notify('该异常账号未找到可用卡密，请检查账号名称末尾是否包含卡密', 'error');
      return;
    }
    await retrySub2ApiReclaim({
      ok: true,
      outcome: 'failed',
      recovery_status: 'failed',
      recovery_message: `手动找回账号：${name || cardCode}`,
      reclaim_card_codes: [cardCode],
      retryable_card_codes: [cardCode],
      retry_available: true,
      reclaim_summary: {active: 0, failed: 1},
    }, 'direct');
    await loadSub2ApiOptions({quiet: true});
  };

  const saveSub2ApiAutomation = async () => {
    setSub2apiAutomationBusy(true);
    try {
      const proxyId = sub2apiProxyChoice.startsWith('proxy:') ? Number(sub2apiProxyChoice.slice(6)) : null;
      const result = await request('/sub2api/automation', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          ...sub2apiAutomation,
          proxy_id: proxyId,
          group_ids: sub2apiGroupIds,
          codex_fingerprint_mode: sub2apiCodexFingerprintMode,
        }),
      });
      setSub2apiAutomation(result.settings);
      setSub2apiAutomationState(result.state);
      notify(buildSub2ApiAutomationSaveNotice(result.settings));
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setSub2apiAutomationBusy(false);
    }
  };

  const runSub2ApiAutomation = async () => {
    setSub2apiAutomationBusy(true);
    setSub2apiAutomationRetryResult(null);
    try {
      const result = await request('/sub2api/automation/run', {method: 'POST'});
      setSub2apiAutomationState(result.state || null);
      const summary = result.result || result.state?.last_result;
      const summaryModel = normalizeSub2ApiRecoveryResult(summary);
      const verification = summary?.import_result?.import_verification;
      if (summaryModel.importStatus === 'failed' || (summaryModel.importStatus === 'unconfirmed' && verification)) {
        notify(`${summaryModel.recoveryMessage}；${summaryModel.importMeta.label}${summaryModel.importError ? `：${summaryModel.importError}` : ''}`, 'error');
      } else if (summaryModel.outcome === 'partial' || summaryModel.outcome === 'unrecoverable' || summaryModel.outcome === 'failed' || summaryModel.outcome === 'error') {
        notify(summaryModel.recoveryMessage, summaryModel.retryAvailable ? 'error' : 'info');
      } else {
        notify(summaryModel.importStatus === 'confirmed' ? '自动找回完成，账号已导入 Sub2API' : summaryModel.recoveryMessage || '401 自动监控已执行');
      }
      if (summaryModel.importStatus === 'confirmed') await loadSub2ApiAccounts({page: 1, quiet: true});
    } catch (error) {
      notify(error.message, 'error');
      await loadSub2ApiAutomation();
    } finally {
      setSub2apiAutomationBusy(false);
    }
  };

  const buildSub2ApiImportBody = (payloadOverride, reclaimOrderNos = sub2apiReclaimOrderNos) => {
    const payload = payloadOverride || sub2apiPayload || reclaimPayload;
    if (!payload) return null;
    const assignExisting = sub2apiProxyChoice !== 'json' || sub2apiGroupIds.length > 0;
    const proxyId = sub2apiProxyChoice.startsWith('proxy:') ? Number(sub2apiProxyChoice.slice(6)) : null;
    return {
      data: payload,
      assign_existing: assignExisting,
      reclaim_order_nos: reclaimOrderNos,
      proxy_id: proxyId,
      group_ids: sub2apiGroupIds,
      codex_fingerprint_mode: sub2apiCodexFingerprintMode,
      endpoint: '/api/v1/admin/accounts/data',
    };
  };

  const importSub2Api = async (payloadOverride, {reclaimOrderNos = sub2apiReclaimOrderNos, throwOnError = false, bodyOverride = null} = {}) => {
    const importBody = bodyOverride || buildSub2ApiImportBody(payloadOverride, reclaimOrderNos);
    if (!importBody) {
      notify('请先选择账号 JSON 文件或下载找回结果', 'error');
      return null;
    }
    setSub2apiBusy(true);
    try {
      const result = await request('/sub2api/import', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(importBody),
      });
      setSub2apiResult(result);
      const verification = result.import_verification;
      notify(buildSub2ApiImportNotice(result));
      if (verification?.confirmed) {
        setSub2apiPayload(null);
        setReclaimPayload(null);
        setSub2apiFileName('');
        setSub2apiReclaimOrderNos([]);
        await Promise.all([loadSub2ApiAccounts({page: 1, quiet: true}), loadSub2ApiOptions({quiet: true})]);
        return result;
      }
      if (verification) {
        await loadSub2ApiAccounts({page: sub2apiAccounts.page, quiet: true});
        return result;
      }
      return result;
    } catch (error) {
      if (throwOnError) throw error;
      notify(error.message, 'error');
      return null;
    } finally {
      setSub2apiBusy(false);
    }
  };

  const loadSub2ApiCardHistory = async ({
    status = sub2apiCardHistoryFilterRef.current,
    page = sub2apiCardHistoryPageRef.current,
    pageSize = sub2apiCardHistoryPageSizeRef.current,
    quiet = false,
  } = {}) => {
    if (!quiet) setSub2apiCardHistoryBusy(true);
    try {
      const params = new URLSearchParams({status, page: String(page), page_size: String(pageSize)});
      const result = await request(`/sub2api/card-import-records?${params}`);
      setSub2apiCardHistory(result);
      if (Number(result.page) !== sub2apiCardHistoryPageRef.current) {
        setSub2apiCardHistoryPage(Number(result.page) || 1);
      }
      return result;
    } catch (error) {
      if (!quiet) notify(error.message, 'error');
      return null;
    } finally {
      if (!quiet) setSub2apiCardHistoryBusy(false);
    }
  };

  const createSub2ApiCardHistory = async payload => {
    try {
      const record = await request('/sub2api/card-import-records', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      sub2apiCardHistoryPageRef.current = 1;
      setSub2apiCardHistoryPage(1);
      await loadSub2ApiCardHistory({page: 1, quiet: true});
      return record;
    } catch {
      return null;
    }
  };

  const updateSub2ApiCardHistory = async (recordId, payload) => {
    if (!recordId) return null;
    try {
      const record = await request(`/sub2api/card-import-records/${recordId}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      await loadSub2ApiCardHistory({quiet: true});
      return record;
    } catch {
      return null;
    }
  };

  const normalizedSub2ApiCardCodes = () => [...new Set(sub2apiCardCodes.split(/[\s,，]+/).map(value => value.trim()).filter(Boolean))];

  const copySub2ApiCardCode = async code => {
    try {
      await navigator.clipboard.writeText(String(code));
      notify(`已复制卡密：${code}`);
    } catch {
      notify('无法访问剪贴板，请检查浏览器权限', 'error');
    }
  };

  const deleteSub2ApiCardHistory = async records => {
    const batch = Array.isArray(records);
    const values = batch ? records : [records];
    const recordIds = [...new Set(values
      .map(record => Number(record?.id))
      .filter(recordId => Number.isInteger(recordId) && recordId > 0))];
    if (!recordIds.length) return false;
    const prompt = batch
      ? `确定删除选中的 ${recordIds.length} 条导入记录？删除后不可恢复。`
      : `确定删除导入记录 #${recordIds[0]}？删除后不可恢复。`;
    if (!window.confirm(prompt)) return false;

    const actionKey = batch ? 'batchDelete' : recordIds[0];
    setSub2apiCardHistoryActions(current => ({...current, [actionKey]: 'delete'}));
    try {
      const result = batch
        ? await request('/sub2api/card-import-records/batch-delete', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ids: recordIds}),
        })
        : await request('/sub2api/card-import-records/delete', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({id: recordIds[0]}),
        });
      await loadSub2ApiCardHistory({quiet: true});
      notify(batch ? `已删除 ${result.deleted_count} 条导入记录` : `导入记录 #${recordIds[0]} 已删除`);
      return true;
    } catch (error) {
      notify(sub2ApiHistoryDeleteErrorMessage(error, batch), 'error');
      await loadSub2ApiCardHistory({quiet: true});
      return false;
    } finally {
      setSub2apiCardHistoryActions(current => ({...current, [actionKey]: null}));
    }
  };

  const retrySub2ApiCardHistory = async record => {
    const recordId = Number(record?.id);
    if (!Number.isInteger(recordId) || recordId < 1 || !record?.retryable) return;
    setSub2apiCardHistoryActions(current => ({...current, [recordId]: 'retry'}));
    try {
      const result = await request(`/sub2api/card-import-records/${recordId}/retry`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{}',
      });
      setSub2apiResult(result.result || null);
      notify(result.ok ? `记录 #${recordId} 重新推送成功` : `记录 #${recordId} 重新推送后核验失败`, result.ok ? 'info' : 'error');
      await Promise.all([
        loadSub2ApiCardHistory({quiet: true}),
        result.ok ? loadSub2ApiAccounts({page: 1, quiet: true}) : Promise.resolve(),
        result.ok ? loadSub2ApiOptions({quiet: true}) : Promise.resolve(),
      ]);
    } catch (error) {
      notify(error.message, 'error');
      await loadSub2ApiCardHistory({quiet: true});
    } finally {
      setSub2apiCardHistoryActions(current => ({...current, [recordId]: null}));
    }
  };

  const runSub2ApiCardImport = async () => {
    const codes = normalizedSub2ApiCardCodes();
    if (!codes.length) return notify('请先输入卡密，每行一个', 'error');
    if (codes.length > 100) return notify('一次最多处理 100 个卡密', 'error');
    if (sub2apiCardMode === 'auto' && !sub2apiConfig.admin_key_set) return notify('自动推送前请先保存 Sub2API 管理员密钥', 'error');

    setSub2apiCardBusy(true);
    const historyRecord = await createSub2ApiCardHistory({mode: sub2apiCardMode, card_codes: codes});
    const recordId = historyRecord?.id || null;
    let staged = null;
    setSub2apiCardFlow({stage: 'verify', cardCount: codes.length, startedAt: new Date().toISOString(), recordId});
    try {
      const savedRedeem = await request('/redeem/config', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(redeemConfig),
      });
      setRedeemConfig(savedRedeem);
      const health = await request('/redeem/health-check', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({card_codes: codes}),
      });
      setSub2apiCardFlow(current => ({...current, stage: 'reclaim', health, recordId}));
      await updateSub2ApiCardHistory(recordId, {
        status: 'running',
        stage: 'reclaim',
        verified_count: Number(health.total ?? codes.length),
        message: '卡密核验完成，正在找回账号文件',
      });

      const submitted = await request('/redeem/reclaim', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({card_codes: codes, mode: 'all'}),
      });
      await updateSub2ApiCardHistory(recordId, {
        status: 'running',
        stage: 'poll',
        message: '找回任务已提交，正在轮询账号 JSON（最长 1 分钟）',
      });
      const polling = await pollForReclaimDownloads({
        initialResponse: {result: submitted},
        requestProgress: () => request('/sub2api/reclaim-progress', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({card_codes: codes}),
        }),
        onSnapshot: snapshot => {
          setSub2apiCardFlow(current => ({
            ...current,
            stage: snapshot.completed ? 'download' : 'poll',
            health,
            progress: snapshot.progress,
            downloaded: snapshot.downloadedCount,
            pollAttempts: snapshot.attempts,
            pollElapsedSeconds: Math.min(CARD_RECLAIM_POLL_TIMEOUT_SECONDS, Math.ceil(snapshot.elapsedMs / 1000)),
            pollTimeoutSeconds: CARD_RECLAIM_POLL_TIMEOUT_SECONDS,
          }));
        },
      });

      staged = polling.completed ? stageRecoveredPayloads(polling.downloads) : null;
      if (!staged) {
        const pollingError = new Error(reclaimPollingFailureMessage(polling));
        pollingError.pending = Boolean(polling.timedOut && !polling.terminal);
        throw pollingError;
      }

      if (sub2apiCardMode === 'manual') {
        const retryContext = buildSub2ApiImportBody(staged.payload, staged.orderNos);
        setSub2apiCardFlow(current => ({...current, stage: 'ready', downloaded: staged.files, accounts: staged.accounts, filename: staged.filename, recordId}));
        await updateSub2ApiCardHistory(recordId, {
          status: 'pending',
          stage: 'ready',
          downloaded_files: staged.files,
          account_count: staged.accounts,
          filename: staged.filename,
          message: '核验下载成功，等待手动推送',
          retry_context: retryContext,
        });
        notify(`已核验并下载 ${staged.accounts} 个账号，请确认后手动推送`);
        return;
      }

      const retryContext = buildSub2ApiImportBody(staged.payload, staged.orderNos);
      setSub2apiCardFlow(current => ({...current, stage: 'push', downloaded: staged.files, accounts: staged.accounts, filename: staged.filename, recordId}));
      await updateSub2ApiCardHistory(recordId, {
        status: 'running',
        stage: 'push',
        downloaded_files: staged.files,
        account_count: staged.accounts,
        filename: staged.filename,
        message: '账号文件已下载，正在自动推送',
        retry_context: retryContext,
      });
      const pushed = await importSub2Api(staged.payload, {reclaimOrderNos: staged.orderNos, throwOnError: true, bodyOverride: retryContext});
      if (!pushed) throw new Error('账号 JSON 已下载，但自动推送未完成');
      const confirmed = Boolean(pushed.import_verification?.confirmed);
      setSub2apiCardFlow(current => ({...current, stage: confirmed ? 'done' : 'ready', pushed: confirmed, pushResult: pushed}));
      await updateSub2ApiCardHistory(recordId, cardImportVerificationPatch(pushed));
    } catch (error) {
      const pending = Boolean(error.pending);
      setSub2apiCardFlow(current => ({
        ...current,
        stage: pending ? 'waiting' : 'error',
        error: pending ? '' : error.message,
        notice: pending ? error.message : '',
      }));
      await updateSub2ApiCardHistory(recordId, {
        status: pending ? 'pending' : 'failed',
        stage: pending ? 'waiting' : 'error',
        account_count: Number(staged?.accounts || 0),
        failed_count: pending ? 0 : Number(staged?.accounts || 0),
        message: error.message,
      });
      notify(error.message, pending ? 'info' : 'error');
    } finally {
      setSub2apiCardBusy(false);
    }
  };

  const pushStagedSub2ApiCards = async () => {
    if (!sub2apiPayload) return notify('当前没有待推送的卡密账号 JSON', 'error');
    const retryContext = buildSub2ApiImportBody(sub2apiPayload, sub2apiReclaimOrderNos);
    setSub2apiCardFlow(current => ({...current, stage: 'push', error: ''}));
    await updateSub2ApiCardHistory(sub2apiCardFlow.recordId, {
      status: 'running',
      stage: 'push',
      message: '正在手动推送账号文件',
      retry_context: retryContext,
    });
    let pushed;
    try {
      pushed = await importSub2Api(undefined, {throwOnError: true, bodyOverride: retryContext});
    } catch (error) {
      setSub2apiCardFlow(current => ({...current, stage: 'error', error: error.message}));
      await updateSub2ApiCardHistory(sub2apiCardFlow.recordId, {
        status: 'failed',
        stage: 'error',
        failed_count: Number(sub2apiCardFlow.accounts || 0),
        message: error.message,
      });
      notify(error.message, 'error');
      return;
    }
    const confirmed = Boolean(pushed.import_verification?.confirmed);
    setSub2apiCardFlow(current => ({...current, stage: confirmed ? 'done' : 'ready', pushed: confirmed, pushResult: pushed}));
    await updateSub2ApiCardHistory(sub2apiCardFlow.recordId, cardImportVerificationPatch(pushed));
  };

  const changeSub2ApiProxy = value => {
    setSub2apiProxyChoice(value);
    if (value === 'json') setSub2apiGroupIds([]);
  };

  const toggleSub2ApiGroup = groupId => {
    setSub2apiGroupIds(current => current.includes(groupId) ? current.filter(id => id !== groupId) : [...current, groupId]);
    if (sub2apiProxyChoice === 'json') setSub2apiProxyChoice('none');
  };

  const selectedPriceDelta = useMemo(() => {
    const prices = historyTrend.filter(point => point.status === 'success' && point.price !== '' && Number.isFinite(Number(point.price))).map(point => Number(point.price));
    if (prices.length < 2) return null;
    return prices[prices.length - 1] - prices[prices.length - 2];
  }, [historyTrend]);
  const localLowestPrice = useMemo(() => {
    const selectedCategory = selected ? itemCategory(selected) : null;
    const prices = items.filter(item => (!selectedCategory || itemCategory(item) === selectedCategory) && itemStock(item).key !== 'off').map(itemPrice).filter(value => value !== null);
    return prices.length ? Math.min(...prices) : null;
  }, [items, selected]);
  const openProductDetail = id => {
    setSelectedId(id);
    setOverviewOpen(false);
    setDetailOpen(true);
  };
  const openDirectProduct = item => window.open(item.latest?.source_url || item.url, '_blank', 'noopener,noreferrer');
  const switchView = view => {
    if (!canAccessView(view, sessionUser, authMode, accessPolicy)) {
      if (!sessionUser && onLogin) onLogin();
      return;
    }
    FEATURE_MODULE_LOADERS[view]?.();
    setOverviewOpen(false);
    setDetailOpen(false);
    setActiveView(view);
  };
  const viewMeta = {
    products: {title: '商品总览', description: '聚合监控店铺报价，快速比较最低价、库存与销售状态'},
    monitor: {title: '店铺与商品监控', description: '汇总店铺商品，追踪库存、价格与在售状态'},
    history: {title: '价格记录', description: '查看选中商品的抓取结果与价格变化'},
    orders: {title: '订单查询', description: '自动完成链动小铺验证并查看购买订单'},
    reclaim: {title: '卡密 401 找回', description: '检测并找回 30d.team 卡密关联的 401 账号'},
    sub2api: {title: 'Sub2API 账号导入', description: '使用管理员密钥将账号 JSON 导入 Sub2API'},
    settings: {title: '系统设置', description: '管理基础配置、运行模式与用户账号'},
  }[activeView];
  const resetHistoryFilters = () => setHistoryFilters({query: '', startDate: '', endDate: '', status: 'all', stock: 'all'});
  const changeHistoryPageSize = value => {
    setHistoryPageSize(value);
    setHistoryPage(1);
  };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><div className="brand-mark"><Activity size={18}/></div><div><strong>链动监控台</strong><span>LOCAL WATCH</span></div></div>
        <nav className="nav-list" aria-label="页面导航">
          <button className={activeView === 'products' ? 'active' : ''} onClick={() => switchView('products')}><CircleDollarSign size={18}/>商品总览</button>
          <button className={activeView === 'monitor' ? 'active' : ''} onClick={() => switchView('monitor')}><ListChecks size={18}/>监控面板</button>
          <button className={activeView === 'history' ? 'active' : ''} onClick={() => switchView('history')}><History size={18}/>价格记录</button>
          <button className={activeView === 'orders' ? 'active' : ''} onClick={() => switchView('orders')}><ReceiptText size={18}/>订单查询</button>
          {canUseReclaim && <button className={activeView === 'reclaim' ? 'active' : ''} onClick={() => switchView('reclaim')}><KeyRound size={18}/>401 找回</button>}
          {canUseSub2Api && <button className={activeView === 'sub2api' ? 'active' : ''} onClick={() => switchView('sub2api')}><Upload size={18}/>Sub2API 导入</button>}
          {canAccessView('settings', sessionUser, authMode, accessPolicy) && <button className={activeView === 'settings' ? 'active' : ''} onClick={() => switchView('settings')}><Settings2 size={18}/>系统设置</button>}
        </nav>
        <div className="sidebar-foot">
          <div className="sidebar-account"><span className="sidebar-account-icon"><UserCircle size={17}/></span><span><strong>{sessionUser?.display_name || sessionUser?.username || '本机用户'}</strong><small>{sessionUser ? roleLabel(sessionUser) : '自用模式访客'}</small></span></div>
          {sessionUser ? <button className="sidebar-auth-button" type="button" onClick={onLogout}><LogOut size={14}/>退出登录</button> : onLogin && <button className="sidebar-auth-button" type="button" onClick={onLogin}><LogIn size={14}/>登录管理</button>}
          <div className={`service-light ${serviceOnline ? 'online' : ''}`}><span/>{serviceOnline ? '本地服务在线' : '本地服务离线'}</div>
          <small>数据存储于本机 SQLite</small>
        </div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div><h1>{viewMeta.title}</h1><p>{viewMeta.description}</p></div>
          <div className="topbar-actions">
            {['products', 'monitor', 'history'].includes(activeView) && <button className="button secondary overview-button" onClick={() => { setDetailOpen(false); setOverviewOpen(true); }} aria-expanded={overviewOpen} aria-controls="product-overview-drawer" disabled={featureLoadState.monitor.status !== 'ready'}><PanelRight size={16}/>商品总览</button>}
            <IconButton label={theme === 'dark' ? '切换日间模式' : '切换夜间模式'} onClick={() => setTheme(current => current === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? <Sun size={16}/> : <Moon size={16}/>}</IconButton>
            {canManageMonitor && ['products', 'monitor', 'history'].includes(activeView) && <button className="button secondary refresh-all-button" onClick={fetchAll} disabled={busy.fetchAll || (!items.length && !shops.length)}>
              <RefreshCw size={16} className={busy.fetchAll ? 'spin' : ''}/>全部刷新
            </button>}
          </div>
        </header>

        {['monitor', 'history'].includes(activeView) && featureLoadState.monitor.status === 'ready' && <section className="metrics" aria-label="监控概览">
          <div><span>监控店铺</span><strong>{shops.filter(shop => shop.enabled).length}</strong><small>共 {shops.length} 个店铺</small></div>
          <div><span>当前在售</span><strong className="positive">{saleCount}</strong><small>依据公开状态</small></div>
          <div><span>商品监控</span><strong>{monitored}</strong><small>目录共 {items.length} 项</small></div>
          <div><span>抓取异常</span><strong className={failures + shopFailures ? 'negative' : ''}>{failures + shopFailures}</strong><small>{changes ? `${changes} 项价格变化` : '最近一次结果'}</small></div>
        </section>}

        {['products', 'monitor', 'history'].includes(activeView) && featureLoadState.monitor.status !== 'ready' ? (
          <FeatureLoadingState feature="监控数据" state={featureLoadState.monitor} onRetry={() => retryFeature('monitor')}/>
        ) : activeView === 'products' ? (
          <ProductOverviewView items={items} shops={shops} stableOrder={stableItemOrder} busy={busy} selectedId={selectedId} onSelect={setSelectedId} onOpenDetail={openProductDetail} onBuy={oneClickBuy} onAdd={addToCart} onDirect={openDirectProduct} onRefresh={canManageMonitor ? fetchOne : null}/>
        ) : activeView === 'monitor' ? (
          <>
            {canManageMonitor && <form className="watch-composer" onSubmit={addWatch}>
              <div className="composer-title"><div className="section-icon">{sourceMode === 'shop' ? <Store size={17}/> : <BellRing size={17}/>}</div><div><h2>添加监控</h2><div className="source-segment"><button type="button" className={sourceMode === 'shop' ? 'active' : ''} onClick={() => changeSourceMode('shop')}><Store size={13}/>整店</button><button type="button" className={sourceMode === 'item' ? 'active' : ''} onClick={() => changeSourceMode('item')}><Package size={13}/>单商品</button></div></div></div>
              <div className={`composer-fields ${sourceMode === 'shop' ? 'shop-mode' : ''}`}>
                <label className="url-field"><span>{sourceMode === 'shop' ? '店铺链接' : '商品链接'}</span><div><Link2 size={16}/><input value={url} onChange={event => changeSourceUrl(event.target.value)} placeholder={sourceMode === 'shop' ? '粘贴店铺链接' : '粘贴商品链接'} required spellCheck="false"/></div></label>
                <label><span>备注</span><input value={name} onChange={event => setName(event.target.value)} placeholder="可选"/></label>
                {sourceMode === 'shop' && <label className="shop-category-field"><span>{shopCategoryState.token ? `商品分类 · ${shopCategoryState.token}` : '商品分类'}</span><div className="shop-category-control"><select value={categoryId} onChange={event => setCategoryId(event.target.value)} aria-label="选择店铺商品分类" title={shopCategoryState.error || '按店铺公开分类选择同步范围'}><option value="">{shopCategoryState.loading ? '正在读取分类…' : shopCategoryState.error ? '全部分类（读取失败，可重试）' : shopCategoryState.token && !shopCategoryState.categories.length ? '全部分类（暂无子分类）' : '全部分类'}</option>{shopCategoryState.categories.map(category => <option value={category.id} key={category.id}>{category.name} · ID {category.id}{category.goods_count ? ` · ${category.goods_count} 件` : ''}</option>)}</select>{shopCategoryState.loading && <RefreshCw className="category-loading spin" size={14}/>} {shopCategoryState.error && <button type="button" className="icon-button category-retry" aria-label="重新读取店铺分类" title="重新读取店铺分类" onClick={() => setShopCategoryRetry(value => value + 1)}><RefreshCw size={14}/></button>}</div></label>}
                {sourceMode === 'shop' && <label><span>商品类型</span><select value={goodsType} onChange={event => setGoodsType(event.target.value)}>{SHOP_GOODS_TYPES.map(option => <option value={option.value} key={option.value}>{option.label}</option>)}</select></label>}
                <label><span>监控频率</span><select value={intervalSeconds} onChange={event => setIntervalSeconds(Number(event.target.value))}>{MONITOR_INTERVAL_OPTIONS.map(seconds => <option value={seconds} key={seconds}>{intervalOptionLabel(seconds)}</option>)}</select></label>
                <button className="button primary" disabled={busy.add}><Plus size={16}/>{busy.add ? '正在同步' : sourceMode === 'shop' ? '同步店铺' : '开始监控'}</button>
              </div>
            </form>}

            {!!shops.length && <section className={`shop-overview ${canManageMonitor ? '' : 'readonly-mode'}`}>
              <div className="section-heading"><div><h2>店铺汇总</h2><p>店铺接口按设定频率分页同步</p></div>{shopFilter && <button className="clear-filter" onClick={() => { setShopFilter(null); setCheckedIds([]); }}><X size={13}/>显示全部商品</button>}</div>
              <div className="monitor-filter-bar shop-filter-bar">
                <label className="monitor-search"><Search size={15}/><input value={shopQuery} onChange={event => setShopQuery(event.target.value)} placeholder="搜索店铺名称、Token 或分类" aria-label="搜索店铺"/></label>
                <label className="monitor-filter-select"><Filter size={14}/><select value={shopStatusFilter} onChange={event => setShopStatusFilter(event.target.value)} aria-label="筛选店铺状态"><option value="all">全部店铺</option><option value="enabled">运行中</option><option value="paused">已暂停</option><option value="error">同步异常</option></select></label>
                <span className="monitor-result-count">{filteredShops.length} / {shops.length}</span>
                {(shopQuery || shopStatusFilter !== 'all') && <IconButton label="清除店铺筛选" onClick={() => { setShopQuery(''); setShopStatusFilter('all'); }}><X size={14}/></IconButton>}
              </div>
              {canManageMonitor && <div className="shop-batch-toolbar">
                <label className="shop-select-all"><input className="select-checkbox" type="checkbox" checked={allVisibleShopsChecked} onChange={toggleAllVisibleShops} disabled={!filteredShops.length} aria-label="全选当前店铺"/><span>全选当前结果</span></label>
                <div><span>{validCheckedShopIds.length ? `已选 ${validCheckedShopIds.length} 个店铺` : '尚未选择店铺'}</span>{validCheckedShopIds.length > 0 && <IconButton label="取消选择店铺" onClick={() => setCheckedShopIds([])}><X size={14}/></IconButton>}<button className="button danger-button" onClick={removeCheckedShops} disabled={!validCheckedShopIds.length || busy.shopBatchDelete}><Trash2 size={14}/>{busy.shopBatchDelete ? '正在删除' : '批量删除'}</button></div>
              </div>}
              <div className="shop-list">{!filteredShops.length ? <div className="monitor-filter-empty"><Store size={20}/><span>没有符合条件的店铺</span></div> : filteredShops.map(shop => <div className={`shop-row ${shopFilter === shop.id ? 'selected' : ''} ${checkedShopIds.includes(shop.id) ? 'checked' : ''}`} key={shop.id}>
                {canManageMonitor && <label className="check-wrap shop-select-cell" title={`选择店铺：${shop.name || shop.token}`} onClick={event => event.stopPropagation()}><input className="select-checkbox" type="checkbox" checked={checkedShopIds.includes(shop.id)} onChange={() => toggleShopChecked(shop.id)} aria-label={`选择店铺：${shop.name || shop.token}`}/></label>}
                <button className="shop-main" onClick={() => { setShopFilter(current => current === shop.id ? null : shop.id); setCheckedIds([]); }}>
                  <span className="shop-icon"><Store size={18}/></span><span><strong>{shop.name || shop.token}</strong><small>{shop.token} · {shopGoodsTypeLabel(shop.goods_type)} · {shopCategoryLabel(shop)}</small></span>
                </button>
                <div className="shop-stat"><span>商品</span><strong>{shop.product_count}</strong></div>
                <div className="shop-stat"><span>在售</span><strong className="positive">{shop.on_sale_count}</strong></div>
                <div className="shop-stat"><span>已知库存</span><strong>{shop.total_stock ?? '--'}</strong><small>{shop.known_stock_count}/{shop.product_count} 项公开</small></div>
                <div className="shop-time" title={shop.last_attempt?.error || ''}><span className={`pill ${shop.last_attempt?.status === 'error' ? 'error' : 'live'}`}>{needsBrowserVerification(shop) ? '需要验证' : shop.last_attempt?.status === 'error' ? '同步异常' : '已同步'}</span><small>{compactTime(shop.last_attempt?.fetched_at)}</small></div>
                {canManageMonitor && <><MonitorIntervalSelect value={shop.interval_seconds} label={`修改${shop.name || shop.token}监控频率`} caption="监控频率" disabled={busy[`shop-save-${shop.id}`]} onChange={value => updateShop(shop, {interval_seconds: value})}/>
                <button className={`switch ${shop.enabled ? 'on' : ''}`} role="switch" aria-checked={shop.enabled} title={shop.enabled ? '暂停店铺监控' : '开启店铺监控'} disabled={busy[`shop-save-${shop.id}`]} onClick={() => updateShop(shop, {enabled: !shop.enabled})}><span/></button>
                <div className="row-actions">{needsBrowserVerification(shop) && (verificationShopId === shop.id ? <IconButton label="验证完成并同步" tone="verify" onClick={() => completeBrowserVerification(shop)} disabled={busy[`verify-${shop.id}`]}><ShieldCheck size={15} className={busy[`verify-${shop.id}`] ? 'spin' : ''}/></IconButton> : <IconButton label="打开浏览器验证" tone="verify" onClick={() => startBrowserVerification(shop)} disabled={busy[`verify-${shop.id}`]}><ArrowUpRight size={15} className={busy[`verify-${shop.id}`] ? 'spin' : ''}/></IconButton>)}<IconButton label="同步店铺" onClick={() => fetchShop(shop.id)} disabled={busy[`shop-${shop.id}`]}><RefreshCw size={15} className={busy[`shop-${shop.id}`] ? 'spin' : ''}/></IconButton><IconButton label="删除店铺监控" tone="danger" onClick={() => removeShop(shop)}><Trash2 size={15}/></IconButton></div></>}
              </div>)}</div>
            </section>}

            <div className="content-layout">
              <section className="monitor-panel">
                <div className="section-heading"><div><h2>{shopFilter ? `${shops.find(shop => shop.id === shopFilter)?.name || '店铺'}商品` : '商品目录'}</h2><p>{visibleItems.length ? `最近状态已同步，共 ${visibleItems.length} 项` : shopScopedItems.length ? '没有符合当前搜索和筛选条件的商品' : '添加商品或同步店铺后会显示在这里'}</p></div><span className="count-badge">{visibleItems.length}</span></div>
                <div className="monitor-filter-bar product-filter-bar">
                  <label className="monitor-search"><Search size={15}/><input value={productQuery} onChange={event => setProductQuery(event.target.value)} placeholder="搜索商品、店铺或链接" aria-label="搜索监控商品"/></label>
                  <label className="monitor-filter-select"><SlidersHorizontal size={14}/><select value={productStatusFilter} onChange={event => setProductStatusFilter(event.target.value)} aria-label="筛选商品状态"><option value="all">全部商品</option><option value="enabled">监控中</option><option value="paused">已暂停</option><option value="in_stock">有货</option><option value="out_of_stock">缺货</option><option value="error">抓取异常</option></select></label>
                  <span className="monitor-result-count">{visibleItems.length} / {shopScopedItems.length}</span>
                  {(productQuery || productStatusFilter !== 'all') && <IconButton label="清除商品筛选" onClick={() => { setProductQuery(''); setProductStatusFilter('all'); }}><X size={14}/></IconButton>}
                </div>
                {canManageMonitor && visibleCheckedIds.length > 0 && <div className="batch-toolbar"><span>已选 {visibleCheckedIds.length} 项</span><div><button className="button preorder-button" onClick={openPreorder} disabled={busy.preorderRefresh}><Clock3 size={14}/>{busy.preorderRefresh ? '正在同步库存' : '设置预购'}</button><button className="button secondary" onClick={() => copyLinks(visibleItems.filter(item => visibleCheckedIds.includes(item.id)))}><Clipboard size={14}/>复制链接</button><button className="button danger-button" onClick={removeChecked} disabled={busy.batchDelete}><Trash2 size={14}/>{busy.batchDelete ? '正在移除' : '移出本地目录'}</button></div></div>}
                {!!displayedPreorders.length && <div className="preorder-list" aria-label="自动预购任务">{displayedPreorders.map(preorder => <div className={`preorder-row ${preorder.status}`} key={preorder.id}><span className="preorder-icon"><Clock3 size={15}/></span><div className="preorder-copy"><strong>{preorder.title}</strong><small>目标 {preorder.quantity} 件 · 每 {preorder.interval_seconds} 秒检查 · 当前库存 {preorder.stock_label}</small>{preorder.last_error && <small className="negative">{preorder.last_error}</small>}</div><span className={`pill ${preorder.status === 'triggered' ? 'live' : preorder.status === 'error' ? 'error' : 'neutral'}`}>{preorder.status === 'watching' ? '预购监控中' : preorder.status === 'processing' ? '正在创建订单' : preorder.status === 'triggered' ? '支付链接已创建' : '预购失败'}</span>{preorder.payment_url ? <a className="button official preorder-pay-link" href={preorder.payment_url} target="_blank" rel="noreferrer"><ArrowUpRight size={14}/>打开支付链接</a> : preorder.status === 'watching' || preorder.status === 'error' ? <IconButton label="停止自动预购" tone="danger" onClick={() => cancelPreorder(preorder)}><X size={15}/></IconButton> : <span/>}</div>)}</div>}
                <div className="table-head">{canManageMonitor ? <label className="check-wrap" title="全选当前列表"><input className="select-checkbox" type="checkbox" checked={allVisibleChecked} onChange={toggleAllVisible}/></label> : <span/>}<span>商品</span><span>价格</span><span>库存 / 状态</span><span>监控频率</span><span>操作</span></div>
                <div className="product-list">
                  {!visibleItems.length ? <div className="empty-state"><Package size={28}/><strong>{shopScopedItems.length ? '没有匹配的商品' : '暂无商品数据'}</strong><span>{shopScopedItems.length ? '调整搜索词或状态筛选后重试' : '在上方添加店铺或商品链接'}</span></div> : visibleItems.map(item => (
                    <div className={`product-row ${selectedId === item.id ? 'selected' : ''} ${checkedIds.includes(item.id) ? 'checked' : ''}`} key={item.id} onClick={() => openProductDetail(item.id)}>
                      {canManageMonitor ? <label className="check-wrap checkbox-cell" title="选择商品" onClick={event => event.stopPropagation()}><input className="select-checkbox" type="checkbox" checked={checkedIds.includes(item.id)} onChange={() => toggleChecked(item.id)}/></label> : <span className="checkbox-cell" aria-hidden="true"/>}
                      <div className="product-cell"><ProductImage item={item}/><div className="product-copy"><strong>{item.latest?.title || item.name || '等待首次抓取'}</strong><span>{item.shops?.length ? `${item.shops[0].name || item.shops[0].token} · ${item.url}` : item.name && item.latest ? item.name : item.url}</span><small>{compactTime(item.last_attempt?.fetched_at)}</small></div></div>
                      <div className="price-cell"><strong>{money(item.latest?.price)}</strong>{item.price_changed && <span className="change-flag">有变化</span>}</div>
                      <div className="state-cell"><StatusPill item={item}/><small>{itemStockLabel(item)}</small>{preorderByWatch.get(item.id)?.status === 'watching' && <small className="preorder-state">自动预购 {preorderByWatch.get(item.id).quantity} 件</small>}</div>
                      <div className="monitor-cell" onClick={event => event.stopPropagation()}>{canManageMonitor ? <><button className={`switch ${itemMonitoringEnabled(item) ? 'on' : ''}`} role="switch" aria-checked={itemMonitoringEnabled(item)} title={item.shops?.length ? '由所属店铺统一控制' : item.enabled ? '暂停自动监控' : '开启自动监控'} disabled={busy[`watch-save-${item.id}`] || !!item.shops?.length} onClick={() => updateWatch(item, {enabled: !item.enabled})}><span/></button><MonitorIntervalSelect value={item.interval_seconds} label={`修改${item.latest?.title || item.name || '商品'}监控频率`} disabled={busy[`watch-save-${item.id}`] || !!item.shops?.length} onChange={value => updateWatch(item, {interval_seconds: value})}/><small>{item.shops?.length ? '跟随店铺' : '独立设置'}</small></> : <small>只读</small>}</div>
                      <div className="row-actions" onClick={event => event.stopPropagation()}>
                        {canManageMonitor && <IconButton label={item.shops?.length ? '同步所属店铺库存' : '立即抓取'} onClick={() => fetchOne(item.id)} disabled={busy[`fetch-${item.id}`]}><RefreshCw size={15} className={busy[`fetch-${item.id}`] ? 'spin' : ''}/></IconButton>}
                        <IconButton label="复制商品链接" onClick={() => copyLinks([item])}><Clipboard size={15}/></IconButton>
                        <IconButton label="加入购买清单" onClick={() => addToCart(item)} disabled={!itemPurchasable(item)}><ShoppingBag size={15}/></IconButton>
                        <IconButton label="使用已保存配置一键购买" tone="buy" onClick={() => oneClickBuy(item)} disabled={!itemPurchasable(item) || busy[`buy-${item.id}`]}><Zap size={15} className={busy[`buy-${item.id}`] ? 'spin' : ''}/></IconButton>
                        {canManageMonitor && <IconButton label="删除监控" tone="danger" onClick={() => removeWatch(item)}><Trash2 size={15}/></IconButton>}
                      </div>
                    </div>
                  ))}
                </div>

              </section>

              <PurchasePanel items={items} cart={cart} setCart={setCart} totalCart={totalCart} estimatedTotal={estimatedTotal} setQuantity={setQuantity} contact={contact} setContact={setContact} saveCheckout={saveCheckout} queryPassword={queryPassword} setQueryPassword={setQueryPassword} passwordVisible={passwordVisible} setPasswordVisible={setPasswordVisible} paymentChannel={paymentChannel} setPaymentChannel={setPaymentChannel} couponCode={couponCode} setCouponCode={setCouponCode} checkoutStorageMode={checkoutStorageMode} setCheckoutStorageMode={setCheckoutStorageMode} prepareCheckout={prepareCheckout} busy={busy}/>
            </div>
          </>
        ) : activeView === 'history' ? (
          <HistoryView
            items={items}
            selectedId={selectedId}
            onSelect={setSelectedId}
            history={history}
            trend={historyTrend}
            meta={historyMeta}
            filters={historyFilters}
            onFiltersChange={setHistoryFilters}
            onResetFilters={resetHistoryFilters}
            page={historyPage}
            pageSize={historyPageSize}
            onPageChange={setHistoryPage}
            onPageSizeChange={changeHistoryPageSize}
            busy={historyBusy}
          />
        ) : activeView === 'orders' ? (
          <React.Suspense fallback={<FeatureLoadingState feature="订单查询界面" state={{status: 'loading'}}/>}>
            <OrderQueryView request={request} notify={notify} initialKeywords={savedCheckout.contact} checkoutProfile={savedCheckout} onSaveCheckout={async profile => { try { const saved = await persistCheckout(profile); notify(saved.storage_mode === 'browser' ? '购买配置已保存到浏览器缓存' : '购买配置已保存到本机'); } catch (error) { notify(error.message, 'error'); throw error; } }}/>
          </React.Suspense>
        ) : activeView === 'settings' ? (
          <SettingsView request={request} user={sessionUser} mode={authMode} notify={notify} onModeChange={onModeChange} onPasswordChanged={onLogout}/>
        ) : activeView === 'reclaim' ? featureLoadState.reclaim.status !== 'ready' ? (
          <FeatureLoadingState feature="401 找回" state={featureLoadState.reclaim} onRetry={() => retryFeature('reclaim')}/>
        ) : (
          <React.Suspense fallback={<FeatureLoadingState feature="401 找回界面" state={{status: 'loading'}}/>}>
            <ReclaimView config={redeemConfig} setConfig={setRedeemConfig} cardCodes={cardCodes} setCardCodes={setCardCodes} result={reclaimResult} busy={reclaimBusy} onSave={saveRedeemConfig} onRun={runReclaim} onRetry={retryLegacyReclaim} onDownload={downloadReclaimed} canConfigure={canManageMonitor} canImport={canUseSub2Api} onImport={() => { switchView('sub2api'); if (reclaimPayload) { setSub2apiPayload(reclaimPayload); setSub2apiFileName('找回结果.json'); } }}/>
          </React.Suspense>
        ) : featureLoadState.sub2api.status !== 'ready' ? (
          <FeatureLoadingState feature="Sub2API" state={featureLoadState.sub2api} onRetry={() => retryFeature('sub2api')}/>
        ) : (
          <React.Suspense fallback={<FeatureLoadingState feature="Sub2API 界面" state={{status: 'loading'}}/>}>
            <Sub2ApiView
            canUseReclaim={canUseReclaim} canUseImport={canUseSub2Api} canConfigure={canManageMonitor}
            config={sub2apiConfig} setConfig={setSub2apiConfig} adminKey={sub2apiAdminKey} setAdminKey={setSub2apiAdminKey}
            redeemConfig={redeemConfig} setRedeemConfig={setRedeemConfig} onSaveRedeem={saveRedeemConfig}
            cardCodes={sub2apiCardCodes} onCardCodes={setSub2apiCardCodes} cardMode={sub2apiCardMode} onCardMode={setSub2apiCardMode}
            cardBusy={sub2apiCardBusy} cardFlow={sub2apiCardFlow} onRunCardImport={runSub2ApiCardImport} onPushCards={pushStagedSub2ApiCards}
            cardHistory={sub2apiCardHistory} cardHistoryFilter={sub2apiCardHistoryFilter} onCardHistoryFilter={value => { setSub2apiCardHistoryPage(1); setSub2apiCardHistoryFilter(value); }}
            cardHistoryPage={sub2apiCardHistoryPage} cardHistoryPageSize={sub2apiCardHistoryPageSize}
            onCardHistoryPage={setSub2apiCardHistoryPage} onCardHistoryPageSize={value => { setSub2apiCardHistoryPage(1); setSub2apiCardHistoryPageSize(value); }}
            cardHistoryBusy={sub2apiCardHistoryBusy} cardHistoryActions={sub2apiCardHistoryActions} onRefreshCardHistory={() => loadSub2ApiCardHistory()}
            onRetryCardHistory={retrySub2ApiCardHistory} onDeleteCardHistory={deleteSub2ApiCardHistory} onCopyCardCode={copySub2ApiCardCode}
            fileName={sub2apiFileName} payload={sub2apiPayload} result={sub2apiResult} busy={sub2apiBusy}
            optionsBusy={sub2apiOptionsBusy} options={sub2apiOptions} proxyChoice={sub2apiProxyChoice} groupIds={sub2apiGroupIds}
            codexFingerprintMode={sub2apiCodexFingerprintMode} onCodexFingerprintMode={setSub2apiCodexFingerprintMode}
             reclaimBusy={sub2apiReclaimBusy} reclaimResult={sub2apiReclaimResult} onReclaim401={reclaimSub2Api401} onRetry401={retrySub2Api401} retryBusy={sub2apiRetryBusy}
             automation={sub2apiAutomation} automationState={sub2apiAutomationState} automationRetryResult={sub2apiAutomationRetryResult} automationBusy={sub2apiAutomationBusy}
             onAutomationChange={setSub2apiAutomation} onSaveAutomation={saveSub2ApiAutomation} onRunAutomation={runSub2ApiAutomation} onRetryAutomation={retrySub2ApiAutomation}
            onSave={saveSub2ApiConfig} onTest={testSub2Api} onLoadOptions={() => loadSub2ApiOptions()} onProxyChoice={changeSub2ApiProxy}
            onToggleGroup={toggleSub2ApiGroup} onFile={parseSub2ApiFile} onFiles={loadSub2ApiFiles} onImport={() => importSub2Api()}
            accountsData={sub2apiAccounts} accountFilters={sub2apiAccountFilters} onAccountFiltersChange={setSub2apiAccountFilters}
            accountBusy={sub2apiAccountBusy} accountError={sub2apiAccountError} accountActions={sub2apiAccountActions} testedAccounts={sub2apiTestedAccounts} accountRefresh={sub2apiAccountRefresh}
            onLoadAccounts={loadSub2ApiAccounts} onRefreshAllAccounts={refreshAllSub2ApiAccounts} onTestAccount={testSub2ApiAccount} onDeleteAccount={deleteSub2ApiAccount} onCopyAccountName={copySub2ApiAccountName} onManualReclaim={manualSub2ApiAccountReclaim}
            />
          </React.Suspense>
        )}
      </main>

      <ProductOverviewDrawer id="product-overview-drawer" open={overviewOpen} items={items} shops={shops} stableOrder={stableItemOrder} busy={busy} selectedId={selectedId} shopFilter={shopFilter} onClose={() => setOverviewOpen(false)} onSelect={setSelectedId} onOpenDetail={openProductDetail} onBuy={oneClickBuy} onAdd={addToCart} onDirect={openDirectProduct} onRefresh={canManageMonitor ? fetchOne : null} onShopFilter={value => { setShopFilter(value); setCheckedIds([]); }}/>
      <ProductDetailDrawer open={detailOpen} item={selected} history={history} trend={historyTrend} historyTotal={historyMeta.total} priceDelta={selectedPriceDelta} lowestPrice={localLowestPrice} historyBusy={historyBusy} busy={busy} onClose={() => setDetailOpen(false)} onBuy={oneClickBuy} onAdd={addToCart} onRefresh={canManageMonitor ? fetchOne : null} onDirect={openDirectProduct}/>

      {preorderDraft && <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && setPreorderDraft(null)}><div className="checkout-modal preorder-modal" role="dialog" aria-modal="true" aria-label="设置自动预购"><div className="modal-head"><div><span>STOCK PREORDER</span><h2>设置自动预购</h2></div><IconButton label="关闭" onClick={() => setPreorderDraft(null)}><X size={17}/></IconButton></div><div className="preorder-config"><label className="preorder-enable"><input type="checkbox" checked={preorderDraft.enabled} onChange={event => setPreorderDraft({...preorderDraft, enabled: event.target.checked})}/><span><strong>启用自动预购</strong><small>仅缺货商品进入监控，有货商品不会创建任务</small></span></label><label className="preorder-interval"><span>库存检查间隔</span><div><input type="number" min="1" max="86400" value={preorderDraft.interval_seconds} onChange={event => setPreorderDraft({...preorderDraft, interval_seconds: Math.max(1, Math.min(86400, Number(event.target.value) || 1))})} inputMode="numeric"/><span>秒</span></div></label></div><div className="preorder-items">{preorderDraft.items.map(entry => { const eligible = entry.sale_status === 'on_sale' && entry.stock !== null && Number(entry.stock) === 0; return <div className={`preorder-item ${eligible ? '' : 'unavailable'}`} key={entry.watch_id}><div><strong>{entry.title}</strong><small>当前库存：{entry.stock_label}{entry.minimum > 1 ? ` · 最低 ${entry.minimum} 件起购` : ''}</small></div>{eligible ? <label><span>预购数量</span><input type="number" min={entry.minimum} max="99" value={entry.quantity} onChange={event => updatePreorderQuantity(entry.watch_id, event.target.value)} inputMode="numeric"/></label> : <span className="pill paused">{entry.sale_status === 'off_sale' ? '未上架' : entry.stock === null ? '库存未知' : '当前有货'}</span>}</div>; })}</div><div className={`preorder-checkout-status ${savedCheckout.contact ? 'ready' : 'missing'}`}><ShieldCheck size={16}/><span>{savedCheckout.contact ? `使用已保存联系方式 · ${Number(savedCheckout.channel_id) === 4 ? '微信支付' : '支付宝'}` : '请先在右侧购买配置中保存联系方式'}</span></div><div className="modal-foot"><span><Clock3 size={14}/>库存达到预购数量后只创建一次支付链接</span><div className="modal-foot-actions"><button className="button secondary" onClick={() => setPreorderDraft(null)}>取消</button><button className="button official" onClick={savePreorders} disabled={!preorderDraft.enabled || !savedCheckout.contact || busy.preorder || !preorderDraft.items.some(entry => entry.sale_status === 'on_sale' && entry.stock !== null && Number(entry.stock) === 0)}><Zap size={15}/>{busy.preorder ? '正在保存' : '启用预购'}</button></div></div></div></div>}
      {checkoutPrompt && <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && setCheckoutPrompt(null)}><div className="checkout-modal" role="dialog" aria-modal="true" aria-label="完善购买配置"><div className="modal-head"><div><span>CHECKOUT PROFILE</span><h2>完善购买配置</h2></div><IconButton label="关闭" onClick={() => setCheckoutPrompt(null)}><X size={17}/></IconButton></div><div className="modal-notice"><ShieldCheck size={18}/><p>购买前需要联系方式{checkoutPrompt.requiresPassword ? '和安全密码' : ''}，支付渠道与优惠券可按商品支持情况使用。</p></div><div className="purchase-form"><label><span>联系方式</span><input value={contact.contact} onChange={event => setContact({...contact, contact: event.target.value})} placeholder="邮箱、手机号或其他联系方式" autoComplete="email"/></label>{checkoutPrompt.requiresPassword && <label><span>安全密码</span><input type={passwordVisible ? 'text' : 'password'} value={queryPassword} onChange={event => setQueryPassword(event.target.value)} placeholder="用于查询订单详情" autoComplete="off"/></label>}<label><span>支付渠道</span><select value={paymentChannel} onChange={event => setPaymentChannel(Number(event.target.value))}>{paymentChannels.map(channel => <option value={channel.id} key={channel.id}>{channel.name}</option>)}</select></label><label><span>优惠券 <small>可选</small></span><input value={couponCode} onChange={event => setCouponCode(event.target.value)} placeholder="输入优惠券码" autoComplete="off"/></label><label><span>配置保存位置</span><select value={checkoutStorageMode} onChange={event => setCheckoutStorageMode(event.target.value)}><option value="local">本机数据库</option><option value="browser">浏览器缓存</option></select></label></div><div className="modal-foot"><span><ShieldCheck size={14}/>保存后会用于后续购买和订单查询</span><div className="modal-foot-actions"><button className="button secondary" onClick={() => setCheckoutPrompt(null)}>取消</button><button className="button official" onClick={submitCheckoutPrompt}><Save size={15}/>保存并继续</button></div></div></div></div>}
      {review && <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && setReview(null)}><div className="checkout-modal" role="dialog" aria-modal="true" aria-label="购买确认"><div className="modal-head"><div><span>DIRECT CHECKOUT</span><h2>支付链接已准备</h2></div><IconButton label="关闭" onClick={() => setReview(null)}><X size={17}/></IconButton></div><div className="modal-notice"><ShieldCheck size={18}/><p>{review.notice} 创建成功后会自动打开支付页面；下方仍保留“打开支付链接”入口，方便重复打开。</p></div><div className="review-list">{review.items.map(item => <div className="review-item" key={item.watch_id}><div><strong>{item.title}</strong><span>{money(item.unit_price)} × {item.quantity}</span><a className="payment-link" href={item.official_url} target="_blank" rel="noreferrer"><Link2 size={13}/>{item.official_url}</a></div><strong>{money(item.subtotal)}</strong><div className="review-actions"><button className="button secondary" onClick={() => copyPaymentLink(item)}><Clipboard size={15}/>复制商品链接</button></div></div>)}</div>{officialOrder && <div className="payment-order-result"><div><span>官方订单</span><strong>{officialOrder.trade_no}</strong></div><a href={officialOrder.payment_url} target="_blank" rel="noreferrer"><Link2 size={14}/>{officialOrder.payment_url}</a><small>{officialOrder.notice} 渠道：{officialOrder.channel === 'alipay' ? '支付宝' : '微信支付'}，金额：{money(officialOrder.amount)}</small><button className="button official" onClick={() => window.open(officialOrder.payment_url, '_blank', 'noopener,noreferrer')}><ArrowUpRight size={15}/>打开支付链接</button></div>}<div className="review-total"><span>清单合计</span><strong>{money(review.total)}</strong></div><div className="modal-foot"><span><ShieldCheck size={14}/>支付前请核对订单金额</span><div className="modal-foot-actions"><label className="payment-channel"><span>支付渠道</span><select value={paymentChannel} onChange={event => setPaymentChannel(Number(event.target.value))}>{paymentChannels.map(channel => <option value={channel.id} key={channel.id}>{channel.name}</option>)}</select></label><button className="button official auto-pay-button" onClick={createOfficialOrder} disabled={busy.officialOrder}><Package size={15}/>{busy.officialOrder ? '正在创建并跳转' : '创建订单并自动跳转'}</button><button className="button secondary" onClick={() => setReview(null)}>返回修改</button></div></div></div></div>}
      {toast && <div className={`toast ${toast.type} ${toast.sections?.length ? 'detailed' : ''}`} role={toast.type === 'error' ? 'alert' : 'status'} aria-live={toast.type === 'error' ? 'assertive' : 'polite'} aria-atomic="true" key={toast.id}>
        <span className="toast-icon">{toast.type === 'error' ? <AlertCircle size={18}/> : toast.type === 'warning' ? <TriangleAlert size={18}/> : toast.type === 'info' ? <BellRing size={18}/> : <Check size={18}/>}</span>
        <div className="toast-copy">
          <strong>{toast.title || (toast.type === 'error' ? '操作未完成' : toast.type === 'warning' ? '操作需要确认' : '操作成功')}</strong>
          {toast.message && <p>{toast.message}</p>}
          {toast.sections?.length > 0 && <div className="toast-sections">{toast.sections.map(section => <section key={section.title}>
            <div className="toast-section-head"><span>{section.title}</span>{section.badge && <em>{section.badge}</em>}</div>
            <div className="toast-metrics">{section.metrics.map(item => <div className={item.tone || ''} key={item.label}><span>{item.label}</span><strong>{item.value}</strong></div>)}</div>
            {section.detail && <p className="toast-section-detail">{section.detail}</p>}
          </section>)}</div>}
        </div>
        <button className="toast-close" type="button" onClick={dismissToast} aria-label="关闭提示" title="关闭提示"><X size={15}/></button>
        <span className="toast-progress" aria-hidden="true" style={{animationDuration: `${toast.duration}ms`}}/>
      </div>}
    </div>
  );
}

function PurchasePanel({items, cart, setCart, totalCart, estimatedTotal, setQuantity, contact, setContact, saveCheckout, queryPassword, setQueryPassword, passwordVisible, setPasswordVisible, paymentChannel, setPaymentChannel, couponCode, setCouponCode, checkoutStorageMode, setCheckoutStorageMode, prepareCheckout, busy}) {
  const requiresPassword = cart.some(entry => items.find(item => item.id === entry.watch_id)?.latest?.query_password_required);
  return (
    <aside className="purchase-panel">
      <div className="purchase-head">
        <div><span>PURCHASE LIST</span><h2>购买清单</h2></div>
        <span className="cart-count">{totalCart}</span>
      </div>
      <div className="cart-items">
        {!cart.length ? (
          <div className="cart-empty"><ShoppingBag size={24}/><span>从监控列表加入商品</span></div>
        ) : cart.map(entry => {
          const item = items.find(candidate => candidate.id === entry.watch_id);
          if (!item) return null;
          const minimum = Math.max(1, Number(item.latest?.limit_count || 1));
          return (
            <div className="cart-item" key={entry.watch_id}>
              <div className="cart-title">
                <strong>{item.latest?.title || item.name}</strong>
                <button title="移出清单" aria-label="移出清单" onClick={() => setCart(current => current.filter(value => value.watch_id !== entry.watch_id))}><X size={14}/></button>
              </div>
              <div className="cart-meta">
                <span>{money(item.latest?.price)}</span>
                <div className="stepper">
                  <button title="减少数量" aria-label="减少数量" onClick={() => setQuantity(entry.watch_id, entry.quantity - 1)} disabled={entry.quantity <= minimum}><Minus size={13}/></button>
                  <input type="number" min={minimum} max="99" value={entry.quantity} aria-label="购买数量" onChange={event => setQuantity(entry.watch_id, event.target.value)}/>
                  <button title="增加数量" aria-label="增加数量" onClick={() => setQuantity(entry.watch_id, entry.quantity + 1)} disabled={entry.quantity >= 99}><Plus size={13}/></button>
                </div>
              </div>
              <small>{minimum} 件起购，不限购</small>
            </div>
          );
        })}
      </div>
      <div className="purchase-form">
        <div className="form-heading">
          <h3>购买配置</h3>
          <button onClick={saveCheckout} title="保存购买配置到本机"><Save size={14}/>保存配置</button>
        </div>
        <label>
          <span>联系方式</span>
          <input value={contact.contact} onChange={event => setContact({...contact, contact: event.target.value})} placeholder="邮箱、手机或其他联系方式"/>
        </label>
        <label>
          <span className="field-label">查询/安全密码 <small>{requiresPassword ? '当前商品需要' : '当前商品不需要，可留空'}</small></span>
          <div className="password-field">
            <input type={passwordVisible ? 'text' : 'password'} value={queryPassword} onChange={event => setQueryPassword(event.target.value)} placeholder="可保存供下次一键购买" autoComplete="off"/>
            <button type="button" title={passwordVisible ? '隐藏密码' : '显示密码'} aria-label={passwordVisible ? '隐藏密码' : '显示密码'} onClick={() => setPasswordVisible(value => !value)}>{passwordVisible ? <EyeOff size={15}/> : <Eye size={15}/>}</button>
          </div>
        </label>
        <label>
          <span>默认支付方式</span>
          <select value={paymentChannel} onChange={event => setPaymentChannel(Number(event.target.value))}>
            <option value={1}>支付宝</option>
            <option value={4}>微信支付</option>
          </select>
        </label>
        <label>
          <span>备注</span>
          <input value={contact.note} onChange={event => setContact({...contact, note: event.target.value})} placeholder="可选"/>
        </label>
      </div>
      <div className="purchase-summary">
        <div><span>商品数量</span><strong>{totalCart}</strong></div>
        <div><span>预计合计</span><strong>{money(estimatedTotal)}</strong></div>
      </div>
      <button className="button checkout" onClick={prepareCheckout} disabled={!cart.length || busy.checkout}><ShieldCheck size={17}/>{busy.checkout ? '正在校验' : '核对并打开支付链接'}</button>
      <label>
        <span>优惠券 <small>商品支持时生效</small></span>
        <input value={couponCode} onChange={event => setCouponCode(event.target.value)} placeholder="可选，输入优惠券码" autoComplete="off"/>
      </label>
      <label>
        <span>配置保存位置</span>
        <select value={checkoutStorageMode} onChange={event => setCheckoutStorageMode(event.target.value)}>
          <option value="local">本机数据库</option>
          <option value="browser">浏览器缓存</option>
        </select>
      </label>
      <p className="local-note"><ShieldCheck size={14}/>联系方式与安全密码会按所选位置保存，商品不要求密码时不会提交密码</p>
    </aside>
  );
}

function App() {
  const [installStatus, setInstallStatus] = useState(null);
  const [auth, setAuth] = useState({status: 'loading', mode: AUTH_MODES.SELF_USE, user: null, token: ''});
  const [bootError, setBootError] = useState('');
  const [loginOpen, setLoginOpen] = useState(false);

  const clearSession = useMemo(() => () => {
    setApiAuthToken('');
    window.localStorage.removeItem(AUTH_TOKEN_KEY);
    setAuth(current => ({
      status: requiresLogin({...installStatus, mode: current.mode}) ? 'anonymous' : 'guest',
      mode: current.mode,
      user: null,
      token: '',
    }));
  }, [installStatus]);

  const applySession = payload => {
    const token = String(payload?.token || '').trim();
    const normalized = normalizeSession({
      ...payload,
      authenticated: payload?.authenticated !== undefined ? payload.authenticated : Boolean(token || payload?.user),
    });
    if (!normalized.user) {
      clearSession();
      return;
    }
    const effectiveToken = token || String(window.localStorage.getItem(AUTH_TOKEN_KEY) || '').trim();
    setApiAuthToken(effectiveToken);
    if (effectiveToken) window.localStorage.setItem(AUTH_TOKEN_KEY, effectiveToken);
    const nextMode = normalizeMode(payload?.mode || installStatus?.mode || auth.mode);
    setAuth({status: 'authenticated', mode: nextMode, user: normalized.user, token: effectiveToken});
    const registration = payload?.allow_registration ?? payload?.installation?.allow_registration;
    setInstallStatus(current => current ? {
      ...current,
      configured: true,
      needs_setup: false,
      mode: nextMode,
      ...(registration !== undefined ? {allow_registration: Boolean(registration)} : {}),
    } : current);
    setLoginOpen(false);
  };

  useEffect(() => {
    const handleExpired = () => {
      clearSession();
      setLoginOpen(requiresLogin({...installStatus, mode: auth.mode}));
    };
    window.addEventListener('ldxp-auth-expired', handleExpired);
    return () => window.removeEventListener('ldxp-auth-expired', handleExpired);
  }, [auth.mode, clearSession, installStatus]);

  useEffect(() => {
    let cancelled = false;
    const bootstrap = async () => {
      setBootError('');
      let status;
      try {
        status = normalizeInstallStatus(await request('/install/status', {}, {auth: false}));
      } catch (error) {
        // Keep existing self-use installations usable while an older backend is running.
        if (error.status === 404) {
          status = normalizeInstallStatus({configured: true, mode: AUTH_MODES.SELF_USE, needs_setup: false});
        } else {
          if (!cancelled) {
            setBootError(error.message || '无法读取安装状态');
            setAuth(current => ({...current, status: 'error'}));
          }
          return;
        }
      }
      if (cancelled) return;
      setInstallStatus(status);
      if (status.needs_setup || !status.configured) {
        setAuth({status: 'setup', mode: status.mode, user: null, token: ''});
        return;
      }

      const storedToken = String(window.localStorage.getItem(AUTH_TOKEN_KEY) || '').trim();
      if (storedToken) setApiAuthToken(storedToken);
      try {
        // Always probe /auth/me so an HttpOnly cookie can restore a session
        // even after the optional local Bearer token has been removed.
        const me = await request('/auth/me', {}, {auth: Boolean(storedToken)});
        if (!cancelled && me?.authenticated && me?.user) {
          applySession({...me, token: storedToken, mode: me.mode || status.mode});
          return;
        }
      } catch {
        // Expired or unavailable sessions fall through to the login gate.
      }
      if (storedToken) {
        setApiAuthToken('');
        window.localStorage.removeItem(AUTH_TOKEN_KEY);
      }
      if (!cancelled) setAuth({status: requiresLogin(status) ? 'anonymous' : 'guest', mode: status.mode, user: null, token: ''});
    };
    bootstrap();
    return () => { cancelled = true; };
    // Bootstrap intentionally runs once; session changes use applySession/clearSession.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const completeInstall = result => {
    const nextMode = normalizeMode(result?.mode || installStatus?.mode);
    const registration = result?.allow_registration ?? result?.installation?.allow_registration;
    const policy = {
      mode: nextMode,
      auth_required: result?.auth_required ?? result?.installation?.auth_required,
      force_login: result?.force_login ?? result?.installation?.force_login,
      allow_user_reclaim: result?.allow_user_reclaim ?? result?.installation?.allow_user_reclaim,
      allow_user_sub2api_import: result?.allow_user_sub2api_import ?? result?.installation?.allow_user_sub2api_import,
    };
    setInstallStatus(current => ({
      ...(current || {}),
      configured: true,
      needs_setup: false,
      mode: nextMode,
      allow_registration: Boolean(registration),
      ...Object.fromEntries(Object.entries(policy).filter(([, value]) => value !== undefined)),
    }));
    if (result?.token || result?.user) applySession({...result, mode: nextMode});
    else setAuth({status: requiresLogin(policy) ? 'anonymous' : 'guest', mode: nextMode, user: null, token: ''});
  };

  const logout = async () => {
    try {
      await request('/auth/logout', {method: 'POST'});
    } catch {
      // Local session cleanup remains authoritative if the server is unavailable.
    }
    clearSession();
  };

  const handleModeChange = nextMode => {
    const patch = nextMode && typeof nextMode === 'object' ? nextMode : {mode: nextMode};
    const normalizedMode = normalizeMode(patch.mode);
    const policy = {...patch, mode: normalizedMode};
    setInstallStatus(current => current ? {...current, ...policy, mode: normalizedMode, auth_required: patch.auth_required ?? requiresLogin(policy)} : current);
    setAuth(current => ({
      ...current,
      mode: normalizedMode,
      status: current.user || !requiresLogin(policy) ? (current.user ? 'authenticated' : 'guest') : 'anonymous',
    }));
  };

  if (auth.status === 'loading') return <div className="auth-boot-state" role="status"><Wrench size={20} className="spin"/><span>正在检查安装状态</span></div>;
  if (auth.status === 'error') return <div className="auth-boot-state error" role="alert"><strong>无法连接服务</strong><span>{bootError || '请确认后端服务正在运行'}</span><button className="button secondary" type="button" onClick={() => window.location.reload()}><RefreshCw size={15}/>重新加载</button></div>;
  if (auth.status === 'setup') return <InstallWizard request={request} onComplete={completeInstall}/>;
  if (auth.status === 'anonymous') return <LoginView request={request} mode={auth.mode} allowRegistration={Boolean(installStatus?.allow_registration)} onAuthenticated={applySession} onGuest={auth.mode === AUTH_MODES.SELF_USE ? () => setAuth(current => ({...current, status: 'guest'})) : undefined}/>;

  return <>
    <WorkspaceApp sessionUser={auth.user} authMode={auth.mode} accessPolicy={installStatus || {}} onLogout={logout} onLogin={() => setLoginOpen(true)} onModeChange={handleModeChange}/>
    {loginOpen && <div className="auth-modal-host"><LoginView request={request} mode={auth.mode} allowRegistration={Boolean(installStatus?.allow_registration)} onAuthenticated={applySession} onBack={() => setLoginOpen(false)} onGuest={() => setLoginOpen(false)}/></div>}
  </>;
}

const rootElement = document.getElementById('root');
const root = import.meta.hot?.data.root || createRoot(rootElement);
if (import.meta.hot) import.meta.hot.data.root = root;
root.render(<App/>);
