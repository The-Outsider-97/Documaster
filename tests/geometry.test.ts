import test from 'node:test';
import assert from 'node:assert/strict';
import { clamp01, contains, isTiny, rectFromPoints, rectToCss, simplify, toNormalised, type Pt } from '../src/client/geometry.js';

test('a rectangle from two points is ordered and stays on the page', () => {
  assert.deepEqual(rectFromPoints({ x: 0.6, y: 0.7 }, { x: 0.2, y: 0.1 }), [0.2, 0.1, 0.6, 0.7]);
  assert.deepEqual(rectFromPoints({ x: -1, y: 0.5 }, { x: 2, y: 3 }), [0, 0.5, 1, 1]);
});

test('pointer positions are normalised to the box and clamped', () => {
  const box = { left: 100, top: 50, width: 200, height: 400 };
  assert.deepEqual(toNormalised(200, 250, box), { x: 0.5, y: 0.5 });
  assert.deepEqual(toNormalised(0, 9999, box), { x: 0, y: 1 });
});

test('tiny drags are not turned into marks', () => {
  assert.ok(isTiny([0.1, 0.1, 0.1005, 0.3]));
  assert.ok(isTiny([0.1, 0.1, 0.3, 0.1]));
  assert.ok(!isTiny([0.1, 0.1, 0.2, 0.2]));
});

test('css placement uses percentages of the page', () => {
  assert.deepEqual(rectToCss([0.1, 0.2, 0.6, 0.5]), { left: '10%', top: '20%', width: '50%', height: '30%' });
});

test('hit testing includes the edges', () => {
  assert.ok(contains([0.1, 0.1, 0.5, 0.5], { x: 0.5, y: 0.1 }));
  assert.ok(!contains([0.1, 0.1, 0.5, 0.5], { x: 0.51, y: 0.3 }));
  assert.equal(clamp01(1.2), 1);
});

test('freehand strokes are simplified but keep their shape', () => {
  const line: Pt[] = Array.from({ length: 200 }, (_, i) => ({ x: i / 199, y: 0.5 + (i % 2 ? 0.0001 : 0) }));
  assert.deepEqual(simplify(line).length, 2, 'a straight line needs two points');
  const corner: Pt[] = [...Array.from({ length: 50 }, (_, i) => ({ x: i / 49, y: 0 })), ...Array.from({ length: 50 }, (_, i) => ({ x: 1, y: (i + 1) / 50 }))];
  const s = simplify(corner);
  assert.ok(s.length >= 3 && s.length <= 5, `corner kept (${s.length} points)`);
  assert.deepEqual(s[0], corner[0]);
  assert.deepEqual(s[s.length - 1], corner[corner.length - 1]);
  assert.deepEqual(simplify([{ x: 0, y: 0 }, { x: 1, y: 1 }]), [{ x: 0, y: 0 }, { x: 1, y: 1 }]);
});
