// Shared state of the PDF editor: the open document, the current page, staged (not yet applied) marks, and the way edits are run.
import { ApiError } from '../api.js';
import { button, errorMessage, toast } from '../dom.js';
import { Emitter, type AppContext } from '../store.js';
import type { Rect } from '../geometry.js';
import type { DocInfo, FindHit, OpResult, PdfInfo } from '../types.js';
import type { Progress } from '../components.js';

export type Tool = 'view' | 'highlight' | 'underline' | 'strikeout' | 'note' | 'rect' | 'ellipse' | 'ink' | 'redact' | 'textbox' | 'imagebox' | 'crop' | 'stamp';

export interface Style { color: string; width: number; opacity: number; fill: string }

/** Marks drawn on the page but not yet written into the file. */
export type Staged =
  | { id: number; kind: 'annotation'; page: number; spec: Record<string, unknown>; preview: { rect?: Rect; rects?: Rect[]; point?: [number, number]; strokes?: [number, number][][]; type: string; color: string } }
  | { id: number; kind: 'redaction'; page: number; rect: Rect; label?: string }
  | { id: number; kind: 'textbox'; page: number; rect: Rect; text: string; size: number; color: string }
  | { id: number; kind: 'image'; page: number; rect: Rect; asset: string; name: string }
  | { id: number; kind: 'crop'; page: number; rect: Rect };

export class EditorCore {
  doc: DocInfo | null = null;
  info: PdfInfo | null = null;
  page = 0;
  tool: Tool = 'view';
  zoom = 1;                       // 1 = fit width
  style: Style = { color: '#e11d48', width: 2, opacity: 1, fill: '' };
  hits: FindHit[] = [];
  hitIndex = -1;
  staged: Staged[] = [];
  selectedStaged: number | null = null;
  pendingAsset: { id: string; name: string } | null = null;
  /** Settings of the text stamp placed by clicking on the page. */
  stampForm = { text: '', size: 24, color: '#dc2626', opacity: 1, rotation: 0 };
  busy = false;
  private seq = 0;
  readonly changed = new Emitter<'doc' | 'page' | 'tool' | 'staged' | 'hits' | 'info' | 'busy'>();

  constructor(readonly ctx: AppContext, readonly progress: Progress) {}

  get pages(): number { return this.doc?.pages ?? 0; }
  get canEdit(): boolean { return !!this.doc && !this.doc.locked; }

  can(permission: 'annotate' | 'modify' | 'fillForms' | 'assemble' | 'copy' | 'print'): boolean {
    const d = this.doc;
    if (!d) return false;
    return !d.encrypted || d.owner || !d.permissions || d.permissions[permission] !== false;
  }

  async open(doc: DocInfo, page = 0): Promise<void> {
    this.doc = doc;
    this.page = Math.max(0, Math.min(page, Math.max(0, (doc.pages ?? 1) - 1)));
    this.staged = [];
    this.hits = [];
    this.hitIndex = -1;
    this.info = null;
    this.changed.emit('doc');
    await this.loadInfo();
  }

  close(): void {
    this.doc = null;
    this.info = null;
    this.staged = [];
    this.hits = [];
    this.changed.emit('doc');
  }

  async loadInfo(): Promise<void> {
    const d = this.doc;
    if (!d || d.locked) return;
    try {
      this.info = await this.ctx.api.get<PdfInfo>(`/api/documents/${d.id}/pdfinfo`);
    } catch (e) {
      this.info = null;
      toast(errorMessage(e), 'error');
    }
    this.changed.emit('info');
  }

  setPage(n: number): void {
    const p = Math.max(0, Math.min(n, this.pages - 1));
    if (p === this.page) return;
    this.page = p;
    this.changed.emit('page');
  }

  setTool(t: Tool): void { this.tool = t; this.selectedStaged = null; this.changed.emit('tool'); }
  setZoom(z: number): void { this.zoom = Math.max(0.25, Math.min(4, z)); this.changed.emit('page'); }

  // ---- staged marks ------------------------------------------------------------------------------------------------------
  stage(item: Staged extends infer S ? S extends { id: number } ? Omit<S, 'id'> : never : never): number {
    const id = ++this.seq;
    this.staged.push({ ...item, id } as Staged);
    this.changed.emit('staged');
    return id;
  }
  unstage(id: number): void {
    this.staged = this.staged.filter((s) => s.id !== id);
    if (this.selectedStaged === id) this.selectedStaged = null;
    this.changed.emit('staged');
  }
  clearStaged(kind?: Staged['kind']): void {
    this.staged = kind ? this.staged.filter((s) => s.kind !== kind) : [];
    this.changed.emit('staged');
  }
  stagedOf<K extends Staged['kind']>(kind: K): Extract<Staged, { kind: K }>[] {
    return this.staged.filter((s): s is Extract<Staged, { kind: K }> => s.kind === kind);
  }

  setHits(hits: FindHit[]): void { this.hits = hits; this.hitIndex = hits.length ? 0 : -1; this.changed.emit('hits'); }

  // ---- running edits ------------------------------------------------------------------------------------------------------
  /** Run an edit as a job. Updates the document, reloads its details, and returns the result (or null if it failed). */
  async run(spec: Record<string, unknown>, label: string, opts: { keepStaged?: boolean; quiet?: boolean } = {}): Promise<OpResult | null> {
    const d = this.doc;
    if (!d || this.busy) return null;
    this.busy = true;
    this.changed.emit('busy');
    try {
      const r = await this.progress.run<OpResult>(label, () => this.ctx.api.startJob(`/api/documents/${d.id}/op`, spec));
      this.ctx.docs.set(r.document);
      this.doc = r.document;
      if (!opts.keepStaged) this.staged = [];
      this.hits = [];
      this.hitIndex = -1;
      if (this.page >= (r.document.pages ?? 1)) this.page = Math.max(0, (r.document.pages ?? 1) - 1);
      await this.loadInfo();
      this.changed.emit('doc');
      this.changed.emit('staged');
      if (!opts.quiet) toast(r.changed ? r.label : r.label || 'Nothing changed', r.changed ? 'ok' : 'info');
      for (const w of r.warnings.slice(0, 2)) toast(w, 'info', 9000);
      return r;
    } catch (e) {
      if (e instanceof ApiError && e.cancelled) toast('Cancelled');
      else toast(errorMessage(e), 'error');
      return null;
    } finally {
      this.busy = false;
      this.changed.emit('busy');
    }
  }

  async step(direction: 'undo' | 'redo'): Promise<void> {
    const d = this.doc;
    if (!d) return;
    try {
      const nd = await this.ctx.api.post<DocInfo>(`/api/documents/${d.id}/${direction}`);
      this.ctx.docs.set(nd);
      this.doc = nd;
      this.staged = [];
      this.hits = [];
      if (this.page >= (nd.pages ?? 1)) this.page = Math.max(0, (nd.pages ?? 1) - 1);
      await this.loadInfo();
      this.changed.emit('doc');
      this.changed.emit('staged');
    } catch (e) { toast(errorMessage(e), 'error'); }
  }

  async goto(revision: number): Promise<void> {
    const d = this.doc;
    if (!d) return;
    try {
      const nd = await this.ctx.api.post<DocInfo>(`/api/documents/${d.id}/revision/${revision}`);
      this.ctx.docs.set(nd);
      this.doc = nd;
      this.staged = [];
      await this.loadInfo();
      this.changed.emit('doc');
      this.changed.emit('staged');
    } catch (e) { toast(errorMessage(e), 'error'); }
  }

  /** Refresh our copy of the document after something else changed it. */
  sync(all: DocInfo[]): void {
    if (!this.doc) return;
    const fresh = all.find((d) => d.id === this.doc!.id);
    if (!fresh) { this.close(); return; }
    if (fresh.sha256 !== this.doc.sha256 || fresh.locked !== this.doc.locked || fresh.revision.current !== this.doc.revision.current) {
      this.doc = fresh;
      void this.loadInfo().then(() => this.changed.emit('doc'));
    } else this.doc = fresh;
  }
}

export function tinyButton(label: string, title: string, fn: () => void): HTMLButtonElement {
  return button(label, fn, 'btn-small', { title, attrs: { 'aria-label': title } });
}

import type { Organizer } from '../organizer.js';

/** What the right-hand panels need from the editor. */
export interface PanelEnv {
  core: EditorCore;
  org: Organizer;
  /** Apply a change to the page list as one edit: ``change`` modifies the organiser, which is then sent as an arrangement. */
  pageOp(label: string, change: () => boolean): Promise<void>;
  /** Make sure something is selected (the current page if nothing is). */
  ensureSelection(): void;
  /** Hand the edited file to the Converter or another workspace. */
  open(workspace: 'converter' | 'merger' | 'reader' | 'assistant', docId: string): void;
}

export interface Panel { id: string; title: string; el: HTMLElement; refresh(): void }
