import assert from 'node:assert/strict';
import test from 'node:test';

import {
  clampProductPage,
  paginateProducts,
  productPageCount,
  PRODUCT_PAGE_SIZE_OPTIONS,
} from './productPagination.js';

test('calculates product pages and clamps invalid pages', () => {
  assert.deepEqual(PRODUCT_PAGE_SIZE_OPTIONS, [24, 48, 96]);
  assert.equal(productPageCount(0, 24), 1);
  assert.equal(productPageCount(49, 24), 3);
  assert.equal(clampProductPage(9, 49, 24), 3);
  assert.equal(clampProductPage(0, 49, 24), 1);
});

test('returns only the requested product page', () => {
  const items = Array.from({length: 5}, (_, index) => index + 1);
  assert.deepEqual(paginateProducts(items, 2, 2), [3, 4]);
  assert.deepEqual(paginateProducts(items, 9, 2), [5]);
  assert.deepEqual(paginateProducts(null, 1, 24), []);
});
