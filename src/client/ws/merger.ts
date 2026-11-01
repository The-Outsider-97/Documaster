// Merger & Page Organizer: combine documents of any supported type, reorder / exclude / duplicate / rotate pages first, and merge.
// The handler names below are kept from the earlier Documaster so existing muscle memory (and any external scripts) still apply.
import { dropzone, docRow, DOCUMENT_ACCEPT, downloadLink, emptyState, IMAGE_ACCEPT, ingest, listBlock, progress, sectionTitle, verificationView } from '../components.js';
import { badge, button, checkbox, confirmDialog, errorMessage, field, h, replace, select, toast } from '../dom.js';
import { formatDuration, plural } from '../format.js';
import { dropMissing, Organizer, type PageItem } from '../organizer.js';
import type { AppContext } from '../store.js';
import type { DocInfo, FormatsInfo, MergeResult, OutlineDoc } from '../types.js';
import { WorkingSet } from '../workingset.js';
import type { Workspace } from './types.js';

const docColour = (index: number): string => `hsl(${(index * 67 + 30) % 360} 60% 52%)`;

export interface MergeOptions {
  target: string; title: string; stem: string; password: string; splitHeadings: number;
  sourceBookmarks: boolean; keepBookmarks: boolean; pageNumbers: boolean; pageSize: string; fontSizePt: number;
}

/** State and actions of the merger. The UI binds buttons to these methods. */
export class MergeController {
  readonly set: WorkingSet;
  readonly organizer = new Organizer();
  readonly outlines = new Map<string, OutlineDoc>();
  options: MergeOptions = { target: 'pdf', title: '', stem: '', password: '', splitHeadings: 0, sourceBookmarks: true, keepBookmarks: true, pageNumbers: true, pageSize: '', fontSizePt: 11 };
  busy = false;
  lastResult: MergeResult | null = null;
  private modeKey = '';
  readonly notices: string[] = [];

  constructor(private readonly ctx: AppContext, private readonly ui: { progress: ReturnType<typeof progress>; changed: () => void; result: (r: MergeResult | null) => void; tray: () => HTMLElement }) {
    this.set = new WorkingSet(ctx, (d) => !d.locked);
    ctx.docs.changed.on((all) => {
      dropMissing(this.organizer, new Set(all.map((d) => d.id)));
      this.ui.changed();
    });
  }

  get rotationAllowed(): boolean { return this.options.target === 'pdf'; }
  docs(): DocInfo[] { return this.set.docs(); }
  docIndex(id: string): number { return this.docs().findIndex((d) => d.id === id); }
  kindOf(id: string): OutlineDoc['kind'] | undefined { return this.outlines.get(id)?.kind; }
  canRotate = (i: PageItem): boolean => this.rotationAllowed && !i.blank && this.kindOf(i.docId) !== 'model';

  // ---- add files / build plan --------------------------------------------------------------------------------------------
  async _add_merge_files(files: File[]): Promise<void> {
    const docs = await ingest(this.ctx, files, { images: true, tray: this.ui.tray() });
    const usable = docs.filter((d) => !d.locked);
    if (docs.length !== usable.length) toast('Protected documents that were not unlocked were left out', 'info');
    this.set.add(...usable);
    await this._build_merge_plan();
  }

  /** Read the structure of every document and (re)build the page list. Existing edits are kept unless the output mode changed. */
  async _build_merge_plan(): Promise<void> {
    const docs = this.docs();
    if (!docs.length) { this.organizer.load([]); this.outlines.clear(); this.modeKey = ''; this.ui.changed(); return; }
    const mode = this.options.target === 'pdf' ? 'pdf' : 'model';
    const key = `${mode}:${mode === 'model' ? this.options.splitHeadings : 0}`;
    let outline: OutlineDoc[];
    try {
      outline = await this.ctx.api.post<OutlineDoc[]>('/api/merge/outline', { docs: docs.map((d) => d.id), target: this.options.target, splitHeadings: this.options.splitHeadings });
    } catch (e) { toast(errorMessage(e), 'error'); return; }
    this.outlines.clear();
    for (const o of outline) this.outlines.set(o.id, o);
    const itemsFor = (o: OutlineDoc): Omit<PageItem, 'uid'>[] => o.pages.map((p) => ({
      docId: o.id, page: p.index, rotation: 0, included: true, docName: o.name, width: p.width, height: p.height, baseRotation: p.rotation,
      label: p.label || `Page ${p.index + 1}`,
    }));
    this.notices.length = 0;
    for (const o of outline) for (const w of o.warnings) this.notices.push(`${o.name}: ${w}`);
    if (key !== this.modeKey || !this.organizer.items.length) {
      this.organizer.load(outline.flatMap(itemsFor));
      this.modeKey = key;
    } else {
      const have = new Set(this.organizer.items.filter((i) => !i.blank).map((i) => i.docId));
      const fresh = outline.filter((o) => !have.has(o.id));
      if (fresh.length) {
        const added = fresh.flatMap(itemsFor).map((i) => ({ ...i, uid: this.organizer.newUid() }));
        this.organizer.items = [...this.organizer.items, ...added];
        this.organizer.selected.clear();
        this.ui.changed();
        return;
      }
    }
    this.ui.changed();
  }

  // ---- page actions (names kept) ------------------------------------------------------------------------------------------
  _move_merge_page_up(): void { this.organizer.moveUp(); }
  _move_merge_page_down(): void { this.organizer.moveDown(); }
  _toggle_selected_merge_pages(): void { this.organizer.toggleIncluded(); }

  // ---- merge ------------------------------------------------------------------------------------------------------------------
  async _merge_selected_pdfs(): Promise<MergeResult | null> {
    const docs = this.docs();
    if (!docs.length) { toast('Add at least one document first'); return null; }
    if (this.organizer.includedCount() === 0) { toast('Every page is excluded. Include at least one page.', 'error'); return null; }
    if (this.busy) return null;
    const o = this.options;
    const ids = docs.map((d) => d.id);
    const body: Record<string, unknown> = {
      docs: ids, target: o.target, plan: this.organizer.toPlan(ids, { rotation: this.rotationAllowed }), title: o.title || undefined, stem: o.stem || undefined,
      password: o.password || undefined, splitHeadings: o.target === 'pdf' ? 0 : o.splitHeadings, sourceBookmarks: o.sourceBookmarks, keepBookmarks: o.keepBookmarks,
      pageNumbers: o.pageNumbers, pageSize: o.pageSize || undefined, fontSizePt: o.fontSizePt,
    };
    this.busy = true;
    this.ui.changed();
    try {
      const r = await this.ui.progress.run<MergeResult>(`Merging ${plural(this.organizer.includedCount(), 'page')}`, () => this.ctx.api.startJob('/api/merge', body));
      this.lastResult = r;
      this.ui.result(r);
      return r;
    } catch (e) {
      if ((e as { cancelled?: boolean }).cancelled) toast('Merge cancelled'); else toast(errorMessage(e), 'error');
      return null;
    } finally { this.busy = false; this.ui.changed(); }
  }

  async _clear_merge_files(): Promise<void> {
    if (this.docs().length > 1 && !(await confirmDialog('Clear the file list?', 'The files stay open in other workspaces. Your page order, exclusions and rotations here are discarded.', 'Clear'))) return;
    this.set.clear();
    this.organizer.load([]);
    this.outlines.clear();
    this.modeKey = '';
    this.lastResult = null;
    this.ui.result(null);
    this.ui.changed();
  }
}

export function createMerger(ctx: AppContext): Workspace {
  const prog = progress();
  const resultBox = h('div', { class: 'results' });
  const grid = h('div', { class: 'page-grid', attrs: { role: 'listbox', 'aria-multiselectable': 'true', 'aria-label': 'Pages to merge' } });
  const toolbar = h('div', { class: 'toolbar', attrs: { role: 'toolbar', 'aria-label': 'Page actions' } });
  const fileList = h('div', { class: 'doc-list' });
  const status = h('div', { class: 'muted small' });
  const dragStatus = h('div', { class: 'visually-hidden', attrs: { 'aria-live': 'polite', 'aria-atomic': 'true' } });
  const notices = h('div', { class: 'notices' });
  const optionsBox = h('div', { class: 'card options' });
  const mergeBtn = button('Merge Files', () => void c._merge_selected_pdfs(), 'btn-primary');
  const editBtn = button('Edit Files', () => editSelected(), '', { title: 'Open the first selected PDF (or the first PDF) in the PDF Editor' });
  let formats: FormatsInfo | null = null;
  const cards = new Map<string, HTMLElement>();

  const c: MergeController = new MergeController(ctx, {
    progress: prog,
    changed: () => render(),
    result: (r) => renderResult(r),
    tray: () => dz.tray,
  });
  const org = c.organizer;
  org.onChange(() => render());

  // Pointer dragging works with a mouse, pen and touch. The browser's native HTML drag
  // events are deliberately not mixed with this path (touch support is inconsistent).
  type DropTarget = { uid: string; before: boolean; beforeUid: string | null };
  type PageDrag = {
    uid: string; pointerId: number; startX: number; startY: number; x: number; y: number;
    active: boolean; target: DropTarget | null; frame: number | null;
  };
  let drag: PageDrag | null = null;
  let suppressClickUid: string | null = null;

  function clearDropMarkers(): void {
    for (const card of cards.values()) card.classList.remove('dragging', 'drop-before', 'drop-after');
  }

  function dropTargetAt(x: number, y: number): DropTarget | null {
    const hit = document.elementFromPoint(x, y)?.closest<HTMLElement>('.page-card');
    const card = hit && grid.contains(hit) ? hit : null;
    const targetUid = card?.dataset['uid'];
    if (!targetUid || org.selected.has(targetUid)) {
      // The empty space below the grid's final row is a valid drop-at-end target.
      const bounds = grid.getBoundingClientRect();
      const last = grid.lastElementChild as HTMLElement | null;
      if (!targetUid && last?.classList.contains('page-card') && x >= bounds.left && x <= bounds.right &&
          y >= last.getBoundingClientRect().bottom && y <= bounds.bottom) {
        return { uid: last.dataset['uid']!, before: false, beforeUid: null };
      }
      return null;
    }
    const bounds = card!.getBoundingClientRect();
    const before = x < bounds.left + bounds.width / 2;
    const index = org.items.findIndex((i) => i.uid === targetUid);
    // "After" must skip selected pages: dropBefore refuses a selected target.
    const next = org.items.slice(index + 1).find((i) => !org.selected.has(i.uid));
    return { uid: targetUid, before, beforeUid: before ? targetUid : next?.uid ?? null };
  }

  function updateDropTarget(): void {
    if (!drag?.active) return;
    drag.target = dropTargetAt(drag.x, drag.y);
    for (const card of cards.values()) card.classList.remove('drop-before', 'drop-after');
    if (drag.target) cards.get(drag.target.uid)?.classList.add(drag.target.before ? 'drop-before' : 'drop-after');
  }

  function edgeScroll(): void {
    if (!drag?.active) return;
    const edge = 65;
    const amount = drag.y < edge ? -Math.min(20, (edge - drag.y) / 3) :
      drag.y > window.innerHeight - edge ? Math.min(20, (drag.y - (window.innerHeight - edge)) / 3) : 0;
    if (amount) { window.scrollBy(0, amount); updateDropTarget(); }
    drag.frame = window.requestAnimationFrame(edgeScroll);
  }

  function finishDrag(commit: boolean): void {
    const previous = drag;
    if (!previous) return;
    drag = null;
    if (previous.frame !== null) window.cancelAnimationFrame(previous.frame);
    clearDropMarkers();
    if (!previous.active) return;
    suppressClickUid = previous.uid;
    window.setTimeout(() => { if (suppressClickUid === previous.uid) suppressClickUid = null; }, 0);
    const changed = commit && previous.target ? org.dropBefore(previous.target.beforeUid) : false;
    dragStatus.textContent = changed ? `Moved ${org.selected.size} page(s).` : 'Page move cancelled or order unchanged.';
  }

  function editSelected(): void {
    const sel = org.selectedItems().find((i) => !i.blank && c.kindOf(i.docId) === 'pdf');
    const doc = (sel ? ctx.docs.get(sel.docId) : undefined) ?? c.docs().find((d) => d.format === 'pdf');
    if (!doc) { toast('Add a PDF to edit it'); return; }
    ctx.open('editor', doc.id, sel ? sel.page + 1 : 1);
  }

  // ---- toolbar -----------------------------------------------------------------------------------------------------
  const rangeInput = h('input', { class: 'input range', attrs: { type: 'text', placeholder: 'Select pages: 1-3, 7', 'aria-label': 'Select pages by position', size: 16 },
    on: { keydown: (e) => { if (e.key === 'Enter') { if (!org.selectRanges((e.target as HTMLInputElement).value)) toast('Use page numbers like 1-3, 7', 'error'); } } } });
  const sel = select([{ value: '', label: 'Select…' }, { value: 'all', label: 'All pages' }, { value: 'none', label: 'None' }, { value: 'invert', label: 'Invert selection' }, { value: 'excluded', label: 'Excluded pages' }], '', (v) => {
    if (v === 'all') org.selectAll(); else if (v === 'none') org.selectNone(); else if (v === 'invert') org.invertSelection(); else if (v === 'excluded') org.selectExcluded();
    sel.value = '';
  });
  sel.setAttribute('aria-label', 'Selection');
  const tbtn = (label: string, title: string, fn: () => void, id: string): HTMLButtonElement => button(label, fn, 'btn-small', { title, attrs: { 'aria-label': title, 'data-act': id } });
  const buttons = {
    up: tbtn('↑ Earlier', 'Move the selected pages one place earlier (Alt+↑)', () => c._move_merge_page_up(), 'up'),
    down: tbtn('↓ Later', 'Move the selected pages one place later (Alt+↓)', () => c._move_merge_page_down(), 'down'),
    toggle: tbtn('Include / exclude', 'Include or exclude the selected pages (Delete)', () => c._toggle_selected_merge_pages(), 'toggle'),
    dup: tbtn('Duplicate', 'Duplicate the selected pages', () => org.duplicateSelected(), 'dup'),
    left: tbtn('⟲', 'Rotate the selected pages left', () => org.rotateSelected(-90, c.canRotate), 'rotl'),
    right: tbtn('⟳', 'Rotate the selected pages right', () => org.rotateSelected(90, c.canRotate), 'rotr'),
    blank: tbtn('+ Blank', 'Insert a blank page after the selection', () => { const last = org.selectedItems().pop(); org.insertBlankAfter(last?.uid ?? null); }, 'blank'),
    remove: tbtn('Remove', 'Remove the selected pages from the list', () => org.removeSelected(), 'remove'),
    reverse: tbtn('Reverse', 'Reverse the whole page order', () => org.reverse(), 'reverse'),
    undo: tbtn('↶ Undo', 'Undo (Ctrl+Z)', () => org.undo(), 'undo'),
    redo: tbtn('↷ Redo', 'Redo (Ctrl+Y)', () => org.redo(), 'redo'),
  };
  replace(toolbar, sel, rangeInput, h('span', { class: 'sep' }), buttons.up, buttons.down, buttons.toggle, buttons.dup, buttons.left, buttons.right, buttons.blank, buttons.remove, buttons.reverse, h('span', { class: 'sep' }), buttons.undo, buttons.redo);

  // ---- options -------------------------------------------------------------------------------------------------------------
  function renderOptions(): void {
    const o = c.options;
    const targets = (formats?.targets ?? [{ id: 'pdf', label: 'PDF', available: true, limits: [] }]).map((t) => ({ value: t.id, label: t.available ? t.label : `${t.label} (unavailable)`, disabled: !t.available }));
    const isPdf = o.target === 'pdf';
    replace(optionsBox,
      field('Merge into:', select(targets, o.target, (v) => { o.target = v; void c._build_merge_plan().then(renderOptions); renderOptions(); })),
      isPdf ? h('div', { class: 'note small muted', text: 'PDF pages are copied unchanged. Word, ODT, HTML, text and SLDC files are laid out as new pages; images get a page each.' })
        : h('div', { class: 'note small muted', text: 'The merge goes through the SLDC document model: styles, sections, notes, images and provenance are combined. PDFs are merged as text only.' }),
      h('div', { class: 'row' },
        field('Title (optional)', h('input', { class: 'input', value: o.title, attrs: { type: 'text', maxlength: 200 }, on: { input: (e) => { o.title = (e.target as HTMLInputElement).value; } } })),
        field('Output name (optional)', h('input', { class: 'input', value: o.stem, attrs: { type: 'text', maxlength: 100, placeholder: 'merged' }, on: { input: (e) => { o.stem = (e.target as HTMLInputElement).value; } } }))),
      isPdf ? h('div', {}, checkbox('Bookmark each file', o.sourceBookmarks, (v) => { o.sourceBookmarks = v; }), checkbox('Keep the original bookmarks', o.keepBookmarks, (v) => { o.keepBookmarks = v; }),
        field('Password for the merged PDF (optional)', h('input', { class: 'input', attrs: { type: 'password', autocomplete: 'new-password' }, on: { input: (e) => { o.password = (e.target as HTMLInputElement).value; } } }), 'AES-256. Not stored.'))
        : h('div', {},
          field('Start a new page at headings up to level', select([0, 1, 2, 3, 4].map((n) => ({ value: String(n), label: n === 0 ? 'Only where the document already breaks' : `Heading ${n}` })), String(o.splitHeadings), (v) => { o.splitHeadings = Number(v); void c._build_merge_plan(); })),
          ['pdf', 'docx', 'odt', 'rtf'].includes(o.target) ? checkbox('Page numbers', o.pageNumbers, (v) => { o.pageNumbers = v; }) : null,
          o.target === 'sldc' ? field('Encrypt the SLDC file with a password (optional)', h('input', { class: 'input', attrs: { type: 'password', autocomplete: 'new-password' }, on: { input: (e) => { o.password = (e.target as HTMLInputElement).value; } } })) : null),
      h('div', { class: 'row-actions' }, mergeBtn, editBtn));
  }
  void ctx.formats().then((f) => { formats = f; renderOptions(); }).catch(() => renderOptions());

  // ---- cards -----------------------------------------------------------------------------------------------------------------
  function makeCard(item: PageItem): HTMLElement {
    const thumb = h('div', { class: 'page-thumb' });
    const pos = h('span', { class: 'pos' });
    const src = h('span', { class: 'src' });
    const act = (label: string, title: string, fn: () => void): HTMLButtonElement => button(label, fn, 'btn-icon btn-mini', { title, attrs: { 'aria-label': title } });
    const card = h('div', { class: 'page-card', attrs: { draggable: false, tabindex: 0, role: 'option', 'data-uid': item.uid,
      'aria-description': 'Drag to reorder. Use Alt and arrow keys to move selected pages.' } }, thumb,
      h('div', { class: 'page-meta' }, pos, src),
      h('div', { class: 'page-actions' },
        act('👁', 'Include or exclude this page', () => { org.selectOnly(item.uid); org.toggleIncluded(); }),
        act('⟲', 'Rotate left', () => { org.selectOnly(item.uid); org.rotateSelected(-90, c.canRotate); }),
        act('⟳', 'Rotate right', () => { org.selectOnly(item.uid); org.rotateSelected(90, c.canRotate); }),
        act('⧉', 'Duplicate this page', () => { org.selectOnly(item.uid); org.duplicateSelected(); })));
    card.addEventListener('click', (e) => {
      if (suppressClickUid === item.uid) { e.preventDefault(); suppressClickUid = null; return; }
      if ((e.target as HTMLElement).closest('button')) return;
      if (e.shiftKey) org.selectThrough(item.uid); else if (e.ctrlKey || e.metaKey) org.toggleSelect(item.uid); else org.selectOnly(item.uid);
    });
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (e.ctrlKey || e.metaKey) org.toggleSelect(item.uid); else org.selectOnly(item.uid); }
      else if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowLeft')) { e.preventDefault(); if (!org.selected.has(item.uid)) org.selectOnly(item.uid); org.moveUp(); focusCard(item.uid); }
      else if (e.altKey && (e.key === 'ArrowDown' || e.key === 'ArrowRight')) { e.preventDefault(); if (!org.selected.has(item.uid)) org.selectOnly(item.uid); org.moveDown(); focusCard(item.uid); }
      else if (e.key === 'Delete') { e.preventDefault(); if (!org.selected.has(item.uid)) org.selectOnly(item.uid); org.toggleIncluded(); }
      else if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') { const n = e.key === 'ArrowRight' ? card.nextElementSibling : card.previousElementSibling; (n as HTMLElement | null)?.focus(); }
    });
    card.addEventListener('pointerdown', (e) => {
      if (drag || e.button !== 0 || !e.isPrimary || (e.target as HTMLElement).closest('button, input')) return;
      drag = { uid: item.uid, pointerId: e.pointerId, startX: e.clientX, startY: e.clientY,
        x: e.clientX, y: e.clientY, active: false, target: null, frame: null };
      card.setPointerCapture(e.pointerId);
    });
    card.addEventListener('pointermove', (e) => {
      if (!drag || drag.pointerId !== e.pointerId || drag.uid !== item.uid) return;
      drag.x = e.clientX; drag.y = e.clientY;
      if (!drag.active && Math.hypot(e.clientX - drag.startX, e.clientY - drag.startY) >= 7) {
        drag.active = true;
        if (!org.selected.has(item.uid)) org.selectOnly(item.uid);
        for (const uid of org.selected) cards.get(uid)?.classList.add('dragging');
        dragStatus.textContent = `Moving ${org.selected.size} page(s). Release at the insertion marker.`;
        drag.frame = window.requestAnimationFrame(edgeScroll);
      }
      if (drag.active) { e.preventDefault(); updateDropTarget(); }
    });
    card.addEventListener('pointerup', (e) => { if (drag?.pointerId === e.pointerId && drag.uid === item.uid) finishDrag(true); });
    card.addEventListener('pointercancel', (e) => { if (drag?.pointerId === e.pointerId && drag.uid === item.uid) finishDrag(false); });
    card.addEventListener('lostpointercapture', () => { if (drag?.uid === item.uid) finishDrag(false); });
    cards.set(item.uid, card);
    return card;
  }

  function focusCard(uid: string): void { window.setTimeout(() => cards.get(uid)?.focus(), 0); }

  function fillThumb(card: HTMLElement, item: PageItem): void {
    const thumb = card.querySelector('.page-thumb') as HTMLElement;
    const kind = c.kindOf(item.docId);
    const rot = item.rotation;
    if (item.blank) { replace(thumb, h('div', { class: 'blank-page', text: 'Blank page' })); return; }
    const doc = ctx.docs.get(item.docId);
    if ((kind === 'pdf' || kind === 'image') && c.options.target === 'pdf' && doc) {
      let img = thumb.querySelector('img') as HTMLImageElement | null;
      const url = ctx.api.pageImageUrl(doc.id, item.page, 240, doc.sha256);
      if (!img || img.getAttribute('src') !== url) {
        img = h('img', { attrs: { src: url, alt: `${doc.name} page ${item.page + 1}`, loading: 'lazy', draggable: false } });
        replace(thumb, img);
      }
      const swap = rot % 180 !== 0;
      img.style.transform = rot ? `rotate(${rot}deg)${swap ? ' scale(0.72)' : ''}` : '';
    } else {
      const o = c.outlines.get(item.docId)?.pages.find((p) => p.index === item.page);
      replace(thumb, h('div', { class: 'text-page' }, h('div', { class: 'tp-label', text: item.label }), o?.words !== undefined ? h('div', { class: 'tp-meta muted', text: `${o.words} words · ${o.blocks ?? 0} blocks` }) : null,
        o?.kinds?.length ? h('div', { class: 'tp-kinds' }, ...o.kinds.map((k) => badge(k, 'neutral'))) : null));
    }
  }

  function render(): void {
    const docs = c.docs();
    // files
    replace(fileList, ...(docs.length ? docs.map((d, i) => docRow(ctx, d, { remove: false, actions: [
      h('span', { class: 'swatch', style: { background: docColour(i) }, attrs: { 'aria-hidden': 'true' } }),
      button('↑', () => { c.set.move(d.id, -1); reorder(); }, 'btn-icon', { attrs: { 'aria-label': 'Move file up' } }),
      button('↓', () => { c.set.move(d.id, 1); reorder(); }, 'btn-icon', { attrs: { 'aria-label': 'Move file down' } }),
      button('✕', () => { c.set.remove(d.id); dropMissing(org, new Set(c.docs().map((x) => x.id))); render(); }, 'btn-icon', { attrs: { 'aria-label': `Remove ${d.name} from the merge` } })] }))
      : [emptyState('No files yet', 'Add PDFs, Word files, images and more.')]));
    // grid (keyed, so images are not reloaded on every change)
    const live = new Set(org.items.map((i) => i.uid));
    for (const [uid, el] of cards) if (!live.has(uid)) { el.remove(); cards.delete(uid); }
    grid.querySelector('.empty')?.remove();
    let n = 0;
    for (const item of org.items) {
      n++;
      const card = cards.get(item.uid) ?? makeCard(item);
      card.classList.toggle('selected', org.selected.has(item.uid));
      card.classList.toggle('excluded', !item.included);
      card.setAttribute('aria-selected', String(org.selected.has(item.uid)));
      card.style.setProperty('--doc', docColour(Math.max(0, c.docIndex(item.docId))));
      (card.querySelector('.pos') as HTMLElement).textContent = String(n);
      (card.querySelector('.src') as HTMLElement).textContent = item.blank ? 'blank' : `${c.outlines.get(item.docId)?.name ?? item.docName} · ${c.kindOf(item.docId) === 'image' ? 'image' : `p. ${item.page + 1}`}`;
      (card.querySelector('.src') as HTMLElement).title = item.docName;
      card.title = item.included ? '' : 'Excluded: this page will not be in the result';
      fillThumb(card, item);
      if (grid.children[n - 1] !== card) grid.insertBefore(card, grid.children[n - 1] ?? null);   // keeps keyboard focus on cards that did not move
    }
    if (!org.items.length) grid.appendChild(emptyState('The pages of your files appear here', 'Drag them into the order you want, exclude the ones you do not need, then merge.'));
    // toolbar state
    const some = org.selected.size > 0;
    for (const k of ['up', 'down', 'toggle', 'dup', 'remove'] as const) buttons[k].disabled = !some;
    const canRot = org.selectedItems().some(c.canRotate);
    buttons.left.disabled = buttons.right.disabled = !canRot;
    buttons.left.title = buttons.right.title = c.rotationAllowed ? 'Rotate the selected pages' : 'Rotation applies when merging into PDF';
    buttons.blank.disabled = !org.items.length || !c.rotationAllowed;
    buttons.blank.title = c.rotationAllowed ? 'Insert a blank page after the selection' : 'Blank pages are added when merging into PDF';
    buttons.reverse.disabled = org.items.length < 2;
    buttons.undo.disabled = !org.canUndo;
    buttons.redo.disabled = !org.canRedo;
    mergeBtn.disabled = c.busy || org.includedCount() === 0;
    editBtn.disabled = !docs.some((d) => d.format === 'pdf');
    const excluded = org.items.length - org.includedCount();
    status.textContent = org.items.length ? `${org.includedCount()} of ${plural(org.items.length, 'page')} will be merged${excluded ? ` · ${excluded} excluded` : ''}${some ? ` · ${org.selected.size} selected` : ''}` : '';
    replace(notices, ...c.notices.slice(0, 4).map((t) => h('div', { class: 'notice', text: t })));
  }

  /** After files were reordered, the list order of whole documents follows the file order. */
  function reorder(): void {
    const order = c.docs().map((d) => d.id);
    const blocks = new Map<string, PageItem[]>();
    for (const i of org.items) { const k = i.blank ? '' : i.docId; blocks.set(k, [...(blocks.get(k) ?? []), i]); }
    // keep each document's own page order and exclusions; only the documents' relative order follows the file list
    org.items = [...order.flatMap((id) => blocks.get(id) ?? []), ...(blocks.get('') ?? [])];
    render();
  }

  function renderResult(r: MergeResult | null): void {
    if (!r) { replace(resultBox); return; }
    replace(resultBox, h('section', { class: 'card result' },
      h('div', { class: 'card-head' }, h('h3', { text: r.output.name }), badge(r.verification.ok ? 'Checked' : 'Check failed', r.verification.ok ? 'ok' : 'bad')),
      h('div', { class: 'row-actions' }, downloadLink(ctx, r.output),
        r.output.format === 'pdf' ? button('Open in PDF Editor', () => { void ctx.api.post<DocInfo>(`/api/outputs/${r.output.id}/open`).then((d) => { ctx.docs.set(d); ctx.open('editor', d.id); }).catch((e) => toast(errorMessage(e), 'error')); }, 'btn-small') : null,
        h('span', { class: 'muted small', text: `${plural(r.plan.pages, 'page')} · ${r.engine} · ${formatDuration(r.durationMs)}${r.bookmarks ? ` · ${plural(r.bookmarks, 'bookmark')}` : ''}` })),
      listBlock('Warnings', r.warnings, 'warn'),
      h('div', { class: 'two-lists' }, listBlock('Kept', r.kept, 'ok'), listBlock('Not carried over', r.lost, 'lost')),
      verificationView(r.verification),
      h('details', { class: 'verify' }, h('summary', { text: 'Sources' }), h('ul', {}, ...r.inputs.map((i) => h('li', {}, `${i.name} (${i.format.toUpperCase()}, ${plural(i.pages, 'page')}) `, h('code', { class: 'sha', text: i.sha256.slice(0, 12) })))))));
  }

  const dz = dropzone({
    title: 'Drop files to merge', hint: 'PDF, Word, ODT, RTF, HTML, Markdown, text, SLDC, PNG, JPEG', accept: `${DOCUMENT_ACCEPT},${IMAGE_ACCEPT}`,
    onFiles: (files) => { void c._add_merge_files(files); },
  });

  grid.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); if (e.shiftKey) org.redo(); else org.undo(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') { e.preventDefault(); org.redo(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') { e.preventDefault(); org.selectAll(); }
  });
  window.addEventListener('blur', () => finishDrag(false));
  grid.addEventListener('keydown', (e) => { if (e.key === 'Escape' && drag) finishDrag(false); });

  const el = h('div', { class: 'workspace merger' },
    h('header', { class: 'ws-head' }, h('h1', { text: 'Merger & Page Organizer' }), h('p', { class: 'muted', text: 'Combine documents and arrange their pages: drag to reorder, exclude, duplicate or rotate pages, then merge. PDF pages are copied unchanged.' })),
    h('div', { class: 'two-col merger-cols' },
      h('div', { class: 'col' }, dz.el, c.set.picker(), sectionTitle('Files', button('Clear', () => void c._clear_merge_files(), 'btn-small')), fileList, optionsBox, prog.el, resultBox),
      h('div', { class: 'col wide' }, sectionTitle('Pages', status), dragStatus, toolbar, notices, grid)));
  renderOptions();
  render();
  return {
    id: 'merger', title: 'Merger', el,
    activate: () => { render(); },
  };
}
