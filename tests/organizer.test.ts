import test from 'node:test';
import assert from 'node:assert/strict';
import { Organizer, type PageItem } from '../src/client/organizer.js';
import { formatRanges, parseRanges } from '../src/client/format.js';

function make(counts: number[]): Organizer {
  const o = new Organizer();
  const items: Omit<PageItem, 'uid'>[] = [];
  counts.forEach((n, d) => { for (let p = 0; p < n; p++) items.push({ docId: `D${d}`, page: p, rotation: 0, included: true, label: `d${d}p${p}`, docName: `d${d}` }); });
  o.load(items);
  return o;
}
const order = (o: Organizer): string => o.items.map((i) => i.label).join(',');
const sel = (o: Organizer, ...pos: number[]): void => { o.selectNone(); for (const p of pos) o.toggleSelect(o.items[p - 1]!.uid); };

test('load keeps document order and the plan matches', () => {
  const o = make([2, 3]);
  assert.equal(order(o), 'd0p0,d0p1,d1p0,d1p1,d1p2');
  const ids = ['D0', 'D1'];
  assert.deepEqual(o.toPlan(ids)[2], { doc: 1, page: 0, rotation: 0, included: true });
  assert.ok(o.isUntouched([{ id: 'D0', pages: 2 }, { id: 'D1', pages: 3 }]));
  assert.deepEqual(o.toPlan(['D1', 'D0'])[2], { doc: 0, page: 0, rotation: 0, included: true }, 'the plan follows the document order sent');
});

test('moveUp and moveDown move selected blocks and stop at the ends', () => {
  const o = make([5]);
  sel(o, 2, 3);
  assert.ok(o.moveUp());
  assert.equal(order(o), 'd0p1,d0p2,d0p0,d0p3,d0p4');
  assert.equal(o.moveUp(), false, 'a block already at the top does not move');
  assert.equal(order(o), 'd0p1,d0p2,d0p0,d0p3,d0p4');
  const p = make([3]);
  sel(p, 1);
  assert.equal(p.moveUp(), false, 'first page cannot move up');
  sel(p, 3);
  assert.equal(p.moveDown(), false, 'last page cannot move down');
  sel(p, 1, 3);
  assert.ok(p.moveDown());
  assert.equal(order(p), 'd0p1,d0p0,d0p2');
});

test('moving a block up past a non-selected page keeps the block together', () => {
  const o = make([6]);
  sel(o, 3, 4, 6);
  o.moveUp();
  assert.equal(order(o), 'd0p0,d0p2,d0p3,d0p1,d0p5,d0p4');
  o.moveDown();
  assert.equal(order(o), 'd0p0,d0p1,d0p2,d0p3,d0p4,d0p5');
});

test('undo and redo restore order and selection', () => {
  const o = make([4]);
  sel(o, 4);
  o.moveUp(); o.moveUp();
  assert.equal(order(o), 'd0p0,d0p3,d0p1,d0p2');
  assert.ok(o.undo());
  assert.equal(order(o), 'd0p0,d0p1,d0p3,d0p2');
  assert.ok(o.undo());
  assert.equal(order(o), 'd0p0,d0p1,d0p2,d0p3');
  assert.equal(o.undo(), false);
  assert.ok(o.redo());
  assert.equal(order(o), 'd0p0,d0p1,d0p3,d0p2');
  o.rotateSelected(90);
  assert.equal(o.canRedo, false, 'a new change clears redo');
});

test('toggleIncluded excludes then includes, and excluded pages stay in the plan flagged', () => {
  const o = make([3]);
  sel(o, 2);
  o.toggleIncluded();
  assert.equal(o.includedCount(), 2);
  assert.equal(o.toPlan(['D0'])[1]!.included, false);
  sel(o, 1, 2);
  o.toggleIncluded();      // mixed -> all included
  assert.equal(o.includedCount(), 3);
  o.toggleIncluded();      // all included -> excluded
  assert.equal(o.includedCount(), 1);
});

test('duplicate puts a copy after each selected page and selects the copies', () => {
  const o = make([3]);
  sel(o, 1, 3);
  o.duplicateSelected();
  assert.equal(order(o), 'd0p0,d0p0,d0p1,d0p2,d0p2');
  assert.equal(o.selected.size, 2);
  assert.equal(new Set(o.items.map((i) => i.uid)).size, 5, 'uids stay unique');
  assert.ok(!o.isUntouched([{ id: 'D0', pages: 3 }]));
});

test('rotation accumulates modulo 360 and can be dropped from the plan', () => {
  const o = make([2]);
  sel(o, 1);
  o.rotateSelected(90); o.rotateSelected(270); o.rotateSelected(-90);
  assert.equal(o.items[0]!.rotation, 270);
  assert.equal(o.toPlan(['D0'])[0]!.rotation, 270);
  assert.equal(o.toPlan(['D0'], { rotation: false })[0]!.rotation, 0);
  sel(o, 2);
  o.rotateSelected(90, (i) => i.page !== 1);
  assert.equal(o.items[1]!.rotation, 0, 'the filter keeps unsuitable pages unrotated');
});

test('dropBefore moves a selection to a position and to the end', () => {
  const o = make([5]);
  sel(o, 4, 5);
  o.dropBefore(o.items[0]!.uid);
  assert.equal(order(o), 'd0p3,d0p4,d0p0,d0p1,d0p2');
  sel(o, 1);
  o.dropBefore(null);
  assert.equal(order(o), 'd0p4,d0p0,d0p1,d0p2,d0p3');
  sel(o, 2);
  assert.equal(o.dropBefore(o.items[1]!.uid), false, 'dropping a page on itself changes nothing');
});

test('moveToStart and moveToEnd', () => {
  const o = make([4]);
  sel(o, 3);
  o.moveToStart();
  assert.equal(order(o), 'd0p2,d0p0,d0p1,d0p3');
  sel(o, 1);
  o.moveToEnd();
  assert.equal(order(o), 'd0p0,d0p1,d0p3,d0p2');
});

test('range selection by position, and shift-click', () => {
  const o = make([3, 4]);
  assert.ok(o.selectRanges('1-2, 6'));
  assert.deepEqual(o.selectedItems().map((i) => i.label), ['d0p0', 'd0p1', 'd1p2']);
  assert.equal(o.selectRanges('9'), false);
  assert.equal(o.selectRanges('3-1'), false);
  o.selectOnly(o.items[1]!.uid);
  o.selectThrough(o.items[4]!.uid);
  assert.equal(o.selected.size, 4);
  o.invertSelection();
  assert.deepEqual(o.selectedItems().map((i) => i.label), ['d0p0', 'd1p2', 'd1p3']);
  o.selectDoc('D1');
  assert.equal(o.selected.size, 4);
});

test('blank pages and removal', () => {
  const o = make([2]);
  o.insertBlankAfter(o.items[0]!.uid, { width: 600, height: 800 });
  assert.equal(o.items.length, 3);
  assert.deepEqual(o.toPlan(['D0'])[1], { blank: true, included: true, width: 600, height: 800 });
  o.removeSelected();
  assert.equal(o.items.length, 2);
  o.undo();
  assert.equal(o.items.length, 3);
});

test('reverse', () => {
  const o = make([3]);
  o.reverse();
  assert.equal(order(o), 'd0p2,d0p1,d0p0');
});

test('range parsing and formatting', () => {
  assert.deepEqual(parseRanges('1-3, 5', 10), [1, 2, 3, 5]);
  assert.deepEqual(parseRanges('8-', 10), [8, 9, 10]);
  assert.equal(parseRanges('0', 10), null);
  assert.equal(parseRanges('a', 10), null);
  assert.deepEqual(parseRanges('', 10), []);
  assert.equal(formatRanges([1, 2, 3, 5, 7, 8]), '1-3, 5, 7-8');
});
