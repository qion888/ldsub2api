import React, {useMemo} from 'react';
import {BarChart3, Clock3} from 'lucide-react';
import {
  Area,
  Bar,
  Brush,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

function money(value) {
  if (value === null || value === undefined || value === '') return '--';
  const number = Number(value);
  return Number.isFinite(number) ? `¥${number.toFixed(2)}` : '--';
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

function stockMeta(point) {
  const raw = point?.stock;
  if (raw === null || raw === undefined || raw === '' || !Number.isFinite(Number(raw))) {
    return {key: 'unknown', label: point?.stock_label || '库存未知', value: null};
  }
  const value = Number(raw);
  return value > 0 ? {key: 'in', label: '有货', value} : {key: 'out', label: '缺货', value: 0};
}

function fullTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(date);
}

function normalizeChartPoints(points) {
  let previousPrice = null;
  return (points || [])
    .filter(point => point.status === 'success')
    .map(point => {
      const timestamp = new Date(point.fetched_at).getTime();
      const price = point.price === null || point.price === '' || !Number.isFinite(Number(point.price)) ? null : Number(point.price);
      return {...point, timestamp, price, stock: stockMeta(point).value};
    })
    .filter(point => Number.isFinite(point.timestamp) && (point.price !== null || point.stock !== null))
    .sort((left, right) => left.timestamp - right.timestamp)
    .map(point => {
      const priceDelta = point.price !== null && previousPrice !== null ? point.price - previousPrice : null;
      const priceDeltaPercent = priceDelta !== null && previousPrice ? priceDelta / previousPrice * 100 : null;
      if (point.price !== null) previousPrice = point.price;
      return {...point, priceDelta, priceDeltaPercent};
    });
}

function timeTickFormatter(firstTimestamp, lastTimestamp) {
  const spansMultipleDays = lastTimestamp - firstTimestamp > 48 * 60 * 60 * 1000;
  return value => {
    const date = new Date(value);
    return spansMultipleDays
      ? `${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')}`
      : `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  };
}

function compactDateRange(firstTimestamp, lastTimestamp) {
  const format = value => {
    const date = new Date(value);
    return `${String(date.getFullYear()).slice(-2)}/${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  };
  return firstTimestamp === lastTimestamp ? format(firstTimestamp) : `${format(firstTimestamp)} - ${format(lastTimestamp)}`;
}

function priceScale(minimum, maximum) {
  const padding = Math.max((maximum - minimum) * .16, Math.abs(maximum || 1) * .015, .5);
  const targetStep = ((maximum + padding) - Math.max(0, minimum - padding)) / 4;
  const magnitude = 10 ** Math.floor(Math.log10(targetStep || 1));
  const fraction = targetStep / magnitude;
  const niceFraction = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10;
  const step = niceFraction * magnitude;
  const lower = Math.max(0, Math.floor((minimum - padding) / step) * step);
  const upper = Math.ceil((maximum + padding) / step) * step;
  const count = Math.round((upper - lower) / step);
  return {
    domain: [lower, upper],
    ticks: Array.from({length: count + 1}, (_, index) => Number((lower + index * step).toFixed(6))),
  };
}

function ChartTooltip({active, payload}) {
  const point = active && payload?.[0]?.payload;
  if (!point) return null;
  const currentStock = stockMeta(point);
  return <div className="history-chart-tooltip-detail">
    <div className="history-tooltip-time"><Clock3 size={13}/><span>{fullTime(point.fetched_at)}</span></div>
    <dl><dt>成交价格</dt><dd>{money(point.price)}</dd></dl>
    <dl><dt>较上次</dt><dd className={point.priceDelta > 0 ? 'negative' : point.priceDelta < 0 ? 'positive' : ''}>{point.priceDelta === null ? '--' : `${signedMoney(point.priceDelta)} · ${signedPercent(point.priceDeltaPercent)}`}</dd></dl>
    <dl><dt>库存数量</dt><dd className={currentStock.key === 'out' ? 'negative' : currentStock.key === 'in' ? 'positive' : ''}>{currentStock.value === null ? currentStock.label : `${currentStock.value} · ${currentStock.label}`}</dd></dl>
    <dl><dt>销售状态</dt><dd>{point.sale_status === 'on_sale' ? '在售' : point.sale_status === 'off_sale' ? '已下架' : '未知'}</dd></dl>
  </div>;
}

export default function PriceHistoryChart({points, stats, mode = 'combined', comparison = 'average'}) {
  const chartPoints = useMemo(() => normalizeChartPoints(points), [points]);

  if (!chartPoints.length) return <div className="chart-empty"><BarChart3 size={24}/><strong>暂无可绘制的有效快照</strong><span>完成成功抓取后，价格与库存趋势会显示在这里</span></div>;

  const firstTimestamp = chartPoints[0].timestamp;
  const lastTimestamp = chartPoints[chartPoints.length - 1].timestamp;
  const tickTime = timeTickFormatter(firstTimestamp, lastTimestamp);
  const comparisonMap = {
    previous: {label: '上次价格', value: Number(stats?.previous_price)},
    average: {label: '区间均价', value: Number(stats?.average_price)},
    lowest: {label: '区间最低', value: Number(stats?.min_price)},
    highest: {label: '区间最高', value: Number(stats?.max_price)},
  };
  const baseline = comparisonMap[comparison];
  const showPrice = mode !== 'inventory';
  const showStock = mode !== 'price';

  return <div className="history-chart-wrap">
    <div className="history-chart-canvas" role="img" aria-label="价格与库存双轴历史趋势图">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={chartPoints} margin={{top: 17, right: 8, bottom: chartPoints.length > 8 ? 7 : 0, left: 2}}>
          <CartesianGrid vertical={false} stroke="var(--ui-border-soft)" strokeDasharray="3 4"/>
          <XAxis dataKey="timestamp" type="number" domain={['dataMin', 'dataMax']} scale="time" tickFormatter={tickTime} minTickGap={42} tick={{fill: 'var(--ui-text-faint)', fontSize: 10}} tickLine={false} axisLine={{stroke: 'var(--ui-border)'}}/>
          <YAxis yAxisId="price" hide={!showPrice} width={66} domain={['auto', 'auto']} tickFormatter={value => money(value)} tick={{fill: 'var(--ui-text-muted)', fontSize: 10}} tickLine={false} axisLine={false}/>
          <YAxis yAxisId="stock" hide={!showStock} orientation="right" width={38} allowDecimals={false} domain={[0, 'auto']} tick={{fill: 'var(--ui-text-muted)', fontSize: 10}} tickLine={false} axisLine={false}/>
          <Tooltip content={<ChartTooltip/>} cursor={{stroke: 'var(--ui-text-muted)', strokeDasharray: '3 3'}} isAnimationActive={false}/>
          {showStock && <Bar yAxisId="stock" dataKey="stock" name="库存" maxBarSize={18} minPointSize={2} radius={[2, 2, 0, 0]} isAnimationActive={false}>
            {chartPoints.map((point, index) => <Cell key={`${point.id || index}-stock`} fill={point.stock === 0 ? 'var(--ui-danger)' : 'var(--history-stock)'}/>) }
          </Bar>}
          {showPrice && <Line yAxisId="price" type="stepAfter" dataKey="price" name="价格" stroke="var(--history-price)" strokeWidth={2.5} connectNulls dot={chartPoints.length <= 80 ? {r: 2.5, fill: 'var(--ui-surface)', strokeWidth: 2} : false} activeDot={{r: 5, strokeWidth: 2, fill: 'var(--ui-surface)'}} isAnimationActive={false}/>}
          {showPrice && baseline && Number.isFinite(baseline.value) && <ReferenceLine yAxisId="price" y={baseline.value} ifOverflow="extendDomain" stroke="var(--history-reference)" strokeDasharray="6 5" label={{value: `${baseline.label} ${money(baseline.value)}`, position: 'insideTopRight', fill: 'var(--ui-text-muted)', fontSize: 10}}/>}
          {chartPoints.length > 8 && <Brush dataKey="timestamp" height={28} travellerWidth={9} tickFormatter={tickTime} stroke="var(--ui-border)" fill="var(--ui-surface-muted)"/>}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
    <div className="history-chart-foot">
      <div className="history-chart-legend"><span><i className="legend-line"/>价格</span>{showStock && <span><i className="legend-bar"/>库存</span>}{comparison !== 'none' && showPrice && <span><i className="legend-reference"/>对比基准</span>}</div>
      <span>拖动底部选择器可放大区间 · 共 {chartPoints.length} 个快照</span>
    </div>
  </div>;
}

export function ProductDetailPriceChart({points}) {
  const chartPoints = useMemo(
    () => normalizeChartPoints(points).filter(point => point.price !== null),
    [points],
  );

  if (!chartPoints.length) return <div className="detail-price-chart-empty"><BarChart3 size={24}/><strong>暂无有效价格</strong><span>完成一次成功抓取后，将按真实价格和时间绘制走势</span></div>;

  const prices = chartPoints.map(point => point.price);
  const minimum = Math.min(...prices);
  const maximum = Math.max(...prices);
  const average = prices.reduce((sum, value) => sum + value, 0) / prices.length;
  const scale = priceScale(minimum, maximum);
  const firstTimestamp = chartPoints[0].timestamp;
  const lastTimestamp = chartPoints[chartPoints.length - 1].timestamp;
  const timePadding = firstTimestamp === lastTimestamp ? 30 * 60 * 1000 : 0;
  const timeDomain = [firstTimestamp - timePadding, lastTimestamp + timePadding];
  const tickTime = timeTickFormatter(firstTimestamp, lastTimestamp);
  const minimumPoint = chartPoints.find(point => point.price === minimum);
  const maximumPoint = [...chartPoints].reverse().find(point => point.price === maximum);
  const showExtremes = minimum !== maximum && chartPoints.length > 1;
  const current = chartPoints[chartPoints.length - 1];

  return <div className="detail-price-chart">
    <div className="detail-chart-context">
      <span><i className="legend-line"/>报价曲线</span>
      <span>{compactDateRange(firstTimestamp, lastTimestamp)}</span>
    </div>
    <div className="detail-price-chart-canvas" role="img" aria-label={`商品价格趋势图，共 ${chartPoints.length} 个有效价格，当前价格 ${money(current.price)}`}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={chartPoints} margin={{top: 25, right: 15, bottom: 3, left: 1}}>
          <defs>
            <linearGradient id="detailPriceFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--history-price)" stopOpacity={.2}/>
              <stop offset="100%" stopColor="var(--history-price)" stopOpacity={.02}/>
            </linearGradient>
          </defs>
          <CartesianGrid vertical={false} stroke="var(--ui-border-soft)" strokeDasharray="3 4"/>
          <XAxis dataKey="timestamp" type="number" domain={timeDomain} scale="time" tickFormatter={tickTime} minTickGap={48} tick={{fill: 'var(--ui-text-faint)', fontSize: 10}} tickLine={false} axisLine={{stroke: 'var(--ui-border)'}}/>
          <YAxis yAxisId="price" width={65} domain={scale.domain} ticks={scale.ticks} tickFormatter={value => money(value)} tick={{fill: 'var(--ui-text-muted)', fontSize: 10}} tickLine={false} axisLine={false}/>
          <Tooltip content={<ChartTooltip/>} cursor={{stroke: 'var(--ui-text-muted)', strokeDasharray: '3 3'}} isAnimationActive={false}/>
          <ReferenceLine yAxisId="price" y={average} stroke="var(--history-reference)" strokeDasharray="5 5" label={{value: `均价 ${money(average)}`, position: 'insideTopLeft', fill: 'var(--ui-text-muted)', fontSize: 10}}/>
          <Area yAxisId="price" type="stepAfter" dataKey="price" fill="url(#detailPriceFill)" stroke="none" isAnimationActive={false}/>
          <Line yAxisId="price" type="stepAfter" dataKey="price" name="价格" stroke="var(--history-price)" strokeWidth={2.5} connectNulls dot={chartPoints.length <= 48 ? {r: 2.5, fill: 'var(--ui-surface)', strokeWidth: 2} : false} activeDot={{r: 5, strokeWidth: 2, fill: 'var(--ui-surface)'}} isAnimationActive={false}/>
          {showExtremes && <ReferenceDot yAxisId="price" x={minimumPoint.timestamp} y={minimum} r={3.5} fill="var(--ui-success)" stroke="var(--ui-surface)" strokeWidth={2} label={{value: `低 ${money(minimum)}`, position: 'bottom', fill: 'var(--ui-success)', fontSize: 9}}/>}
          {showExtremes && <ReferenceDot yAxisId="price" x={maximumPoint.timestamp} y={maximum} r={3.5} fill="var(--ui-danger)" stroke="var(--ui-surface)" strokeWidth={2} label={{value: `高 ${money(maximum)}`, position: 'top', fill: 'var(--ui-danger)', fontSize: 9}}/>}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
    <div className="detail-chart-foot">
      <div><span><i className="legend-line"/>实际价格</span><span><i className="legend-reference"/>历史均价</span></div>
      <span>共 {chartPoints.length} 个有效价格 · 波动 {money(maximum - minimum)}</span>
    </div>
  </div>;
}
