export const DEFAULT_PRODUCT_PAGE_SIZE = 24;
export const PRODUCT_PAGE_SIZE_OPTIONS = Object.freeze([24, 48, 96]);

function normalizedPageSize(pageSize) {
  const value = Number(pageSize);
  return Number.isInteger(value) && value > 0 ? value : DEFAULT_PRODUCT_PAGE_SIZE;
}

export function productPageCount(total, pageSize = DEFAULT_PRODUCT_PAGE_SIZE) {
  const count = Number(total);
  if (!Number.isFinite(count) || count <= 0) return 1;
  return Math.max(1, Math.ceil(count / normalizedPageSize(pageSize)));
}

export function clampProductPage(page, total, pageSize = DEFAULT_PRODUCT_PAGE_SIZE) {
  const value = Number(page);
  const safePage = Number.isInteger(value) && value > 0 ? value : 1;
  return Math.min(safePage, productPageCount(total, pageSize));
}

export function paginateProducts(items, page, pageSize = DEFAULT_PRODUCT_PAGE_SIZE) {
  const source = Array.isArray(items) ? items : [];
  const size = normalizedPageSize(pageSize);
  const currentPage = clampProductPage(page, source.length, size);
  const start = (currentPage - 1) * size;
  return source.slice(start, start + size);
}
