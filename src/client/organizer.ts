// The page organiser: an ordered list of page instances with selection, include/exclude, rotation, duplication and undo/redo.
// Pure state (no DOM) so it can be tested; the Merger and the PDF editor's page panel both render from it.
import { parseRanges } from './format.js';
import type { PlanEntry } from './types.js';

export interface PageItem {
  uid: string;
  /** The source document's id ('' for a blank page). */
  docId: string;
  /** 0-based page in that document. */
  page: number;
  blank?: boolean;
  /** Extra clockwise rotation (0, 90, 180, 270) applied on top of the page's own rotation. */
  rotation: number;
  included: boolean;
  label: string;
  docName: string;
  /** The page's own rotation (preview only). */
  baseRotation?: number;
  width?: number;
  height?: number;
}

interface Snapshot { items: PageItem[]; selected: string[] }

export class Organizer {
  items: PageItem[] = [];
  selected = new Set<string>();
  private past: Snapshot[] = [];
  private future: Snapshot[] = [];
  private seq = 0;
  private listeners = new Set<() => void>();
  readonly limit: number;

  constructor(limit = 100) { this.limit = limit; }

  onChange(fn: () => void): () => void { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  private emit(): void { for (const fn of [...this.listeners]) fn(); }

  newUid(): string { return `p${++this.seq}`; }

  // ---- history -----------------------------------------------------------------------------------------------------
  private snapshot(): Snapshot { return { items: this.items.map((i) => ({ ...i })), selected: [...this.selected] }; }
  private restore(s: Snapshot): void { this.items = s.items.map((i) => ({ ...i })); this.selected = new Set(s.selected); }
  private commit(change: () => boolean | void): boolean {
    const before = this.snapshot();
    if (change() === false) return false;
    this.past.push(before);
    if (this.past.length > this.limit) this.past.shift();
    this.future = [];
    this.emit();
    return true;
  }
  get canUndo(): boolean { return this.past.length > 0; }
  get canRedo(): boolean { return this.future.length > 0; }
  undo(): boolean {
    const s = this.past.pop();
    if (!s) return false;
    this.future.push(this.snapshot());
    this.restore(s);
    this.emit();
    return true;
  }
  redo(): boolean {
    const s = this.future.pop();
    if (!s) return false;
    this.past.push(this.snapshot());
    this.restore(s);
    this.emit();
    return true;
  }

  /** Replace the whole list (fresh load); history is cleared. */
  load(items: Omit<PageItem, 'uid'>[]): void {
    this.items = items.map((i) => ({ ...i, uid: this.newUid() }));
    this.selected.clear();
    this.past = [];
    this.future = [];
    this.emit();
  }

  // ---- selection (not part of history) -----------------------------------------------------------------------------
  private indexOf(uid: string): number { return this.items.findIndex((i) => i.uid === uid); }
  private anchor: string | null = null;

  selectOnly(uid: string): void { this.selected = new Set([uid]); this.anchor = uid; this.emit(); }
  toggleSelect(uid: string): void {
    if (this.selected.has(uid)) this.selected.delete(uid); else this.selected.add(uid);
    this.anchor = uid;
    this.emit();
  }
  /** Shift-click: select everything between the anchor and ``uid``. */
  selectThrough(uid: string): void {
    const a = this.anchor ? this.indexOf(this.anchor) : -1;
    const b = this.indexOf(uid);
    if (a < 0 || b < 0) { this.selectOnly(uid); return; }
    const [lo, hi] = a < b ? [a, b] : [b, a];
    this.selected = new Set(this.items.slice(lo, hi + 1).map((i) => i.uid));
    this.emit();
  }
  selectAll(): void { this.selected = new Set(this.items.map((i) => i.uid)); this.emit(); }
  selectNone(): void { this.selected.clear(); this.emit(); }
  invertSelection(): void { this.selected = new Set(this.items.filter((i) => !this.selected.has(i.uid)).map((i) => i.uid)); this.emit(); }
  selectDoc(docId: string): void { this.selected = new Set(this.items.filter((i) => i.docId === docId).map((i) => i.uid)); this.emit(); }
  selectExcluded(): void { this.selected = new Set(this.items.filter((i) => !i.included).map((i) => i.uid)); this.emit(); }
  /** Select by position in the current order: "1-3, 7". Returns false if the text is not a valid range list. */
  selectRanges(text: string): boolean {
    const pos = parseRanges(text, this.items.length);
    if (pos === null) return false;
    this.selected = new Set(pos.map((p) => this.items[p - 1]!.uid));
    this.emit();
    return true;
  }
  selectedItems(): PageItem[] { return this.items.filter((i) => this.selected.has(i.uid)); }
  position(uid: string): number { return this.indexOf(uid) + 1; }

  // ---- changes ---------------------------------------------------------------------------------------------------------
  /** Move the selection one place towards the start (selected blocks move together; the top of the list stops them). */
  moveUp(): boolean {
    return this.commit(() => {
      let moved = false;
      for (let i = 1; i < this.items.length; i++) {
        if (this.selected.has(this.items[i]!.uid) && !this.selected.has(this.items[i - 1]!.uid)) {
          [this.items[i - 1], this.items[i]] = [this.items[i]!, this.items[i - 1]!];
          moved = true;
        }
      }
      return moved;
    });
  }

  moveDown(): boolean {
    return this.commit(() => {
      let moved = false;
      for (let i = this.items.length - 2; i >= 0; i--) {
        if (this.selected.has(this.items[i]!.uid) && !this.selected.has(this.items[i + 1]!.uid)) {
          [this.items[i + 1], this.items[i]] = [this.items[i]!, this.items[i + 1]!];
          moved = true;
        }
      }
      return moved;
    });
  }

  moveToStart(): boolean { return this.moveBlock(0); }
  moveToEnd(): boolean { return this.moveBlock(this.items.length); }

  /** Put the selected items (in their current order) before the item ``beforeUid``; ``null`` puts them at the end. Used by drag and drop. */
  dropBefore(beforeUid: string | null, uids: string[] = [...this.selected]): boolean {
    const moving = new Set(uids);
    if (!moving.size || (beforeUid !== null && moving.has(beforeUid))) return false;
    return this.commit(() => {
      const block = this.items.filter((i) => moving.has(i.uid));
      const rest = this.items.filter((i) => !moving.has(i.uid));
      let at = beforeUid === null ? rest.length : rest.findIndex((i) => i.uid === beforeUid);
      if (at < 0) at = rest.length;
      const next = [...rest.slice(0, at), ...block, ...rest.slice(at)];
      const same = next.every((i, k) => i.uid === this.items[k]!.uid);
      if (same) return false;
      this.items = next;
      return true;
    });
  }

  private moveBlock(index: number): boolean {
    const block = this.selectedItems();
    if (!block.length) return false;
    const rest = this.items.filter((i) => !this.selected.has(i.uid));
    const before = index === 0 ? rest[0]?.uid ?? null : null;
    return index === 0 ? (before === null ? false : this.dropBefore(before)) : this.dropBefore(null);
  }

  /** Include or exclude the selected pages. If every selected page is included they are excluded; otherwise all become included. */
  toggleIncluded(): boolean {
    const sel = this.selectedItems();
    if (!sel.length) return false;
    const to = !sel.every((i) => i.included);
    return this.setIncluded(sel.map((i) => i.uid), to);
  }

  setIncluded(uids: string[], included: boolean): boolean {
    const set = new Set(uids);
    return this.commit(() => {
      let changed = false;
      for (const i of this.items) if (set.has(i.uid) && i.included !== included) { i.included = included; changed = true; }
      return changed;
    });
  }

  /** Rotate the selected pages; ``only`` limits it to pages that can be rotated. */
  rotateSelected(delta: number, only: (i: PageItem) => boolean = () => true): boolean {
    return this.commit(() => {
      const sel = this.selectedItems().filter(only);
      if (!sel.length) return false;
      for (const i of sel) i.rotation = (((i.rotation + delta) % 360) + 360) % 360;
      return true;
    });
  }

  /** Duplicate each selected page right after itself; the copies become the selection. */
  duplicateSelected(): boolean {
    return this.commit(() => {
      if (!this.selected.size) return false;
      const out: PageItem[] = [];
      const copies = new Set<string>();
      for (const i of this.items) {
        out.push(i);
        if (this.selected.has(i.uid)) {
          const c = { ...i, uid: this.newUid() };
          copies.add(c.uid);
          out.push(c);
        }
      }
      this.items = out;
      this.selected = copies;
      return true;
    });
  }

  removeSelected(): boolean {
    return this.commit(() => {
      if (!this.selected.size) return false;
      this.items = this.items.filter((i) => !this.selected.has(i.uid));
      this.selected.clear();
      return true;
    });
  }

  insertBlankAfter(uid: string | null, size?: { width: number; height: number }): boolean {
    return this.commit(() => {
      const idx = uid === null ? this.items.length - 1 : this.indexOf(uid);
      const ref = this.items[idx];
      const blank: PageItem = { uid: this.newUid(), docId: '', page: -1, blank: true, rotation: 0, included: true, label: 'Blank page', docName: '',
        width: size?.width ?? ref?.width, height: size?.height ?? ref?.height };
      this.items.splice(idx + 1, 0, blank);
      this.selected = new Set([blank.uid]);
      return true;
    });
  }

  reverse(): boolean {
    return this.commit(() => {
      if (this.items.length < 2) return false;
      this.items.reverse();
      return true;
    });
  }

  // ---- output ----------------------------------------------------------------------------------------------------------
  includedCount(): number { return this.items.filter((i) => i.included).length; }

  /** The plan sent to the server; ``docIds`` is the document list sent with it (the plan refers to documents by position in it).
   *  Rotation is dropped when ``rotation`` is false (output formats that cannot rotate pages). */
  toPlan(docIds: string[], opts: { rotation?: boolean } = {}): PlanEntry[] {
    const rot = opts.rotation !== false;
    return this.items.map((i) => {
      if (i.blank) {
        const e: PlanEntry = { blank: true, included: i.included };
        if (i.width && i.height) { e.width = i.width; e.height = i.height; }
        return e;
      }
      return { doc: docIds.indexOf(i.docId), page: i.page, rotation: rot ? i.rotation : 0, included: i.included };
    });
  }

  /** Is the list exactly the documents in order, every page once, nothing rotated or excluded? */
  isUntouched(docs: { id: string; pages: number }[]): boolean {
    let k = 0;
    for (const d of docs) {
      for (let p = 0; p < d.pages; p++) {
        const i = this.items[k++];
        if (!i || i.blank || i.docId !== d.id || i.page !== p || i.rotation !== 0 || !i.included) return false;
      }
    }
    return k === this.items.length;
  }
}

/** Remove every page of the documents that are no longer open (history is kept; this is not an undoable user action). */
export function dropMissing(o: Organizer, alive: Set<string>): void {
  const keep = o.items.filter((i) => i.blank || alive.has(i.docId));
  if (keep.length !== o.items.length) {
    o.items = keep;
    o.selected = new Set([...o.selected].filter((u) => keep.some((i) => i.uid === u)));
  }
}
