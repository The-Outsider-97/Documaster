// Normalised page geometry. A rectangle is [left, top, right, bottom] in 0..1 of the *displayed* page (origin top-left,
// after /Rotate). The server converts to PDF user space; the client never needs to know the page's rotation.

export type Rect = [number, number, number, number];
export interface Pt { x: number; y: number }

export const clamp01 = (v: number): number => Math.min(1, Math.max(0, v));

export function rectFromPoints(a: Pt, b: Pt): Rect {
  return [clamp01(Math.min(a.x, b.x)), clamp01(Math.min(a.y, b.y)), clamp01(Math.max(a.x, b.x)), clamp01(Math.max(a.y, b.y))];
}

export function isTiny(r: Rect, minW = 0.004, minH = 0.004): boolean {
  return r[2] - r[0] < minW || r[3] - r[1] < minH;
}

/** Pointer position inside a box of the given size, as a normalised point. */
export function toNormalised(clientX: number, clientY: number, box: { left: number; top: number; width: number; height: number }): Pt {
  return { x: clamp01((clientX - box.left) / box.width), y: clamp01((clientY - box.top) / box.height) };
}

export function rectToCss(r: Rect): { left: string; top: string; width: string; height: string } {
  return { left: `${r[0] * 100}%`, top: `${r[1] * 100}%`, width: `${(r[2] - r[0]) * 100}%`, height: `${(r[3] - r[1]) * 100}%` };
}

export function contains(r: Rect, p: Pt): boolean {
  return p.x >= r[0] && p.x <= r[2] && p.y >= r[1] && p.y <= r[3];
}

/** Simplify a freehand stroke (Ramer-Douglas-Peucker) so an ink annotation is not thousands of points. */
export function simplify(points: Pt[], epsilon = 0.0015): Pt[] {
  if (points.length < 3) return points.slice();
  const first = points[0];
  const last = points[points.length - 1];
  let worst = 0;
  let at = 0;
  for (let i = 1; i < points.length - 1; i++) {
    const d = distanceToSegment(points[i], first, last);
    if (d > worst) { worst = d; at = i; }
  }
  if (worst <= epsilon) return [first, last];
  return [...simplify(points.slice(0, at + 1), epsilon).slice(0, -1), ...simplify(points.slice(at), epsilon)];
}

function distanceToSegment(p: Pt, a: Pt, b: Pt): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len2 = dx * dx + dy * dy;
  if (len2 === 0) return Math.hypot(p.x - a.x, p.y - a.y);
  const t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2));
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}
