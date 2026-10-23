// PDF Editor: view pages, rearrange them, annotate, stamp, redact for real, fill forms, and manage properties and security.
// Every change is a revision on the server with undo/redo; drafts drawn on the page are only written when you apply them.
import { dropzone, downloadLink, emptyState, ingest, progress } from '../components.js';
import { button, errorMessage, h, replace, select, toast } from '../dom.js';
import { formatBytes } from '../format.js';
import { Organizer, type PageItem } from '../organizer.js';
import type { AppContext } from '../store.js';
import type { DocInfo } from '../types.js';
import { EditorCore, type Panel, type PanelEnv } from './editor-core.js';
import { annotatePanel, findPanel, pagesPanel, stampPanel } from './editor-panels-a.js';
import { documentPanel, formsPanel, redactPanel } from './editor-panels-b.js';
import { createThumbs, createViewer } from './editor-view.js';
import type { Workspace } from './types.js';

export function createEditor(ctx: AppContext): Workspace {
  const prog = progress();
  const core = new EditorCore(ctx, prog);
  const org = new Organizer();

  // ---- page-list changes become an "arrange" edit ------------------------------------------------------------------------------
  function reloadOrganizer(): void {
    const d = core.doc;
    const pages = core.info?.pages ?? [];
    if (!d) { org.load([]); return; }
    org.load(pages.map((p): Omit<PageItem, 'uid'> => ({ docId: d.id, page: p.index, rotation: 0, included: true, label: `Page ${p.index + 1}`, docName: d.name, width: p.width, height: p.height, baseRotation: p.rotation })));
  }
  function ensureSelection(): void {
    if (org.selected.size) return;
    const item = org.items.find((i) => !i.blank && i.page === core.page);
    if (item) org.selectOnly(item.uid);
  }
  async function pageOp(label: string, change: () => boolean): Promise<void> {
    const d = core.doc;
    if (!d || core.busy) return;
    ensureSelection();
    if (!change()) return;
    if (org.includedCount() === 0) { toast('A PDF needs at least one page', 'error'); org.undo(); return; }
    const plan = org.toPlan([d.id]).map((e) => (e.blank ? e : { page: e.page, rotation: e.rotation, included: e.included }));
    const r = await core.run({ op: 'arrange', plan }, label);
    if (!r) org.undo();
  }
  const env: PanelEnv = { core, org, pageOp, ensureSelection, open: (w, id) => ctx.open(w, id) };

  // ---- pieces ------------------------------------------------------------------------------------------------------------------------
  const stampAt = async (page: number, p: { x: number; y: number }): Promise<void> => {
    const f = core.stampForm;
    if (!f.text.trim()) { toast('Type the text to place first (Add tab)', 'error'); return; }
    await core.run({ op: 'stamp', kind: 'text', text: f.text, size: f.size, color: f.color, rotation: f.rotation, point: [Math.round(p.x * 10000) / 10000, Math.round(p.y * 10000) / 10000], pages: [page + 1] }, 'Placing text');
    core.setTool('view');
  };
  const viewer = createViewer(core, (page, p) => void stampAt(page, p));
  const thumbs = createThumbs(core, org, (before) => void pageOp('Moving pages', () => org.dropBefore(before)));
  const panels: Panel[] = [pagesPanel(env), annotatePanel(env), stampPanel(env), redactPanel(env), formsPanel(env), documentPanel(env), findPanel(env)];
  let active = panels[0]!;

  const tabBar = h('div', { class: 'tabs', attrs: { role: 'tablist' } });
  const panelHost = h('div', { class: 'panel-host' });
  function showPanel(p: Panel): void {
    active = p;
    if (core.tool !== 'view') core.setTool('view');
    replace(panelHost, p.el);
    for (const b of tabBar.querySelectorAll('button')) { const on = b.dataset['panel'] === p.id; b.classList.toggle('active', on); b.setAttribute('aria-selected', String(on)); }
    p.refresh();
  }
  for (const p of panels) tabBar.appendChild(h('button', { class: 'tab', text: p.title, data: { panel: p.id }, attrs: { type: 'button', role: 'tab' }, on: { click: () => showPanel(p) } }));

  // ---- top bar ---------------------------------------------------------------------------------------------------------------------
  const docSelect = h('select', { class: 'input doc-select', attrs: { 'aria-label': 'PDF' } });
  docSelect.addEventListener('change', () => { const d = ctx.docs.get(docSelect.value); if (d) void core.open(d); });
  const pageInput = h('input', { class: 'input page-input', attrs: { type: 'number', min: 1, 'aria-label': 'Page number' } });
  pageInput.addEventListener('change', () => core.setPage((Number(pageInput.value) || 1) - 1));
  const pageTotal = h('span', { class: 'muted' });
  const zoomLabel = h('span', { class: 'zoom-label' });
  const undoBtn = button('↶ Undo', () => void core.step('undo'), 'btn-small', { title: 'Undo the last change (Ctrl+Z)' });
  const redoBtn = button('↷ Redo', () => void core.step('redo'), 'btn-small', { title: 'Redo (Ctrl+Y)' });
  const history = select([{ value: '0', label: 'Original' }], '0', (v) => void core.goto(Number(v)));
  history.classList.add('history');
  history.title = 'Versions of this document in this session';
  const savedBox = h('span', { class: 'saved' });
  const saveBtn = button('Save a copy', () => void saveCopy(), 'btn-primary btn-small', { title: 'Write the current version to your output folder; the original is never overwritten' });
  const openInput = h('input', { class: 'visually-hidden', attrs: { type: 'file', accept: '.pdf', 'aria-label': 'Open a PDF' } });
  openInput.addEventListener('change', () => { const f = [...(openInput.files ?? [])]; openInput.value = ''; if (f.length) void openFiles(f); });

  async function saveCopy(): Promise<void> {
    const d = core.doc;
    if (!d) return;
    try {
      const out = await ctx.api.post<{ id: string; name: string; bytes: number; path: string }>(`/api/documents/${d.id}/save`, {});
      replace(savedBox, downloadLink(ctx, out));
      toast(`Saved ${out.name} (${formatBytes(out.bytes)}) in your output folder`, 'ok');
    } catch (e) { toast(errorMessage(e), 'error'); }
  }

  async function openFiles(files: File[]): Promise<void> {
    const docs = await ingest(ctx, files, { tray: dz.tray });
    const pdfs = docs.filter((d) => d.format === 'pdf' && !d.locked);
    for (const d of docs) if (d.format !== 'pdf') toast(`${d.name} is not a PDF. Use the Converter to turn it into a PDF first.`, 'error');
    if (pdfs[0]) await core.open(pdfs[0]);
  }

  const topbar = h('div', { class: 'editor-top' },
    docSelect, button('Open PDF…', () => openInput.click(), 'btn-small'), openInput,
    h('span', { class: 'sep' }),
    button('◀', () => core.setPage(core.page - 1), 'btn-small', { attrs: { 'aria-label': 'Previous page' } }), pageInput, pageTotal, button('▶', () => core.setPage(core.page + 1), 'btn-small', { attrs: { 'aria-label': 'Next page' } }),
    h('span', { class: 'sep' }),
    button('−', () => core.setZoom(core.zoom / 1.2), 'btn-small', { attrs: { 'aria-label': 'Zoom out' } }), zoomLabel, button('+', () => core.setZoom(core.zoom * 1.2), 'btn-small', { attrs: { 'aria-label': 'Zoom in' } }), button('Fit', () => core.setZoom(1), 'btn-small'),
    h('span', { class: 'spacer' }), undoBtn, redoBtn, history, saveBtn, savedBox);

  const dz = dropzone({ title: 'Drop a PDF here to edit it', hint: 'PDF', accept: '.pdf', multiple: false, onFiles: (f) => void openFiles(f) });
  const empty = h('div', { class: 'editor-empty' }, dz.el, h('div', { class: 'muted', text: 'Or pick one that is already open:' }));
  const emptyPick = select([], '', (v) => { const d = ctx.docs.get(v); if (d) void core.open(d); });
  empty.appendChild(emptyPick);

  const banner = h('div', { class: 'banners' });
  const body = h('div', { class: 'editor-body' },
    h('aside', { class: 'editor-thumbs' }, thumbs.el),
    h('section', { class: 'editor-main' }, banner, viewer.el),
    h('aside', { class: 'editor-side' }, tabBar, panelHost));
  const el = h('div', { class: 'workspace editor' },
    h('header', { class: 'ws-head compact' }, h('h1', { text: 'PDF Editor' })),
    topbar, prog.el, empty, body);

  // ---- refresh ------------------------------------------------------------------------------------------------------------------------
  function refreshChrome(): void {
    const d = core.doc;
    const pdfs = ctx.docs.pdfs();
    const opts = (sel: HTMLSelectElement, label: string): void => {
      replace(sel, h('option', { text: pdfs.length ? label : 'No PDFs open', attrs: { value: '' } }), ...pdfs.map((p) => h('option', { text: p.name, attrs: { value: p.id } })));
      sel.value = d?.id ?? '';
    };
    opts(docSelect, 'Choose a PDF…');
    opts(emptyPick, 'Choose an open PDF…');
    emptyPick.hidden = !pdfs.length;
    (emptyPick.previousElementSibling as HTMLElement).hidden = !pdfs.length;
    const has = !!d;
    empty.hidden = has;
    body.hidden = !has;
    for (const b of [undoBtn, redoBtn, saveBtn]) b.disabled = !has || core.busy;
    undoBtn.disabled = !d?.revision.canUndo || core.busy;
    redoBtn.disabled = !d?.revision.canRedo || core.busy;
    replace(history, ...(d ? d.revision.labels.map((l, i) => h('option', { text: `${i + 1}. ${l}`, attrs: { value: String(i) } })) : []));
    history.value = String(d?.revision.current ?? 0);
    history.disabled = !d || d.revision.count < 2 || core.busy;
    pageInput.max = String(core.pages);
    pageInput.value = String(core.page + 1);
    pageTotal.textContent = `/ ${core.pages}`;
    zoomLabel.textContent = `${Math.round(core.zoom * 100)}%`;
    const notes: HTMLElement[] = [];
    if (d?.encrypted && !d.owner && d.permissions && Object.values(d.permissions).some((v) => !v)) notes.push(h('div', { class: 'notice warn', text: 'This PDF restricts some operations. Open it with the owner password for full editing.' }));
    for (const w of d?.warnings.slice(0, 2) ?? []) notes.push(h('div', { class: 'notice', text: w }));
    replace(banner, ...notes);
  }

  core.changed.on((kind) => {
    if (kind === 'doc') { refreshChrome(); viewer.refresh(); }
    else if (kind === 'info') { reloadOrganizer(); thumbs.refresh(); viewer.refresh(); }
    else if (kind === 'page') { refreshChrome(); thumbs.refresh(); viewer.refresh(); thumbs.el.querySelector('.thumb.current')?.scrollIntoView({ block: 'nearest' }); }
    else if (kind === 'tool' || kind === 'staged' || kind === 'hits') viewer.refresh();
    else if (kind === 'busy') refreshChrome();
  });
  org.onChange(() => thumbs.refresh());
  ctx.docs.changed.on((all) => { core.sync(all); refreshChrome(); });

  el.addEventListener('keydown', (e) => {
    const t = e.target as HTMLElement;
    if (t.matches('input,textarea,select')) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); void core.step(e.shiftKey ? 'redo' : 'undo'); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') { e.preventDefault(); void core.step('redo'); }
    else if (e.key === 'PageDown') { e.preventDefault(); core.setPage(core.page + 1); }
    else if (e.key === 'PageUp') { e.preventDefault(); core.setPage(core.page - 1); }
  });

  showPanel(active);
  refreshChrome();
  return {
    id: 'editor', title: 'PDF Editor', el,
    activate: (arg) => {
      refreshChrome();
      if (arg?.docId) {
        const d = ctx.docs.get(arg.docId);
        if (d && d.format === 'pdf') void core.open(d, (arg.page ?? 1) - 1);
      } else if (!core.doc) {
        const first = ctx.docs.pdfs()[0];
        if (first) void core.open(first);
      }
      viewer.refresh();
    },
  };
}

export { emptyState };
