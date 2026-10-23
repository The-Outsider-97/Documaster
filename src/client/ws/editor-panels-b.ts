// Right-hand panels of the PDF editor, part 2: redaction, forms, and document properties / security.
import { listBlock, verificationView } from '../components.js';
import { badge, button, checkbox, confirmDialog, errorMessage, field, h, promptPassword, replace, select, toast } from '../dom.js';
import { plural } from '../format.js';
import type { Rect } from '../geometry.js';
import type { FindHit, FormField, OpResult, OutlineNode, PiiRegions } from '../types.js';
import type { Panel, PanelEnv } from './editor-core.js';

const permissionNotice = (env: PanelEnv, perm: 'modify' | 'fillForms', what: string): HTMLElement | null =>
  env.core.can(perm) ? null : h('div', { class: 'notice warn', text: `This PDF's permissions do not allow ${what}. Open it with the owner password to continue.` });

// ============================================================================================ redact
export function redactPanel(env: PanelEnv): Panel {
  const { core } = env;
  const root = h('div', { class: 'panel-body' });
  const opts = { dpi: 200, lossless: false, sanitize: true };
  const q = { query: '', regex: false, caseSensitive: false };
  const found = h('div', { class: 'item-list' });
  const pii = h('div', { class: 'item-list' });
  const staged = h('div', { class: 'item-list' });
  const outcome = h('div', {});
  const applyBtn = button('Permanently redact…', () => void apply(), 'btn-danger');
  let matches: FindHit[] = [];
  let piiResult: PiiRegions | null = null;
  const piiPicked = new Set<number>();
  const matchPicked = new Set<number>();

  async function find(): Promise<void> {
    const d = core.doc;
    if (!d || !q.query) return;
    try {
      const r = await core.ctx.api.post<{ hits: FindHit[] }>(`/api/documents/${d.id}/find`, { query: q.query, regex: q.regex, case: q.caseSensitive });
      matches = r.hits;
      matchPicked.clear();
      matches.forEach((_, i) => matchPicked.add(i));
      render();
      if (!matches.length) toast('No matches found');
    } catch (e) { toast(errorMessage(e), 'error'); }
  }

  async function scanPii(): Promise<void> {
    const d = core.doc;
    if (!d) return;
    try {
      piiResult = await core.ctx.api.post<PiiRegions>(`/api/documents/${d.id}/pii`, {});
      piiPicked.clear();
      piiResult.items.forEach((_, i) => piiPicked.add(i));
      render();
    } catch (e) { toast(errorMessage(e), 'error'); }
  }

  function markMatches(): void {
    let n = 0;
    matches.forEach((m, i) => { if (matchPicked.has(i)) for (const r of m.rects) { core.stage({ kind: 'redaction', page: m.page, rect: r as Rect, label: m.text }); n++; } });
    toast(`${plural(n, 'area')} marked`, 'ok');
  }
  function markPii(): void {
    let n = 0;
    piiResult?.items.forEach((g, i) => { if (piiPicked.has(i)) for (const hit of g.hits) for (const r of hit.rects) { core.stage({ kind: 'redaction', page: hit.page, rect: r as Rect, label: g.label }); n++; } });
    toast(`${plural(n, 'area')} marked`, 'ok');
  }

  async function apply(): Promise<void> {
    const marks = core.stagedOf('redaction');
    if (!marks.length) return;
    const pages = new Set(marks.map((m) => m.page));
    const ok = await confirmDialog('Permanently remove this content?',
      `${plural(marks.length, 'area')} on ${plural(pages.size, 'page')} will be removed for good. Those pages become images: their remaining text will no longer be selectable or searchable. ` +
      'Documaster checks the result afterwards and discards it if anything is left behind. You can still use Undo in this session.', 'Redact', true);
    if (!ok) return;
    const r = await core.run({ op: 'redact', regions: marks.map((m) => ({ page: m.page, rect: m.rect })), dpi: opts.dpi, lossless: opts.lossless, sanitize: opts.sanitize }, 'Redacting', { quiet: true });
    if (r) showOutcome(r); else replace(outcome);
  }

  function showOutcome(r: OpResult): void {
    const d = r.details as { sameTextOnOtherPages?: { page: number }[]; cleaned?: Record<string, number> };
    replace(outcome, h('div', { class: 'card result' },
      h('div', { class: 'card-head' }, h('h4', { text: r.label }), badge(r.verification?.ok ? 'Verified' : 'Not verified', r.verification?.ok ? 'ok' : 'bad')),
      verificationView(r.verification), listBlock('Notes', r.warnings, 'warn'),
      d.cleaned ? h('div', { class: 'muted small', text: `Also removed: ${Object.entries(d.cleaned).filter(([, v]) => v).map(([k, v]) => `${v} ${k.replace(/([A-Z])/g, ' $1').toLowerCase()}`).join(', ') || 'nothing else was present'}` }) : null));
    outcome.scrollIntoView({ block: 'nearest' });
  }

  function render(): void {
    const marks = core.stagedOf('redaction');
    replace(staged, ...(marks.length ? marks.map((m) => h('div', { class: `item${core.selectedStaged === m.id ? ' selected' : ''}` },
      h('span', { class: 'clip', text: `page ${m.page + 1}${m.label ? ` · ${m.label}` : ''}` }), h('span', { class: 'item-actions' }, button('Go', () => { core.setPage(m.page); core.selectedStaged = m.id; core.changed.emit('staged'); }, 'btn-small'), button('✕', () => core.unstage(m.id), 'btn-icon', { attrs: { 'aria-label': 'Unmark' } })))) : [h('div', { class: 'muted small', text: 'Nothing is marked.' })]));
    replace(found, ...(matches.length ? [h('div', { class: 'muted small', text: plural(matches.length, 'match', 'matches') }),
      ...matches.slice(0, 200).map((m, i) => checkbox(`p. ${m.page + 1} · ${m.text}`, matchPicked.has(i), (v) => { if (v) matchPicked.add(i); else matchPicked.delete(i); })),
      button('Mark selected matches', markMatches, 'btn-small')] : []));
    replace(pii, ...(piiResult ? (piiResult.items.length ? [
      ...piiResult.items.map((g, i) => checkbox(`${g.label}: ${g.text}${g.count > 1 ? ` (×${g.count})` : ''}`, piiPicked.has(i), (v) => { if (v) piiPicked.add(i); else piiPicked.delete(i); }, g.confidence.startsWith('checksum (weak') ? 'weak check: may be an ordinary number' : undefined)),
      button('Mark selected', markPii, 'btn-small'), h('div', { class: 'muted small', text: piiResult.note })] : [h('div', { class: 'muted small', text: `Nothing found. ${piiResult.note}` })]) : []));
    applyBtn.disabled = !marks.length || core.busy || !core.can('modify');
    applyBtn.textContent = marks.length ? `Permanently redact ${plural(marks.length, 'area')}…` : 'Permanently redact…';
    for (const b of root.querySelectorAll<HTMLButtonElement>('button[data-tool]')) b.classList.toggle('active', core.tool === 'redact');
  }

  const notice = h('div', { class: 'notice-slot' });
  const syncNotice = (): void => replace(notice, permissionNotice(env, 'modify', 'redaction'));
  core.changed.on((k) => { if (k === 'doc') syncNotice(); });
  syncNotice();
  replace(root, notice,
    h('p', { class: 'muted small', text: 'Redaction removes content for good: marked pages are rebuilt as images with black boxes, and the result is checked for leftover text before you get it.' }),
    h('div', { class: 'row-actions wrap' }, button('Draw redaction area', () => core.setTool(core.tool === 'redact' ? 'view' : 'redact'), 'btn-tool', { attrs: { 'data-tool': 'redact' } })),
    h('h4', { text: 'Find text to remove' }),
    h('input', { class: 'input', attrs: { type: 'search', placeholder: 'Text or pattern', 'aria-label': 'Text to redact' }, on: { input: (e) => { q.query = (e.target as HTMLInputElement).value; }, keydown: (e) => { if (e.key === 'Enter') void find(); } } }),
    h('div', { class: 'row-actions wrap' }, checkbox('Pattern (regex)', false, (v) => { q.regex = v; }), checkbox('Match case', false, (v) => { q.caseSensitive = v; }), button('Find', () => void find(), 'btn-small')), found,
    h('h4', { text: 'Personal data' }),
    h('div', { class: 'row-actions wrap' }, button('Scan for personal data', () => void scanPii(), 'btn-small')), pii,
    h('hr'), h('h4', { text: 'Marked for removal' }), staged,
    h('div', { class: 'card options' },
      field('Quality of redacted pages', select([{ value: '150', label: '150 dpi (smaller)' }, { value: '200', label: '200 dpi' }, { value: '300', label: '300 dpi (sharper)' }], '200', (v) => { opts.dpi = Number(v); })),
      checkbox('Lossless images (larger files)', false, (v) => { opts.lossless = v; }), checkbox('Also remove hidden data (properties, attachments, scripts)', true, (v) => { opts.sanitize = v; })),
    h('div', { class: 'row-actions' }, applyBtn, button('Unmark all', () => core.clearStaged('redaction'), 'btn-small')), outcome);
  core.changed.on((k) => { if (k === 'staged' || k === 'tool' || k === 'busy' || k === 'doc') render(); });
  render();
  return { id: 'redact', title: 'Redact', el: root, refresh: render };
}

// ============================================================================================ forms
export function formsPanel(env: PanelEnv): Panel {
  const { core } = env;
  const root = h('div', { class: 'panel-body' });
  const edited = new Map<string, string | boolean>();
  let flatten = false;

  function control(f: FormField): HTMLElement {
    const on = (v: string | boolean): void => { edited.set(f.name, v); };
    if (f.readOnly) return h('div', { class: 'muted small', text: `${f.name}: ${f.value ?? ''} (read-only)` });
    if (f.type === 'signature') return h('div', { class: 'muted small', text: `${f.name}: signature field. Documaster never signs on anyone’s behalf.` });
    if (f.type === 'checkbox/button') return checkbox(f.name, !!f.value && f.value !== '/Off', on);
    if (f.type === 'choice' && f.options?.length) return field(f.name, select([{ value: '', label: '' }, ...f.options.map((o) => ({ value: o, label: o }))], f.value ?? '', (v) => on(v)));
    return field(f.name, h('input', { class: 'input', value: f.value ?? '', attrs: { type: 'text' }, on: { input: (e) => on((e.target as HTMLInputElement).value) } }));
  }

  function render(): void {
    const fields = core.info?.fields ?? [];
    edited.clear();
    replace(root, permissionNotice(env, 'fillForms', 'filling forms'),
      fields.length ? h('div', {},
        h('p', { class: 'muted small', text: `${plural(fields.length, 'field')} found. Fill the ones you need.` }),
        ...fields.map(control),
        checkbox('Flatten after filling (makes the answers part of the page)', false, (v) => { flatten = v; }),
        h('div', { class: 'row-actions' }, button('Fill form', () => {
          if (!edited.size) { toast('Change at least one field'); return; }
          void core.run({ op: 'fill_form', values: Object.fromEntries(edited), flatten }, 'Filling form');
        }, 'btn-primary'), button('Flatten form fields', async () => { if (await confirmDialog('Flatten the form?', 'The fields become part of the page and can no longer be edited. You can undo this.', 'Flatten')) void core.run({ op: 'flatten' }, 'Flattening'); }, 'btn-small')))
        : h('div', { class: 'muted', text: 'This PDF has no fillable form fields.' }));
  }
  core.changed.on((k) => { if (k === 'info' || k === 'doc') render(); });
  render();
  return { id: 'forms', title: 'Forms', el: root, refresh: render };
}

// ============================================================================================ document
interface Bookmark { title: string; page: number; children: Bookmark[] }
const toBm = (n: OutlineNode[]): Bookmark[] => n.map((x) => ({ title: x.title, page: x.page === null ? 1 : x.page + 1, children: toBm(x.children) }));
const fromBm = (n: Bookmark[]): Record<string, unknown>[] => n.map((x) => ({ title: x.title, page: x.page - 1, children: fromBm(x.children) }));

export function documentPanel(env: PanelEnv): Panel {
  const { core } = env;
  const root = h('div', { class: 'panel-body' });
  let bookmarks: Bookmark[] = [];
  const meta: Record<string, string> = {};
  let hasTesseract = false;
  let ocrLang = 'eng';
  void core.ctx.api.get<{ tools: { tesseract: string | null; tesseractLanguages: string[] } }>('/api/capabilities').then((c) => { hasTesseract = !!c.tools.tesseract; render(); }).catch(() => undefined);

  function bmRows(list: Bookmark[], depth: number): HTMLElement[] {
    return list.flatMap((b, i) => [
      h('div', { class: 'bm-row', style: { paddingLeft: `${depth * 14}px` } },
        h('input', { class: 'input', value: b.title, attrs: { type: 'text', 'aria-label': 'Bookmark title' }, on: { input: (e) => { b.title = (e.target as HTMLInputElement).value; } } }),
        h('input', { class: 'input short', value: String(b.page), attrs: { type: 'number', min: 1, max: core.pages, 'aria-label': 'Page' }, on: { input: (e) => { b.page = Number((e.target as HTMLInputElement).value) || 1; } } }),
        button('✕', () => { list.splice(i, 1); render(); }, 'btn-icon', { attrs: { 'aria-label': 'Remove bookmark' } })),
      ...bmRows(b.children, depth + 1)]);
  }

  async function setPassword(): Promise<void> {
    const pw = await promptPassword('Protect this PDF', 'Choose a password (4 to 128 characters). Anyone opening the file will need it. Documaster does not store it anywhere.');
    if (pw) await core.run({ op: 'password', new: pw }, 'Protecting');
  }

  function render(): void {
    const d = core.doc;
    const info = core.info;
    bookmarks = info ? toBm(info.outline) : [];
    const m = info?.metadata ?? {};
    for (const k of ['title', 'author', 'subject', 'keywords']) meta[k] = String(m[k] ?? '');
    const perms = d?.permissions;
    replace(root, permissionNotice(env, 'modify', 'changing the document'),
      h('h4', { text: 'Properties' }),
      ...(['title', 'author', 'subject', 'keywords'] as const).map((k) => field(k[0]!.toUpperCase() + k.slice(1), h('input', { class: 'input', value: meta[k] ?? '', attrs: { type: 'text', maxlength: 2000 }, on: { input: (e) => { meta[k] = (e.target as HTMLInputElement).value; } } }))),
      button('Save properties', () => void core.run({ op: 'metadata', values: { ...meta } }, 'Saving properties'), 'btn-small'),
      h('hr'), h('h4', { text: 'Bookmarks' }),
      ...(bookmarks.length ? bmRows(bookmarks, 0) : [h('div', { class: 'muted small', text: 'No bookmarks.' })]),
      h('div', { class: 'row-actions wrap' }, button('+ Add for the current page', () => { bookmarks.push({ title: `Page ${core.page + 1}`, page: core.page + 1, children: [] }); render(); }, 'btn-small'),
        button('Save bookmarks', () => void core.run({ op: 'bookmarks', entries: fromBm(bookmarks) }, 'Saving bookmarks'), 'btn-small')),
      h('hr'), h('h4', { text: 'Security' }),
      h('div', { class: 'muted small', text: d?.encrypted ? `Password-protected${d.owner ? ' (opened with the owner password)' : ''}.` : 'Not password-protected.' }),
      perms ? h('div', { class: 'perm-grid' }, ...Object.entries(perms).map(([k, v]) => badge(`${k}: ${v ? 'allowed' : 'not allowed'}`, v ? 'ok' : 'warn'))) : null,
      h('div', { class: 'row-actions wrap' }, button(d?.encrypted ? 'Change password…' : 'Set a password…', () => void setPassword(), 'btn-small'),
        d?.encrypted ? button('Remove password', async () => { if (await confirmDialog('Remove the password?', 'The saved copy will open without a password.', 'Remove')) void core.run({ op: 'password', new: '' }, 'Removing password'); }, 'btn-small') : null),
      h('hr'), h('h4', { text: 'Clean up' }),
      h('div', { class: 'row-actions wrap' },
        button('Remove hidden data', async () => { if (await confirmDialog('Remove hidden data?', 'Properties, embedded attachments, scripts and automatic actions are removed. The visible pages stay as they are.', 'Remove')) void core.run({ op: 'sanitize' }, 'Removing hidden data'); }, 'btn-small'),
        button('Compress (lossless)', () => void core.run({ op: 'compress' }, 'Compressing'), 'btn-small')),
      hasTesseract ? h('div', {}, h('hr'), h('h4', { text: 'Text recognition (OCR)' }),
        h('p', { class: 'muted small', text: 'Adds a searchable text layer to pages that have none. Recognised text can contain mistakes.' }),
        h('div', { class: 'row' }, field('Language', h('input', { class: 'input short', value: ocrLang, attrs: { type: 'text' }, on: { input: (e) => { ocrLang = (e.target as HTMLInputElement).value || 'eng'; } } })), button('Recognise text', () => void core.run({ op: 'ocr', lang: ocrLang }, 'Recognising text'), 'btn-small'))) : null);
  }
  core.changed.on((k) => { if (k === 'info' || k === 'doc') render(); });
  render();
  return { id: 'document', title: 'Document', el: root, refresh: render };
}
