import test from 'node:test';
import assert from 'node:assert/strict';
import { extension, formatBytes, formatDuration, formatMinutes, formatRanges, parseRanges, plural, stem } from '../src/client/format.js';

test('bytes are shown in the largest sensible unit', () => {
  assert.equal(formatBytes(0), '0 B');
  assert.equal(formatBytes(1023), '1023 B');
  assert.equal(formatBytes(1024), '1.0 KB');
  assert.equal(formatBytes(1536), '1.5 KB');
  assert.equal(formatBytes(5 * 1024 * 1024), '5.0 MB');
  assert.equal(formatBytes(250 * 1024 * 1024), '250 MB');
  assert.equal(formatBytes(-1), '–');
  assert.equal(formatBytes(NaN), '–');
});

test('durations and reading times', () => {
  assert.equal(formatDuration(420), '420 ms');
  assert.equal(formatDuration(2500), '2.5 s');
  assert.equal(formatDuration(125000), '2 min 5 s');
  assert.equal(formatMinutes(0), '0 min');
  assert.equal(formatMinutes(0.4), '24 sec');
  assert.equal(formatMinutes(3.26), '3.3 min');
  assert.equal(formatMinutes(42), '42 min');
});

test('names split at the last dot only', () => {
  assert.equal(stem('report.final.pdf'), 'report.final');
  assert.equal(extension('Report.PDF'), 'pdf');
  assert.equal(stem('.hidden'), '.hidden');
  assert.equal(extension('noext'), '');
});

test('page ranges parse to ascending unique 1-based pages', () => {
  assert.deepEqual(parseRanges('1-3, 7', 10), [1, 2, 3, 7]);
  assert.deepEqual(parseRanges('7,1-2,2', 10), [1, 2, 7]);
  assert.deepEqual(parseRanges('8-', 10), [8, 9, 10]);
  assert.deepEqual(parseRanges('  ', 10), []);
  assert.deepEqual(parseRanges('2–4', 10), [2, 3, 4], 'an en dash is accepted');
});

test('invalid page ranges are rejected, not repaired', () => {
  for (const bad of ['0', '3-1', '11', 'a', '1-x', '1,,x', '-3']) assert.equal(parseRanges(bad, 10), null, bad);
});

test('page lists format as compact ranges and round-trip', () => {
  assert.equal(formatRanges([1, 2, 3, 5, 7, 8]), '1-3, 5, 7-8');
  assert.equal(formatRanges([]), '');
  const pages = [2, 3, 4, 9];
  assert.deepEqual(parseRanges(formatRanges(pages), 20), pages);
});

test('plural forms', () => {
  assert.equal(plural(1, 'page'), '1 page');
  assert.equal(plural(2, 'page'), '2 pages');
  assert.equal(plural(1, 'match', 'matches'), '1 match');
  assert.equal(plural(1500, 'word'), '1,500 words');
});
