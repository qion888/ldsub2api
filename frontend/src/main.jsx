import React, {useEffect, useMemo, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {
  Activity,
  AlertCircle,
  ArrowUpRight,
  BellRing,
  Check,
  ChevronRight,
  CircleDollarSign,
  Clipboard,
  Clock3,
  Eye,
  EyeOff,
  FileUp,
  Filter,
  History,
  Link2,
  ListChecks,
  Moon,
  Minus,
  Package,
  PanelRight,
  Plus,
  RefreshCw,
  Save,
  Search,
  KeyRound,
  Sun,
  Tag,
  Upload,
  ShieldCheck,
  ShoppingBag,
  Store,
  Trash2,
  X,
  Zap,
} from 'lucide-react';
import './style.css';

const API = '/api';
const DEFAULT_SUB2API_AUTOMATION = {
  enabled: false,
  interval_seconds: 300,
  auto_import: false,
};

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

async function request(path, options) {
  const response = await fetch(`${API}${path}`, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || '请求失败');
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

function intervalLabel(value) {
  const seconds = Number(value || 0);
  if (seconds < 60) return `${seconds}秒`;
  if (seconds % 60 === 0) return `${seconds / 60}分钟`;
  return `${seconds}秒`;
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

function PriceBars({history}) {
  const points = history.filter(point => {
    if (point.status !== 'success' || point.price === null || point.price === undefined || point.price === '') return false;
    return Number.isFinite(Number(point.price));
  });
  if (!points.length) return <div className="chart-empty">完成两次抓取后显示价格走势</div>;
  const values = points.map(point => Number(point.price));
  const min = Math.min(...values);
  const max = Math.max(...values);
  return (
    <div className="price-chart" aria-label="价格历史">
      {points.slice(-24).map((point, index) => {
        const value = Number(point.price);
        const height = max === min ? 58 : 22 + ((value - min) / (max - min)) * 70;
        return <Tooltip key={`${point.id}-${index}`} label={`${compactTime(point.fetched_at)} · ${money(value)}`} placement="top">
          <div className="bar-wrap">
            <span className="bar" style={{height: `${height}%`}}/>
          </div>
        </Tooltip>;
      })}
    </div>
  );
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
                  <IconButton label="刷新商品" onClick={() => onRefresh(item.id)} disabled={busy[`fetch-${item.id}`]}><RefreshCw size={15} className={busy[`fetch-${item.id}`] ? 'spin' : ''}/></IconButton>
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
              <div className="radar-actions"><IconButton label="刷新商品" onClick={() => onRefresh(item.id)} disabled={busy[`fetch-${item.id}`]}><RefreshCw size={14} className={busy[`fetch-${item.id}`] ? 'spin' : ''}/></IconButton><IconButton label="直达商品页" onClick={() => onDirect(item)}><ArrowUpRight size={14}/></IconButton><IconButton label="一键购买" tone="buy" onClick={() => onBuy(item)} disabled={!itemPurchasable(item) || busy[`buy-${item.id}`]}><Zap size={14} className={busy[`buy-${item.id}`] ? 'spin' : ''}/></IconButton><IconButton label="加入购买清单" onClick={() => onAdd(item)} disabled={!itemPurchasable(item)}><ShoppingBag size={14}/></IconButton></div>
            </article>;
          })}
        </div>
      </div>
      <div className="drawer-foot"><Tag size={14}/><span>报价为本地监控快照，最低价按全部监控商品分类计算</span></div>
    </aside>
  </>;
}


function ProductDetailDrawer({open, item, history, priceDelta, lowestPrice, busy = {}, onClose, onBuy, onAdd, onRefresh, onDirect}) {
  const latest = item?.latest;
  const stock = itemStock(item);
  const specs = latest?.specs && typeof latest.specs === 'object' ? Object.entries(latest.specs) : [];
  const sourceUrl = latest?.source_url || item?.url;
  return <>
    <div className={`drawer-backdrop detail-backdrop ${open ? 'open' : ''}`} onClick={onClose} aria-hidden="true"/>
    <aside className={`product-detail-drawer ${open ? 'open' : ''}`} aria-label="商品详情" aria-hidden={!open} inert={!open ? true : undefined}>
      <div className="drawer-header">
        <div><span className="drawer-kicker">PRODUCT DETAIL</span><h2>商品详情</h2><p>{item ? itemShopName(item) : '未选择商品'}</p></div>
        <IconButton label="关闭商品详情" onClick={onClose}><X size={17}/></IconButton>
      </div>
      {!item ? <div className="drawer-empty"><Package size={30}/><strong>请选择商品</strong><span>从价格雷达或商品目录打开详情</span></div> : <div className="detail-drawer-body">
        <div className="detail-drawer-hero"><ProductImage item={item} size="large"/><div><span className="detail-kicker">{itemCategory(item)}</span><h3>{latest?.title || item.name || '等待商品数据'}</h3><span className="detail-source">{itemShopName(item)}</span></div></div>
        <div className="detail-drawer-actions"><button className="button primary" onClick={() => onBuy(item)} disabled={!itemPurchasable(item) || busy[`buy-${item.id}`]}><Zap size={15}/>{busy[`buy-${item.id}`] ? '正在准备' : '一键购买'}</button><button className="button secondary" onClick={() => onAdd(item)} disabled={!itemPurchasable(item)}><ShoppingBag size={15}/>加入清单</button><button className="button secondary" onClick={() => onDirect(item)}><ArrowUpRight size={15}/>直达商品页</button><IconButton label="刷新商品" onClick={() => onRefresh(item.id)} disabled={busy[`fetch-${item.id}`]}><RefreshCw size={15} className={busy[`fetch-${item.id}`] ? 'spin' : ''}/></IconButton></div>
        <div className="detail-price-board"><div><span>当前报价</span><strong>{money(itemPrice(item))}</strong></div><div><span>同类最低</span><strong>{money(lowestPrice)}</strong></div><div><span>参考价</span><strong>{Number(latest?.market_price) > 0 ? money(latest.market_price) : '--'}</strong></div><div><span>价格变化</span><strong className={priceDelta > 0 ? 'negative' : priceDelta < 0 ? 'positive' : ''}>{priceDelta === null || priceDelta === undefined ? '--' : priceDelta === 0 ? '不变' : `${priceDelta > 0 ? '+' : '-'}${money(Math.abs(priceDelta))}`}</strong></div></div>
        <div className="detail-state-grid"><div><span>库存状态</span><strong className={`state-text ${stock.key}`}>{itemStockLabel(item)}</strong></div><div><span>销售状态</span><strong>{itemIsUnlisted(item) ? '未上架' : latest?.sale_status === 'on_sale' ? '在售' : '状态未知'}</strong></div><div><span>最低起购</span><strong>{latest?.limit_count || 1} 件</strong></div><div><span>最近同步</span><strong>{compactTime(item.last_attempt?.fetched_at)}</strong></div></div>
        <div className="detail-drawer-chart"><div className="drawer-section-head"><div><span className="drawer-kicker">PRICE HISTORY</span><h3>价格走势</h3></div><span>{history.length} 次记录</span></div><PriceBars history={history}/></div>
        <div className="detail-description"><span className="drawer-kicker">DESCRIPTION</span><h3>商品信息</h3><p>{latest?.description || '暂无商品描述'}</p></div>
        {specs.length > 0 && <div className="detail-drawer-specs"><span className="drawer-kicker">SPECIFICATIONS</span><h3>规格参数</h3><div>{specs.map(([key, value]) => <dl key={key}><dt>{key}</dt><dd>{String(value)}</dd></dl>)}</div></div>}
        {item.last_attempt?.status === 'error' && <div className="inline-error"><AlertCircle size={16}/><span>{item.last_attempt.error}</span></div>}
        <a className="detail-direct-link" href={sourceUrl} target="_blank" rel="noreferrer"><Link2 size={14}/>打开官方商品页面<ArrowUpRight size={14}/></a>
      </div>}
    </aside>
  </>;
}

function App() {
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
  const [shopFilter, setShopFilter] = useState(null);
  const [checkedIds, setCheckedIds] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [theme, setTheme] = useState(() => window.localStorage.getItem('ldxp-theme') === 'dark' ? 'dark' : 'light');
  const [overviewOpen, setOverviewOpen] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  const [history, setHistory] = useState([]);
  const [cart, setCart] = useState([]);
  const [contact, setContact] = useState({contact: '', note: ''});
  const [queryPassword, setQueryPassword] = useState('');
  const [savedCheckout, setSavedCheckout] = useState({contact: '', note: '', query_password: '', channel_id: 1});
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [review, setReview] = useState(null);
  const [officialOrder, setOfficialOrder] = useState(null);
  const [paymentChannel, setPaymentChannel] = useState(1);
  const [paymentChannels, setPaymentChannels] = useState([{id: 1, name: '支付宝'}]);
  const paymentWindow = useRef(null);
  const [activeView, setActiveView] = useState('monitor');
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
  const [sub2apiOptions, setSub2apiOptions] = useState({loaded: false, proxies: [], groups: [], proxy_count: 0, group_count: 0});
  const [sub2apiProxyChoice, setSub2apiProxyChoice] = useState('json');
  const [sub2apiGroupIds, setSub2apiGroupIds] = useState([]);
  const [sub2apiCodexFingerprintMode, setSub2apiCodexFingerprintMode] = useState('off');
  const [sub2apiReclaimBusy, setSub2apiReclaimBusy] = useState(false);
  const [sub2apiReclaimResult, setSub2apiReclaimResult] = useState(null);
  const [sub2apiAutomation, setSub2apiAutomation] = useState(DEFAULT_SUB2API_AUTOMATION);
  const [sub2apiAutomationState, setSub2apiAutomationState] = useState(null);
  const [sub2apiAutomationBusy, setSub2apiAutomationBusy] = useState(false);
  const [busy, setBusy] = useState({});
  const [verificationShopId, setVerificationShopId] = useState(null);
  const [serviceOnline, setServiceOnline] = useState(false);
  const [toast, setToast] = useState(null);
  const toastTimer = useRef(null);
  const preorderLoaded = useRef(false);
  const knownTriggeredPreorders = useRef(new Set());
  const stableItemOrder = useStableItemOrder(items);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem('ldxp-theme', theme);
  }, [theme]);

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

  const notify = (message, type = 'info') => {
    setToast({message, type});
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 3200);
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
      const [data, shopData, preorderData] = await Promise.all([
        request('/watches'),
        request('/shops'),
        request('/preorders'),
      ]);
      setItems(data);
      setShops(shopData);
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
    } catch (error) {
      setServiceOnline(false);
      if (!quiet) notify(error.message, 'error');
    }
  };

  const loadHistory = async id => {
    if (!id) return setHistory([]);
    try {
      setHistory(await request(`/watches/${id}/history?limit=60`));
    } catch {
      setHistory([]);
    }
  };

  useEffect(() => {
    loadItems();
    request('/settings/checkout').then(config => {
      setContact({contact: config.contact || '', note: config.note || ''});
      setQueryPassword(config.query_password || '');
      setPaymentChannel(Number(config.channel_id || 1));
      setSavedCheckout(config);
    }).catch(() => {});
    request('/redeem/config').then(config => setRedeemConfig(config)).catch(() => {});
    request('/sub2api/config').then(config => setSub2apiConfig(config)).catch(() => {});
    loadSub2ApiAutomation({syncSettings: true});
  }, []);

  useEffect(() => {
    if (activeView === 'sub2api' && sub2apiConfig.admin_key_set) loadSub2ApiOptions({quiet: true});
  }, [activeView, sub2apiConfig.base_url, sub2apiConfig.admin_key_set]);

  useEffect(() => {
    if (activeView !== 'sub2api') return undefined;
    loadSub2ApiAutomation();
    const timer = window.setInterval(() => loadSub2ApiAutomation(), 5000);
    return () => window.clearInterval(timer);
  }, [activeView]);

  const fastestInterval = [...items, ...shops, ...preorders.filter(entry => entry.enabled)]
    .filter(item => item.enabled)
    .reduce((fastest, item) => Math.min(fastest, Number(item.interval_seconds || 60)), 60);
  const dashboardRefreshMs = fastestInterval <= 5 ? 1000 : fastestInterval <= 30 ? 3000 : 5000;

  useEffect(() => {
    const timer = window.setInterval(() => loadItems({quiet: true}), dashboardRefreshMs);
    return () => window.clearInterval(timer);
  }, [dashboardRefreshMs]);

  useEffect(() => {
    loadHistory(selectedId);
  }, [selectedId, items.find(item => item.id === selectedId)?.last_run]);

  const selected = items.find(item => item.id === selectedId) || null;
  const latest = selected?.latest;
  const monitored = items.filter(item => item.enabled).length;
  const saleCount = items.filter(item => item.latest?.sale_status === 'on_sale').length;
  const changes = items.filter(item => item.price_changed).length;
  const failures = items.filter(item => item.last_attempt?.status === 'error' && !itemIsUnlisted(item)).length;
  const shopFailures = shops.filter(shop => shop.last_attempt?.status === 'error').length;
  const visibleItems = shopFilter
    ? items.filter(item => item.shops?.some(shop => shop.id === shopFilter))
    : items;
  const totalCart = cart.reduce((sum, entry) => sum + entry.quantity, 0);
  const estimatedTotal = cart.reduce((sum, entry) => {
    const item = items.find(candidate => candidate.id === entry.watch_id);
    const unitPrice = Number(item?.latest?.price);
    return sum + (Number.isFinite(unitPrice) ? unitPrice : 0) * entry.quantity;
  }, 0);
  const visibleCheckedIds = visibleItems.filter(item => checkedIds.includes(item.id)).map(item => item.id);
  const allVisibleChecked = visibleItems.length > 0 && visibleCheckedIds.length === visibleItems.length;
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
    setShops(current => current.map(value => value.id === shop.id ? next : value));
    try {
      await request(`/shops/${shop.id}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(next),
      });
    } catch (error) {
      await loadItems({quiet: true});
      notify(error.message, 'error');
    }
  };

  const removeShop = async shop => {
    if (!window.confirm(`停止监控店铺“${shop.name || shop.token}”？已导入的商品记录会保留。`)) return;
    try {
      await request(`/shops/${shop.id}`, {method: 'DELETE'});
      if (shopFilter === shop.id) setShopFilter(null);
      await loadItems({quiet: true});
      notify('店铺监控已删除');
    } catch (error) {
      notify(error.message, 'error');
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
    setItems(current => current.map(candidate => candidate.id === item.id ? next : candidate));
    try {
      await request(`/watches/${item.id}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: next.name, enabled: next.enabled, interval_seconds: next.interval_seconds}),
      });
    } catch (error) {
      await loadItems({quiet: true});
      notify(error.message, 'error');
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

  const saveCheckout = async () => {
    try {
      const saved = await request('/settings/checkout', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...contact, query_password: queryPassword, channel_id: paymentChannel}),
      });
      setContact({contact: saved.contact, note: saved.note});
      setQueryPassword(saved.query_password || '');
      setPaymentChannel(Number(saved.channel_id || 1));
      setSavedCheckout(saved);
      notify('购买配置已保存到本机');
    } catch (error) {
      notify(error.message, 'error');
    }
  };

  const prepareCheckout = async () => {
    if (!cart.length) return notify('请先加入商品', 'error');
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
        channel_id: paymentChannel,
      }, paymentWindow.current);
    } catch (error) {
      if (paymentWindow.current && !paymentWindow.current.closed) paymentWindow.current.close();
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, officialOrder: false}));
    }
  };

  const oneClickBuy = async item => {
    if (!itemPurchasable(item)) return notify(itemStock(item).key === 'out' ? '该商品当前缺货，可设置自动预购' : '该商品当前不可购买', 'error');
    if (!savedCheckout.contact) return notify('请先在购买配置中保存联系方式', 'error');
    if (item.latest?.query_password_required && !savedCheckout.query_password) {
      return notify('该商品需要查询密码，请先在购买配置中保存', 'error');
    }
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
      await placeOfficialOrder(checkout, {...savedCheckout, channel_id: channelId}, openedWindow);
    } catch (error) {
      if (openedWindow && !openedWindow.closed) openedWindow.close();
      notify(error.message, 'error');
    } finally {
      setBusy(value => ({...value, [`buy-${item.id}`]: false}));
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

  const runReclaim = async action => {
    const codes = normalizedCardCodes();
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
      setReclaimResult(result);
      if (action === 'reclaim') notify('401 找回任务已提交');
      else if (action === 'progress') notify('找回进度已刷新');
      else notify(`检测完成：${result.need_reclaim || 0} 个需要找回`);
    } catch (error) {
      notify(error.message, 'error');
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
    return {accounts: merged.accounts.length, files: usable.length, filename};
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

  const reclaimSub2Api401 = async () => {
    setSub2apiReclaimBusy(true);
    setSub2apiReclaimResult(null);
    try {
      const initial = await request('/sub2api/reclaim-401', {method: 'POST'});
      setSub2apiReclaimResult(initial);
      if (!initial.accounts_401) {
        notify('扫描完成，没有发现明确的 401 账号');
        return;
      }
      if (!initial.card_code_count) {
        notify(`发现 ${initial.accounts_401} 个 401 账号，但名称末尾没有可用卡密`, 'error');
        return;
      }

      const downloads = new Map();
      const rememberDownloads = values => (values || []).forEach(item => {
        const orderNo = item?.task?.order_no || item?.filename;
        if (orderNo && item?.data) downloads.set(orderNo, item);
      });
      rememberDownloads(initial.downloaded_payloads);
      const codes = Array.isArray(initial.reclaim_card_codes) ? initial.reclaim_card_codes : [];
      let progressResult = initial.result || {};
      let activeTasks = Number(progressResult.queued || 0) + Number(progressResult.already_running || 0);
      let attempts = 0;
      while (codes.length && activeTasks > 0 && attempts < 120) {
        await new Promise(resolve => window.setTimeout(resolve, 5000));
        const progress = await request('/sub2api/reclaim-progress', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({card_codes: codes}),
        });
        rememberDownloads(progress.downloaded_payloads);
        progressResult = progress.result || {};
        activeTasks = Number(progressResult.queued || 0) + Number(progressResult.already_running || 0);
        attempts += 1;
        setSub2apiReclaimResult({
          ...initial,
          result: progressResult,
          downloaded_payloads: [...downloads.values()],
        });
      }

      const staged = stageRecoveredPayloads([...downloads.values()]);
      if (staged) {
        notify(`找回完成并下载 JSON，${staged.accounts} 个账号已放入一键导入区`);
      } else if (activeTasks > 0) {
        notify('找回任务仍在处理，自动轮询已达到 10 分钟', 'error');
      } else {
        notify('找回任务已结束，但没有生成可导入 JSON', 'error');
      }
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setSub2apiReclaimBusy(false);
    }
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
      notify(result.settings.enabled ? '401 自动找回与导入已启用' : '401 自动化配置已保存');
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setSub2apiAutomationBusy(false);
    }
  };

  const runSub2ApiAutomation = async () => {
    setSub2apiAutomationBusy(true);
    try {
      const result = await request('/sub2api/automation/run', {method: 'POST'});
      setSub2apiAutomationState(result.state || null);
      const summary = result.result || result.state?.last_result;
      notify(summary?.imported ? '自动找回完成，账号已导入 Sub2API' : '401 自动监控已执行');
    } catch (error) {
      notify(error.message, 'error');
      await loadSub2ApiAutomation();
    } finally {
      setSub2apiAutomationBusy(false);
    }
  };

  const importSub2Api = async payloadOverride => {
    const payload = payloadOverride || sub2apiPayload || reclaimPayload;
    if (!payload) return notify('请先选择账号 JSON 文件或下载找回结果', 'error');
    setSub2apiBusy(true);
    try {
      const assignExisting = sub2apiProxyChoice !== 'json' || sub2apiGroupIds.length > 0;
      const proxyId = sub2apiProxyChoice.startsWith('proxy:') ? Number(sub2apiProxyChoice.slice(6)) : null;
      const result = await request('/sub2api/import', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          data: payload,
          assign_existing: assignExisting,
          proxy_id: proxyId,
          group_ids: sub2apiGroupIds,
          codex_fingerprint_mode: sub2apiCodexFingerprintMode,
        }),
      });
      setSub2apiResult(result);
      notify('Sub2API 账号导入完成');
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setSub2apiBusy(false);
    }
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
    const prices = history
      .filter(point => point.status === 'success' && point.price !== null && point.price !== undefined && point.price !== '')
      .map(point => Number(point.price))
      .filter(value => Number.isFinite(value));
    if (prices.length < 2) return null;
    return prices[prices.length - 1] - prices[prices.length - 2];
  }, [history]);
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
    setOverviewOpen(false);
    setDetailOpen(false);
    setActiveView(view);
  };
  const viewMeta = {
    products: {title: '商品总览', description: '聚合监控店铺报价，快速比较最低价、库存与销售状态'},
    monitor: {title: '店铺与商品监控', description: '汇总店铺商品，追踪库存、价格与在售状态'},
    history: {title: '价格记录', description: '查看选中商品的抓取结果与价格变化'},
    reclaim: {title: '卡密 401 找回', description: '检测并找回 30d.team 卡密关联的 401 账号'},
    sub2api: {title: 'Sub2API 账号导入', description: '使用管理员密钥将账号 JSON 导入 Sub2API'},
  }[activeView];

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><div className="brand-mark"><Activity size={18}/></div><div><strong>链动监控台</strong><span>LOCAL WATCH</span></div></div>
        <nav className="nav-list" aria-label="页面导航">
          <button className={activeView === 'products' ? 'active' : ''} onClick={() => switchView('products')}><CircleDollarSign size={18}/>商品总览</button>
          <button className={activeView === 'monitor' ? 'active' : ''} onClick={() => switchView('monitor')}><ListChecks size={18}/>监控面板</button>
          <button className={activeView === 'history' ? 'active' : ''} onClick={() => switchView('history')}><History size={18}/>价格记录</button>
          <button className={activeView === 'reclaim' ? 'active' : ''} onClick={() => switchView('reclaim')}><KeyRound size={18}/>401 找回</button>
          <button className={activeView === 'sub2api' ? 'active' : ''} onClick={() => switchView('sub2api')}><Upload size={18}/>Sub2API 导入</button>
        </nav>
        <div className="sidebar-foot">
          <div className={`service-light ${serviceOnline ? 'online' : ''}`}><span/>{serviceOnline ? '本地服务在线' : '本地服务离线'}</div>
          <small>数据存储于本机 SQLite</small>
        </div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div><h1>{viewMeta.title}</h1><p>{viewMeta.description}</p></div>
          <div className="topbar-actions">
            {['products', 'monitor', 'history'].includes(activeView) && <button className="button secondary overview-button" onClick={() => { setDetailOpen(false); setOverviewOpen(true); }} aria-expanded={overviewOpen} aria-controls="product-overview-drawer"><PanelRight size={16}/>商品总览</button>}
            <IconButton label={theme === 'dark' ? '切换日间模式' : '切换夜间模式'} onClick={() => setTheme(current => current === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? <Sun size={16}/> : <Moon size={16}/>}</IconButton>
            {['products', 'monitor', 'history'].includes(activeView) && <button className="button secondary refresh-all-button" onClick={fetchAll} disabled={busy.fetchAll || (!items.length && !shops.length)}>
              <RefreshCw size={16} className={busy.fetchAll ? 'spin' : ''}/>全部刷新
            </button>}
          </div>
        </header>

        {['monitor', 'history'].includes(activeView) && <section className="metrics" aria-label="监控概览">
          <div><span>监控店铺</span><strong>{shops.filter(shop => shop.enabled).length}</strong><small>共 {shops.length} 个店铺</small></div>
          <div><span>当前在售</span><strong className="positive">{saleCount}</strong><small>依据公开状态</small></div>
          <div><span>商品监控</span><strong>{monitored}</strong><small>目录共 {items.length} 项</small></div>
          <div><span>抓取异常</span><strong className={failures + shopFailures ? 'negative' : ''}>{failures + shopFailures}</strong><small>{changes ? `${changes} 项价格变化` : '最近一次结果'}</small></div>
        </section>}

        {activeView === 'products' ? (
          <ProductOverviewView items={items} shops={shops} stableOrder={stableItemOrder} busy={busy} selectedId={selectedId} onSelect={setSelectedId} onOpenDetail={openProductDetail} onBuy={oneClickBuy} onAdd={addToCart} onDirect={openDirectProduct} onRefresh={fetchOne}/>
        ) : activeView === 'monitor' ? (
          <>
            <form className="watch-composer" onSubmit={addWatch}>
              <div className="composer-title"><div className="section-icon">{sourceMode === 'shop' ? <Store size={17}/> : <BellRing size={17}/>}</div><div><h2>添加监控</h2><div className="source-segment"><button type="button" className={sourceMode === 'shop' ? 'active' : ''} onClick={() => changeSourceMode('shop')}><Store size={13}/>整店</button><button type="button" className={sourceMode === 'item' ? 'active' : ''} onClick={() => changeSourceMode('item')}><Package size={13}/>单商品</button></div></div></div>
              <div className={`composer-fields ${sourceMode === 'shop' ? 'shop-mode' : ''}`}>
                <label className="url-field"><span>{sourceMode === 'shop' ? '店铺链接' : '商品链接'}</span><div><Link2 size={16}/><input value={url} onChange={event => changeSourceUrl(event.target.value)} placeholder={sourceMode === 'shop' ? '粘贴店铺链接' : '粘贴商品链接'} required spellCheck="false"/></div></label>
                <label><span>备注</span><input value={name} onChange={event => setName(event.target.value)} placeholder="可选"/></label>
                {sourceMode === 'shop' && <label><span>分类 ID（可选）</span><input value={categoryId} onChange={event => setCategoryId(event.target.value.replace(/\D/g, ''))} placeholder="全部分类" inputMode="numeric"/></label>}
                {sourceMode === 'shop' && <label><span>商品类型</span><input value={goodsType} onChange={event => setGoodsType(event.target.value.replace(/[^A-Za-z0-9_-]/g, ''))} placeholder="card"/></label>}
                <label><span>监控频率</span><select value={intervalSeconds} onChange={event => setIntervalSeconds(Number(event.target.value))}><option value={1}>每 1 秒</option><option value={3}>每 3 秒</option><option value={5}>每 5 秒</option><option value={10}>每 10 秒</option><option value={30}>每 30 秒</option><option value={60}>每 1 分钟</option><option value={300}>每 5 分钟</option><option value={900}>每 15 分钟</option><option value={1800}>每 30 分钟</option></select></label>
                <button className="button primary" disabled={busy.add}><Plus size={16}/>{busy.add ? '正在同步' : sourceMode === 'shop' ? '同步店铺' : '开始监控'}</button>
              </div>
            </form>

            {!!shops.length && <section className="shop-overview">
              <div className="section-heading"><div><h2>店铺汇总</h2><p>店铺接口按设定频率分页同步</p></div>{shopFilter && <button className="clear-filter" onClick={() => { setShopFilter(null); setCheckedIds([]); }}><X size={13}/>显示全部商品</button>}</div>
              <div className="shop-list">{shops.map(shop => <div className={`shop-row ${shopFilter === shop.id ? 'selected' : ''}`} key={shop.id}>
                <button className="shop-main" onClick={() => { setShopFilter(current => current === shop.id ? null : shop.id); setCheckedIds([]); }}>
                  <span className="shop-icon"><Store size={18}/></span><span><strong>{shop.name || shop.token}</strong><small>{shop.token} · 分类 {shop.category_id || '全部'}</small></span>
                </button>
                <div className="shop-stat"><span>商品</span><strong>{shop.product_count}</strong></div>
                <div className="shop-stat"><span>在售</span><strong className="positive">{shop.on_sale_count}</strong></div>
                <div className="shop-stat"><span>已知库存</span><strong>{shop.total_stock ?? '--'}</strong><small>{shop.known_stock_count}/{shop.product_count} 项公开</small></div>
                <div className="shop-time" title={shop.last_attempt?.error || ''}><span className={`pill ${shop.last_attempt?.status === 'error' ? 'error' : 'live'}`}>{needsBrowserVerification(shop) ? '需要验证' : shop.last_attempt?.status === 'error' ? '同步异常' : '已同步'}</span><small>{compactTime(shop.last_attempt?.fetched_at)}</small></div>
                <button className={`switch ${shop.enabled ? 'on' : ''}`} role="switch" aria-checked={shop.enabled} title={shop.enabled ? '暂停店铺监控' : '开启店铺监控'} onClick={() => updateShop(shop, {enabled: !shop.enabled})}><span/></button>
                <div className="row-actions">{needsBrowserVerification(shop) && (verificationShopId === shop.id ? <IconButton label="验证完成并同步" tone="verify" onClick={() => completeBrowserVerification(shop)} disabled={busy[`verify-${shop.id}`]}><ShieldCheck size={15} className={busy[`verify-${shop.id}`] ? 'spin' : ''}/></IconButton> : <IconButton label="打开浏览器验证" tone="verify" onClick={() => startBrowserVerification(shop)} disabled={busy[`verify-${shop.id}`]}><ArrowUpRight size={15} className={busy[`verify-${shop.id}`] ? 'spin' : ''}/></IconButton>)}<IconButton label="同步店铺" onClick={() => fetchShop(shop.id)} disabled={busy[`shop-${shop.id}`]}><RefreshCw size={15} className={busy[`shop-${shop.id}`] ? 'spin' : ''}/></IconButton><IconButton label="删除店铺监控" tone="danger" onClick={() => removeShop(shop)}><Trash2 size={15}/></IconButton></div>
              </div>)}</div>
            </section>}

            <div className="content-layout">
              <section className="monitor-panel">
                <div className="section-heading"><div><h2>{shopFilter ? `${shops.find(shop => shop.id === shopFilter)?.name || '店铺'}商品` : '商品目录'}</h2><p>{visibleItems.length ? `最近状态已同步，共 ${visibleItems.length} 项` : '添加商品或同步店铺后会显示在这里'}</p></div><span className="count-badge">{visibleItems.length}</span></div>
                {visibleCheckedIds.length > 0 && <div className="batch-toolbar"><span>已选 {visibleCheckedIds.length} 项</span><div><button className="button preorder-button" onClick={openPreorder} disabled={busy.preorderRefresh}><Clock3 size={14}/>{busy.preorderRefresh ? '正在同步库存' : '设置预购'}</button><button className="button secondary" onClick={() => copyLinks(visibleItems.filter(item => visibleCheckedIds.includes(item.id)))}><Clipboard size={14}/>复制链接</button><button className="button danger-button" onClick={removeChecked} disabled={busy.batchDelete}><Trash2 size={14}/>{busy.batchDelete ? '正在移除' : '移出本地目录'}</button></div></div>}
                {!!displayedPreorders.length && <div className="preorder-list" aria-label="自动预购任务">{displayedPreorders.map(preorder => <div className={`preorder-row ${preorder.status}`} key={preorder.id}><span className="preorder-icon"><Clock3 size={15}/></span><div className="preorder-copy"><strong>{preorder.title}</strong><small>目标 {preorder.quantity} 件 · 每 {preorder.interval_seconds} 秒检查 · 当前库存 {preorder.stock_label}</small>{preorder.last_error && <small className="negative">{preorder.last_error}</small>}</div><span className={`pill ${preorder.status === 'triggered' ? 'live' : preorder.status === 'error' ? 'error' : 'neutral'}`}>{preorder.status === 'watching' ? '预购监控中' : preorder.status === 'processing' ? '正在创建订单' : preorder.status === 'triggered' ? '支付链接已创建' : '预购失败'}</span>{preorder.payment_url ? <a className="button official preorder-pay-link" href={preorder.payment_url} target="_blank" rel="noreferrer"><ArrowUpRight size={14}/>打开支付链接</a> : preorder.status === 'watching' || preorder.status === 'error' ? <IconButton label="停止自动预购" tone="danger" onClick={() => cancelPreorder(preorder)}><X size={15}/></IconButton> : <span/>}</div>)}</div>}
                <div className="table-head"><label className="check-wrap" title="全选当前列表"><input className="select-checkbox" type="checkbox" checked={allVisibleChecked} onChange={toggleAllVisible}/></label><span>商品</span><span>价格</span><span>库存 / 状态</span><span>监控</span><span>操作</span></div>
                <div className="product-list">
                  {!visibleItems.length ? <div className="empty-state"><Package size={28}/><strong>暂无商品数据</strong><span>在上方添加店铺或商品链接</span></div> : visibleItems.map(item => (
                    <div className={`product-row ${selectedId === item.id ? 'selected' : ''} ${checkedIds.includes(item.id) ? 'checked' : ''}`} key={item.id} onClick={() => openProductDetail(item.id)}>
                      <label className="check-wrap checkbox-cell" title="选择商品" onClick={event => event.stopPropagation()}><input className="select-checkbox" type="checkbox" checked={checkedIds.includes(item.id)} onChange={() => toggleChecked(item.id)}/></label>
                      <div className="product-cell"><ProductImage item={item}/><div className="product-copy"><strong>{item.latest?.title || item.name || '等待首次抓取'}</strong><span>{item.shops?.length ? `${item.shops[0].name || item.shops[0].token} · ${item.url}` : item.name && item.latest ? item.name : item.url}</span><small>{compactTime(item.last_attempt?.fetched_at)}</small></div></div>
                      <div className="price-cell"><strong>{money(item.latest?.price)}</strong>{item.price_changed && <span className="change-flag">有变化</span>}</div>
                      <div className="state-cell"><StatusPill item={item}/><small>{itemStockLabel(item)}</small>{preorderByWatch.get(item.id)?.status === 'watching' && <small className="preorder-state">自动预购 {preorderByWatch.get(item.id).quantity} 件</small>}</div>
                      <div className="monitor-cell"><button className={`switch ${item.enabled ? 'on' : ''}`} role="switch" aria-checked={item.enabled} title={item.enabled ? '暂停自动监控' : '开启自动监控'} onClick={event => {event.stopPropagation(); updateWatch(item, {enabled: !item.enabled});}}><span/></button><small>{intervalLabel(item.interval_seconds)}</small></div>
                      <div className="row-actions" onClick={event => event.stopPropagation()}>
                        <IconButton label={item.shops?.length ? '同步所属店铺库存' : '立即抓取'} onClick={() => fetchOne(item.id)} disabled={busy[`fetch-${item.id}`]}><RefreshCw size={15} className={busy[`fetch-${item.id}`] ? 'spin' : ''}/></IconButton>
                        <IconButton label="复制商品链接" onClick={() => copyLinks([item])}><Clipboard size={15}/></IconButton>
                        <IconButton label="加入购买清单" onClick={() => addToCart(item)} disabled={!itemPurchasable(item)}><ShoppingBag size={15}/></IconButton>
                        <IconButton label="使用已保存配置一键购买" tone="buy" onClick={() => oneClickBuy(item)} disabled={!itemPurchasable(item) || busy[`buy-${item.id}`]}><Zap size={15} className={busy[`buy-${item.id}`] ? 'spin' : ''}/></IconButton>
                        <IconButton label="删除监控" tone="danger" onClick={() => removeWatch(item)}><Trash2 size={15}/></IconButton>
                      </div>
                    </div>
                  ))}
                </div>

              </section>

              <PurchasePanel items={items} cart={cart} setCart={setCart} totalCart={totalCart} estimatedTotal={estimatedTotal} setQuantity={setQuantity} contact={contact} setContact={setContact} saveCheckout={saveCheckout} queryPassword={queryPassword} setQueryPassword={setQueryPassword} passwordVisible={passwordVisible} setPasswordVisible={setPasswordVisible} paymentChannel={paymentChannel} setPaymentChannel={setPaymentChannel} prepareCheckout={prepareCheckout} busy={busy}/>
            </div>
          </>
        ) : activeView === 'history' ? (
          <section className="history-view">
            <div className="history-sidebar"><div className="section-heading"><div><h2>商品</h2><p>选择查看记录</p></div></div>{items.map(item => <button className={item.id === selectedId ? 'active' : ''} key={item.id} onClick={() => setSelectedId(item.id)}><ProductImage item={item}/><span><strong>{item.latest?.title || item.name}</strong><small>{money(item.latest?.price)}</small></span><ChevronRight size={16}/></button>)}</div>
            <div className="history-main"><div className="history-top"><div><span>最近 60 次记录</span><h2>{latest?.title || '请选择商品'}</h2></div><strong>{money(latest?.price)}</strong></div><PriceBars history={history}/><div className="history-table"><div className="history-row head"><span>抓取时间</span><span>价格</span><span>在售状态</span><span>结果</span></div>{[...history].reverse().map((point, index) => <div className="history-row" key={`${point.id}-${index}`}><span>{compactTime(point.fetched_at)}</span><strong>{money(point.price)}</strong><span>{point.sale_status === 'on_sale' ? '在售' : point.sale_status === 'off_sale' ? '未上架' : '--'}</span><span className={point.status === 'success' ? 'positive' : 'negative'}>{point.status === 'success' ? '成功' : point.error || '失败'}</span></div>)}</div></div>
          </section>
        ) : activeView === 'reclaim' ? (
          <ReclaimView config={redeemConfig} setConfig={setRedeemConfig} cardCodes={cardCodes} setCardCodes={setCardCodes} result={reclaimResult} busy={reclaimBusy} onSave={saveRedeemConfig} onRun={runReclaim} onDownload={downloadReclaimed} onImport={() => { setActiveView('sub2api'); if (reclaimPayload) { setSub2apiPayload(reclaimPayload); setSub2apiFileName('找回结果.json'); } }}/>
        ) : (
          <Sub2ApiView config={sub2apiConfig} setConfig={setSub2apiConfig} adminKey={sub2apiAdminKey} setAdminKey={setSub2apiAdminKey} fileName={sub2apiFileName} payload={sub2apiPayload} result={sub2apiResult} busy={sub2apiBusy} optionsBusy={sub2apiOptionsBusy} options={sub2apiOptions} proxyChoice={sub2apiProxyChoice} groupIds={sub2apiGroupIds} codexFingerprintMode={sub2apiCodexFingerprintMode} onCodexFingerprintMode={setSub2apiCodexFingerprintMode} reclaimBusy={sub2apiReclaimBusy} reclaimResult={sub2apiReclaimResult} onReclaim401={reclaimSub2Api401} automation={sub2apiAutomation} automationState={sub2apiAutomationState} automationBusy={sub2apiAutomationBusy} onAutomationChange={setSub2apiAutomation} onSaveAutomation={saveSub2ApiAutomation} onRunAutomation={runSub2ApiAutomation} onSave={saveSub2ApiConfig} onTest={testSub2Api} onLoadOptions={() => loadSub2ApiOptions()} onProxyChoice={changeSub2ApiProxy} onToggleGroup={toggleSub2ApiGroup} onFile={parseSub2ApiFile} onFiles={loadSub2ApiFiles} onImport={() => importSub2Api()}/>
        )}
      </main>

      <ProductOverviewDrawer id="product-overview-drawer" open={overviewOpen} items={items} shops={shops} stableOrder={stableItemOrder} busy={busy} selectedId={selectedId} shopFilter={shopFilter} onClose={() => setOverviewOpen(false)} onSelect={setSelectedId} onOpenDetail={openProductDetail} onBuy={oneClickBuy} onAdd={addToCart} onDirect={openDirectProduct} onRefresh={fetchOne} onShopFilter={value => { setShopFilter(value); setCheckedIds([]); }}/>
      <ProductDetailDrawer open={detailOpen} item={selected} history={history} priceDelta={selectedPriceDelta} lowestPrice={localLowestPrice} busy={busy} onClose={() => setDetailOpen(false)} onBuy={oneClickBuy} onAdd={addToCart} onRefresh={fetchOne} onDirect={openDirectProduct}/>

      {preorderDraft && <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && setPreorderDraft(null)}><div className="checkout-modal preorder-modal" role="dialog" aria-modal="true" aria-label="设置自动预购"><div className="modal-head"><div><span>STOCK PREORDER</span><h2>设置自动预购</h2></div><IconButton label="关闭" onClick={() => setPreorderDraft(null)}><X size={17}/></IconButton></div><div className="preorder-config"><label className="preorder-enable"><input type="checkbox" checked={preorderDraft.enabled} onChange={event => setPreorderDraft({...preorderDraft, enabled: event.target.checked})}/><span><strong>启用自动预购</strong><small>仅缺货商品进入监控，有货商品不会创建任务</small></span></label><label className="preorder-interval"><span>库存检查间隔</span><div><input type="number" min="1" max="86400" value={preorderDraft.interval_seconds} onChange={event => setPreorderDraft({...preorderDraft, interval_seconds: Math.max(1, Math.min(86400, Number(event.target.value) || 1))})} inputMode="numeric"/><span>秒</span></div></label></div><div className="preorder-items">{preorderDraft.items.map(entry => { const eligible = entry.sale_status === 'on_sale' && entry.stock !== null && Number(entry.stock) === 0; return <div className={`preorder-item ${eligible ? '' : 'unavailable'}`} key={entry.watch_id}><div><strong>{entry.title}</strong><small>当前库存：{entry.stock_label}{entry.minimum > 1 ? ` · 最低 ${entry.minimum} 件起购` : ''}</small></div>{eligible ? <label><span>预购数量</span><input type="number" min={entry.minimum} max="99" value={entry.quantity} onChange={event => updatePreorderQuantity(entry.watch_id, event.target.value)} inputMode="numeric"/></label> : <span className="pill paused">{entry.sale_status === 'off_sale' ? '未上架' : entry.stock === null ? '库存未知' : '当前有货'}</span>}</div>; })}</div><div className={`preorder-checkout-status ${savedCheckout.contact ? 'ready' : 'missing'}`}><ShieldCheck size={16}/><span>{savedCheckout.contact ? `使用已保存联系方式 · ${Number(savedCheckout.channel_id) === 4 ? '微信支付' : '支付宝'}` : '请先在右侧购买配置中保存联系方式'}</span></div><div className="modal-foot"><span><Clock3 size={14}/>库存达到预购数量后只创建一次支付链接</span><div className="modal-foot-actions"><button className="button secondary" onClick={() => setPreorderDraft(null)}>取消</button><button className="button official" onClick={savePreorders} disabled={!preorderDraft.enabled || !savedCheckout.contact || busy.preorder || !preorderDraft.items.some(entry => entry.sale_status === 'on_sale' && entry.stock !== null && Number(entry.stock) === 0)}><Zap size={15}/>{busy.preorder ? '正在保存' : '启用预购'}</button></div></div></div></div>}
      {review && <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && setReview(null)}><div className="checkout-modal" role="dialog" aria-modal="true" aria-label="购买确认"><div className="modal-head"><div><span>DIRECT CHECKOUT</span><h2>支付链接已准备</h2></div><IconButton label="关闭" onClick={() => setReview(null)}><X size={17}/></IconButton></div><div className="modal-notice"><ShieldCheck size={18}/><p>{review.notice} 创建成功后会自动打开支付页面；下方仍保留“打开支付链接”入口，方便重复打开。</p></div><div className="review-list">{review.items.map(item => <div className="review-item" key={item.watch_id}><div><strong>{item.title}</strong><span>{money(item.unit_price)} × {item.quantity}</span><a className="payment-link" href={item.official_url} target="_blank" rel="noreferrer"><Link2 size={13}/>{item.official_url}</a></div><strong>{money(item.subtotal)}</strong><div className="review-actions"><button className="button secondary" onClick={() => copyPaymentLink(item)}><Clipboard size={15}/>复制商品链接</button></div></div>)}</div>{officialOrder && <div className="payment-order-result"><div><span>官方订单</span><strong>{officialOrder.trade_no}</strong></div><a href={officialOrder.payment_url} target="_blank" rel="noreferrer"><Link2 size={14}/>{officialOrder.payment_url}</a><small>{officialOrder.notice} 渠道：{officialOrder.channel === 'alipay' ? '支付宝' : '微信支付'}，金额：{money(officialOrder.amount)}</small><button className="button official" onClick={() => window.open(officialOrder.payment_url, '_blank', 'noopener,noreferrer')}><ArrowUpRight size={15}/>打开支付链接</button></div>}<div className="review-total"><span>清单合计</span><strong>{money(review.total)}</strong></div><div className="modal-foot"><span><ShieldCheck size={14}/>支付前请核对订单金额</span><div className="modal-foot-actions"><label className="payment-channel"><span>支付渠道</span><select value={paymentChannel} onChange={event => setPaymentChannel(Number(event.target.value))}>{paymentChannels.map(channel => <option value={channel.id} key={channel.id}>{channel.name}</option>)}</select></label><button className="button official auto-pay-button" onClick={createOfficialOrder} disabled={busy.officialOrder}><Package size={15}/>{busy.officialOrder ? '正在创建并跳转' : '创建订单并自动跳转'}</button><button className="button secondary" onClick={() => setReview(null)}>返回修改</button></div></div></div></div>}
      {toast && <div className={`toast ${toast.type}`}><span>{toast.type === 'error' ? <AlertCircle size={17}/> : <Check size={17}/>}</span>{toast.message}</div>}
    </div>
  );
}

function ReclaimView({config, setConfig, cardCodes, setCardCodes, result, busy, onSave, onRun, onDownload, onImport}) {
  const tasks = result?.all_tasks || [];
  return (
    <section className="tool-view">
      <div className="tool-grid">
        <div className="tool-panel">
          <div className="section-heading"><div><span className="detail-kicker">REDEEM SERVICE</span><h2>401 找回服务</h2><p>卡密只发送到配置的服务地址</p></div><KeyRound size={22}/></div>
          <label><span>服务地址</span><input value={config.base_url} onChange={event => setConfig({...config, base_url: event.target.value})} placeholder="https://30d.team"/></label>
          <button className="button secondary tool-save" onClick={onSave}><Save size={15}/>保存地址</button>
          <label className="code-field"><span>卡密列表</span><textarea value={cardCodes} onChange={event => setCardCodes(event.target.value)} placeholder="每行输入一个卡密" rows={9}/></label>
          <div className="tool-actions"><button className="button secondary" onClick={() => onRun('health')} disabled={busy}><Activity size={15}/>检测 401</button><button className="button primary" onClick={() => onRun('reclaim')} disabled={busy}><Zap size={15}/>只找回 401</button><button className="button secondary" onClick={() => onRun('progress')} disabled={busy}><RefreshCw size={15}/>刷新进度</button></div>
        </div>
        <div className="tool-panel result-panel">
          <div className="section-heading"><div><span className="detail-kicker">RESULT</span><h2>任务结果</h2></div>{busy && <RefreshCw className="spin" size={18}/>}</div>
          {!result ? <div className="tool-empty"><KeyRound size={28}/><span>尚未执行检测</span></div> : <>
            <div className="result-metrics"><div><span>总数</span><strong>{result.total ?? result.requested_cards ?? '--'}</strong></div><div><span>需找回</span><strong>{result.need_reclaim ?? result.queued ?? '--'}</strong></div><div><span>已完成</span><strong>{result.done ?? '--'}</strong></div><div><span>失败</span><strong>{result.failed ?? '--'}</strong></div></div>
            <div className="task-list">{tasks.length ? tasks.map((task, index) => <div className="task-row" key={`${task.order_no || task.card_code}-${index}`}><div><strong>{task.card_code || task.order_no || `任务 ${index + 1}`}</strong><small>{task.message || task.status || '处理中'}</small></div>{task.download_token && task.order_no && <button className="icon-button" title="下载恢复 JSON" aria-label="下载恢复 JSON" onClick={() => onDownload(task)}><FileUp size={15}/></button>}</div>) : <p className="tool-muted">当前没有可下载任务</p>}</div>
            {result.ok && <button className="button secondary import-recovered" onClick={onImport}><Upload size={15}/>转到 Sub2API 导入</button>}
          </>}
        </div>
      </div>
    </section>
  );
}

function Sub2ApiView({config, setConfig, adminKey, setAdminKey, fileName, payload, result, busy, optionsBusy, options, proxyChoice, groupIds, codexFingerprintMode, onCodexFingerprintMode, reclaimBusy, reclaimResult, onReclaim401, automation, automationState, automationBusy, onAutomationChange, onSaveAutomation, onRunAutomation, onSave, onTest, onLoadOptions, onProxyChoice, onToggleGroup, onFile, onFiles, onImport}) {
  const [dragging, setDragging] = useState(false);
  const accountCount = Array.isArray(payload?.accounts) ? payload.accounts.length : 0;
  const jsonProxyCount = Array.isArray(payload?.proxies) ? payload.proxies.length : 0;
  const importResult = result?.mode ? result.result : null;
  const successCount = importResult?.success ?? importResult?.account_created;
  const failedCount = importResult?.failed ?? importResult?.account_failed;
  const fingerprint = result?.fingerprint_verification;
  const automationReady = Boolean(config.admin_key_set && proxyChoice.startsWith('proxy:') && groupIds.length && codexFingerprintMode !== 'off' && automation.auto_import);
  const automationResult = automationState?.last_result;
  return (
    <section className="tool-view">
      <p className="tool-muted">Sub2API 管理员 API Key 通过 x-api-key 发送，不是 JWT。</p>
      <div className="tool-grid">
        <div className="tool-panel">
          <div className="section-heading"><div><span className="detail-kicker">SUB2API ADMIN</span><h2>Sub2API 配置</h2><p>管理员密钥仅通过后端代理发送</p></div><Upload size={22}/></div>
          <label><span>Sub2API 地址</span><input value={config.base_url} onChange={event => setConfig({...config, base_url: event.target.value})} placeholder="http://127.0.0.1:8080"/></label>
          <label><span>管理员密钥 {config.admin_key_set && <small>已保存 {config.admin_key_mask}</small>}</span><input type="password" value={adminKey} onChange={event => setAdminKey(event.target.value)} placeholder={config.admin_key_set ? '留空以保留现有密钥' : '粘贴管理员密钥'} autoComplete="off"/></label>
          <div className="tool-actions"><button className="button primary" onClick={onSave}><Save size={15}/>保存配置</button><button className="button secondary" onClick={onTest} disabled={busy}><Activity size={15}/>测试连接</button><button className="button secondary" onClick={onReclaim401} disabled={reclaimBusy || busy}><KeyRound size={15}/>{reclaimBusy ? '正在扫描' : '一键找回 401'}</button></div>
          {result && <div className={`connection-result ${result.ok ? 'ok' : 'bad'}`}>{result.ok ? `连接成功${result.account_count == null ? '' : `，账号 ${result.account_count} 个`}` : (result.detail || `上游 HTTP ${result.upstream_status}`)}</div>}
          {reclaimResult && <div className={`connection-result ${reclaimResult.ok ? 'ok' : 'bad'}`}>扫描 {reclaimResult.scanned_accounts} 个账号，明确 401 为 {reclaimResult.accounts_401} 个，提交卡密 {reclaimResult.card_code_count} 个，跳过其他错误 {reclaimResult.skipped_non_401} 个</div>}
        </div>
        <div className="tool-panel">
          <div className="section-heading"><div><span className="detail-kicker">ACCOUNT JSON</span><h2>一键导入账号</h2><p>支持 sub2api-data / sub2api-bundle</p></div><FileUp size={22}/></div>
          <label className={`file-drop ${dragging ? 'dragging' : ''}`} onDragEnter={event => { event.preventDefault(); setDragging(true); }} onDragOver={event => event.preventDefault()} onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget)) setDragging(false); }} onDrop={event => { event.preventDefault(); setDragging(false); onFiles(event.dataTransfer.files); }}><input type="file" accept="application/json,.json" multiple onChange={onFile}/><FileUp size={24}/><strong>{fileName || '选择或拖入账号 JSON 文件'}</strong><small>{accountCount ? `${accountCount} 个账号待导入` : '可同时选择或拖入多个 JSON'}</small></label>
          {payload && <pre className="json-preview">{JSON.stringify({type: payload.type, version: payload.version, files: fileName, proxies: Array.isArray(payload.proxies) ? payload.proxies.length : 0, accounts: accountCount}, null, 2)}</pre>}
          <div className="import-options">
            <div className="import-options-head"><div><strong>代理与分组</strong><small>{options.loaded ? `${options.proxy_count} 个代理 · ${options.group_count} 个分组` : '尚未加载'}</small></div><button className="icon-button" title="刷新代理和分组" aria-label="刷新代理和分组" onClick={onLoadOptions} disabled={optionsBusy}><RefreshCw size={15} className={optionsBusy ? 'spin' : ''}/></button></div>
            <label><span>导入代理</span><select value={proxyChoice} onChange={event => onProxyChoice(event.target.value)}><option value="json">JSON 自带代理（{jsonProxyCount}）</option><option value="none">不绑定代理</option>{options.proxies.map(proxy => <option value={`proxy:${proxy.id}`} key={proxy.id}>{proxy.name} · {proxy.protocol}://{proxy.host}:{proxy.port}</option>)}</select></label>
            <label><span>Codex 指纹收敛（OpenAI OAuth / Setup Token）</span><select data-testid="codex-fingerprint-mode" value={codexFingerprintMode} onChange={event => onCodexFingerprintMode(event.target.value)}><option value="off">关闭（透传，默认）</option><option value="device">仅设备</option><option value="session">设备 + 会话</option><option value="full">完全收敛</option></select></label>
            <div className="group-picker"><span>导入分组</span>{options.groups.length ? <div>{options.groups.map(group => <label key={group.id}><input type="checkbox" checked={groupIds.includes(group.id)} onChange={() => onToggleGroup(group.id)}/><span><strong>{group.name}</strong><small>{group.platform || '通用'} · {group.account_count} 个账号</small></span></label>)}</div> : <p>{options.loaded ? 'Sub2API 当前没有可用分组' : '保存配置后加载分组'}</p>}</div>
          </div>
          <button className="button primary import-button" onClick={onImport} disabled={!payload || busy}><Upload size={15}/>{busy ? '正在导入' : '一键导入账号'}</button>
          {importResult && <div className="connection-result ok">导入完成（HTTP {result.upstream_status}）{successCount == null ? '' : `，成功 ${successCount}`}{failedCount == null ? '' : `，失败 ${failedCount}`}</div>}
          {fingerprint && <div className={`connection-result ${fingerprint.unresolved || fingerprint.error ? 'bad' : 'ok'}`}>指纹模式 {fingerprint.mode}：符合 {fingerprint.eligible} 个，已核对 {fingerprint.matched} 个，直接生效 {fingerprint.verified - fingerprint.repaired} 个，补写 {fingerprint.repaired} 个，未匹配 {fingerprint.unresolved} 个{fingerprint.error ? `；核对失败：${fingerprint.error}` : ''}</div>}
        </div>
      </div>
      <div className="automation-band">
        <div className="automation-head">
          <div><span className="detail-kicker">401 AUTOMATION</span><h2>定时找回与自动导入</h2></div>
          <div className={`automation-readiness ${automationReady ? 'ready' : ''}`}><ShieldCheck size={15}/>{automationReady ? '配置完整' : '等待代理、分组与指纹配置'}</div>
        </div>
        <div className="automation-controls">
          <label className="automation-toggle"><button className={`switch ${automation.enabled ? 'on' : ''}`} role="switch" aria-checked={automation.enabled} title={automation.enabled ? '关闭自动监控' : '开启自动监控'} disabled={!automation.enabled && !automationReady} onClick={() => onAutomationChange(current => ({...current, enabled: !current.enabled}))}><span/></button><span><strong>自动监控 401</strong><small>仅处理明确的 401 授权错误</small></span></label>
          <label className="automation-toggle"><input type="checkbox" checked={automation.auto_import} onChange={event => onAutomationChange(current => ({...current, auto_import: event.target.checked}))}/><span><strong>找回后自动导入</strong><small>使用上方代理、分组和指纹设置</small></span></label>
          <label className="automation-interval"><span>监控间隔</span><div><input type="number" min="10" max="86400" value={automation.interval_seconds} onChange={event => onAutomationChange(current => ({...current, interval_seconds: Math.max(10, Math.min(86400, Number(event.target.value) || 10))}))} inputMode="numeric"/><span>秒</span></div></label>
          <div className="automation-actions"><button className="button primary" onClick={onSaveAutomation} disabled={automationBusy}><Save size={15}/>{automationBusy ? '正在处理' : '保存自动化'}</button><button className="button secondary" onClick={onRunAutomation} disabled={automationBusy || !automation.enabled}><RefreshCw size={15} className={automationBusy ? 'spin' : ''}/>立即检查</button></div>
        </div>
        <div className={`automation-status ${automationState?.last_error ? 'bad' : automationResult ? 'ok' : ''}`}>
          <span>最近运行：{automationState?.last_run ? compactTime(automationState.last_run) : '尚未运行'}</span>
          {automationState?.last_error ? <strong>{automationState.last_error}</strong> : automationResult ? <strong>扫描 {automationResult.scanned_accounts ?? 0} 个 · 401 {automationResult.accounts_401 ?? 0} 个 · 下载 {automationResult.downloaded ?? 0} 个 · {automationResult.imported ? '已自动导入' : '无需导入'}</strong> : <strong>等待首次检查</strong>}
          {automationState?.pending_card_codes?.length > 0 && <small>{automationState.pending_card_codes.length} 个卡密正在等待找回</small>}
        </div>
      </div>
    </section>
  );
}

function PurchasePanel({items, cart, setCart, totalCart, estimatedTotal, setQuantity, contact, setContact, saveCheckout, queryPassword, setQueryPassword, passwordVisible, setPasswordVisible, paymentChannel, setPaymentChannel, prepareCheckout, busy}) {
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
      <p className="local-note"><ShieldCheck size={14}/>配置保存在本机 SQLite；商品不要求密码时不会提交密码</p>
    </aside>
  );
}

createRoot(document.getElementById('root')).render(<App/>);
