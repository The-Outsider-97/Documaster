// The documents a workspace is working on: uploaded there, or picked from those already open. Removing a document anywhere removes it here.
import { select, h } from './dom.js';
import { Emitter, type AppContext } from './store.js';
import type { DocInfo } from './types.js';

export class WorkingSet {
  private ids: string[] = [];
  readonly changed = new Emitter<DocInfo[]>();

  constructor(private readonly ctx: AppContext, private readonly accept: (d: DocInfo) => boolean = () => true) {
    ctx.docs.changed.on((all) => {
      const alive = new Set(all.map((d) => d.id));
      const before = this.ids.length;
      this.ids = this.ids.filter((i) => alive.has(i));
      if (this.ids.length !== before || this.ids.length) this.changed.emit(this.docs());
    });
  }

  docs(): DocInfo[] {
    return this.ids.map((i) => this.ctx.docs.get(i)).filter((d): d is DocInfo => !!d);
  }

  has(id: string): boolean { return this.ids.includes(id); }

  add(...docs: DocInfo[]): void {
    let changed = false;
    for (const d of docs) if (!this.ids.includes(d.id) && this.accept(d)) { this.ids.push(d.id); changed = true; }
    if (changed) this.changed.emit(this.docs());
  }

  remove(id: string): void {
    this.ids = this.ids.filter((i) => i !== id);
    this.changed.emit(this.docs());
  }

  clear(): void { this.ids = []; this.changed.emit([]); }

  move(id: string, delta: number): void {
    const i = this.ids.indexOf(id);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= this.ids.length) return;
    [this.ids[i], this.ids[j]] = [this.ids[j]!, this.ids[i]!];
    this.changed.emit(this.docs());
  }

  /** A "use an open document" picker listing documents that are open elsewhere and not yet here. */
  picker(label = 'Add an open document…'): HTMLElement {
    const sel = select([], '');
    const refresh = (): void => {
      while (sel.firstChild) sel.removeChild(sel.firstChild);
      const others = this.ctx.docs.list().filter((d) => !this.has(d.id) && this.accept(d) && !d.locked);
      sel.appendChild(h('option', { text: others.length ? label : 'No other documents are open', attrs: { value: '' } }));
      for (const d of others) sel.appendChild(h('option', { text: d.name, attrs: { value: d.id } }));
      sel.disabled = !others.length;
    };
    sel.addEventListener('change', () => {
      const d = this.ctx.docs.get(sel.value);
      if (d) this.add(d);
      refresh();
    });
    this.ctx.docs.changed.on(refresh);
    this.changed.on(refresh);
    refresh();
    return sel;
  }
}
