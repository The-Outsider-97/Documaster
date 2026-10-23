// Converter: every output is re-opened and checked by the server before it is offered, and the result view says what was kept and lost.
import { dropzone, docRow, DOCUMENT_ACCEPT, downloadLink, emptyState, ingest, listBlock, progress, sectionTitle, verificationView } from '../components.js';
import { badge, button, checkbox, errorMessage, field, h, replace, select, toast } from '../dom.js';
import { formatDuration, plural } from '../format.js';
import type { AppContext } from '../store.js';
import type { ConvertResult, FormatsInfo } from '../types.js';
import { WorkingSet } from '../workingset.js';
import type { Workspace } from './types.js';

export function createConverter(ctx: AppContext): Workspace {
  const set = new WorkingSet(ctx);
  const docList = h('div', { class: 'doc-list' });
  const results = h('div', { class: 'results' });
  const prog = progress();
  const limits = h('div', { class: 'limits' });
  const opts = { target: 'pdf', engine: 'model', title: '', pageSize: '', fontSize: 11, pageNumbers: true, includeNotes: true, ocr: false, lineEnding: '\n', encrypt: '' };
  let formats: FormatsInfo | null = null;
  const target = select([{ value: 'pdf', label: 'PDF' }], 'pdf', (v) => { opts.target = v; showLimits(); syncOptions(); });
  const advanced = h('div', { class: 'advanced' });
  const convertBtn = button('Convert', () => void run(), 'btn-primary');

  function showLimits(): void {
    const t = formats?.targets.find((x) => x.id === opts.target);
    replace(limits, t ? listBlock('What to expect from this format', t.limits) : null);
  }

  function syncOptions(): void {
    const eng = formats?.engines.libreoffice
      ? field('Conversion engine', select([{ value: 'model', label: 'Documaster (structure-preserving, checked)' }, { value: 'libreoffice', label: 'LibreOffice (keeps page layout where it can)' }], opts.engine, (v) => { opts.engine = v; }),
        'LibreOffice reads the original file directly; it does not use the document model.') : null;
    const paged = ['pdf', 'docx', 'odt', 'rtf'].includes(opts.target);
    replace(advanced,
      h('div', { class: 'row' }, field('Title (optional)', h('input', { class: 'input', value: opts.title, attrs: { type: 'text', maxlength: 200 }, on: { input: (e) => { opts.title = (e.target as HTMLInputElement).value; } } }))),
      eng,
      paged ? h('div', { class: 'row' },
        field('Page size', select([{ value: '', label: 'Automatic' }, { value: 'A4', label: 'A4' }, { value: 'Letter', label: 'US Letter' }], opts.pageSize, (v) => { opts.pageSize = v; })),
        field('Font size (pt)', h('input', { class: 'input short', value: String(opts.fontSize), attrs: { type: 'number', min: 6, max: 36, step: 0.5 }, on: { input: (e) => { opts.fontSize = Number((e.target as HTMLInputElement).value) || 11; } } }))) : null,
      paged ? checkbox('Page numbers', opts.pageNumbers, (v) => { opts.pageNumbers = v; }) : null,
      checkbox('Include footnotes and citations', opts.includeNotes, (v) => { opts.includeNotes = v; }),
      formats?.engines.ocr ? checkbox('Read scanned PDF pages with OCR', opts.ocr, (v) => { opts.ocr = v; }, 'Recognised text can contain mistakes') : null,
      opts.target === 'sldc' ? field('Encrypt the SLDC file with a password (optional)', h('input', { class: 'input', attrs: { type: 'password', autocomplete: 'new-password' }, on: { input: (e) => { opts.encrypt = (e.target as HTMLInputElement).value; } } })) : null,
      opts.target === 'txt' ? field('Line endings', select([{ value: '\n', label: 'LF (Unix, macOS)' }, { value: '\r\n', label: 'CRLF (Windows)' }], opts.lineEnding, (v) => { opts.lineEnding = v; })) : null);
  }

  void ctx.formats().then((f) => {
    formats = f;
    replace(target, ...f.targets.map((t) => h('option', { text: t.available ? t.label : `${t.label} (unavailable)`, attrs: { value: t.id, disabled: !t.available } })));
    target.value = opts.target;
    showLimits();
    syncOptions();
  }).catch((e) => toast(errorMessage(e), 'error'));

  async function run(): Promise<void> {
    const docs = set.docs().filter((d) => !d.locked);
    if (!docs.length) { toast('Add at least one document first'); return; }
    if (prog.busy) return;
    const options: Record<string, unknown> = {
      target: opts.target, engine: opts.engine, title: opts.title || undefined, page_size: opts.pageSize || undefined,
      font_size_pt: opts.fontSize, page_numbers: opts.pageNumbers, include_notes: opts.includeNotes, ocr: opts.ocr, line_ending: opts.lineEnding,
      encrypt_password: opts.encrypt || undefined,
    };
    convertBtn.disabled = true;
    try {
      const r = await prog.run<ConvertResult>(`Converting ${plural(docs.length, 'document')} to ${opts.target.toUpperCase()}`, () => ctx.api.startJob('/api/convert', { docs: docs.map((d) => d.id), options }));
      renderResults(r);
    } catch (e) {
      if ((e as { cancelled?: boolean }).cancelled) toast('Conversion cancelled'); else toast(errorMessage(e), 'error');
    } finally { convertBtn.disabled = false; }
  }

  function renderResults(r: ConvertResult): void {
    const names = new Map(set.docs().map((d) => [d.id, d.name]));
    const summary = h('div', { class: 'summary-line' }, badge(`${r.succeeded} converted`, r.succeeded ? 'ok' : 'neutral'), r.failed ? badge(`${r.failed} failed`, 'bad') : null);
    const cards = r.results.map((it) => {
      if (!it.ok || !it.report) {
        return h('section', { class: 'card result bad' }, h('div', { class: 'card-head' }, h('h3', { text: it.name ?? names.get(it.id) ?? 'Document' }), badge('Failed', 'bad')),
          h('div', { class: 'error', text: it.error?.message ?? 'The conversion failed' }));
      }
      const rep = it.report;
      return h('section', { class: 'card result' },
        h('div', { class: 'card-head' }, h('h3', { text: `${rep.input.name} → ${rep.output.name}` }), badge(rep.verification.ok ? 'Checked' : 'Check failed', rep.verification.ok ? 'ok' : 'bad')),
        h('div', { class: 'row-actions' }, downloadLink(ctx, rep.output), h('span', { class: 'muted small', text: `${rep.engine === 'libreoffice' ? 'LibreOffice' : 'Documaster'} · ${formatDuration(rep.durationMs)} · ${Object.values(rep.backend).join(' → ')}` })),
        rep.ocrPages.length ? h('div', { class: 'notice', text: `OCR was used on page(s) ${rep.ocrPages.slice(0, 20).join(', ')}. Recognised text can contain mistakes.` }) : null,
        listBlock('Warnings', rep.warnings, 'warn'),
        h('div', { class: 'two-lists' }, listBlock('Kept', rep.kept, 'ok'), listBlock('Not carried over', rep.lost, 'lost')),
        verificationView(rep.verification));
    });
    replace(results, summary, ...cards);
  }

  function renderDocs(): void {
    const docs = set.docs();
    replace(docList, ...(docs.length ? docs.map((d) => docRow(ctx, d, { remove: false, actions: [
      button('↑', () => set.move(d.id, -1), 'btn-icon', { attrs: { 'aria-label': 'Move up' } }), button('↓', () => set.move(d.id, 1), 'btn-icon', { attrs: { 'aria-label': 'Move down' } }),
      button('✕', () => set.remove(d.id), 'btn-icon', { attrs: { 'aria-label': `Remove ${d.name} from this list` } })] })) : [emptyState('No documents yet', 'Drop files above, then choose a format.')]));
  }
  set.changed.on(renderDocs);

  const dz = dropzone({ title: 'Drop documents here', hint: 'PDF, Word, ODT, RTF, HTML, Markdown, text, SLDC', accept: DOCUMENT_ACCEPT, onFiles: (f) => { void ingest(ctx, f, { tray: dz.tray }).then((d) => set.add(...d)); } });

  const el = h('div', { class: 'workspace converter' },
    h('header', { class: 'ws-head' }, h('h1', { text: 'Converter' }), h('p', { class: 'muted', text: 'Convert between PDF, Word, OpenDocument, RTF, HTML, Markdown, text, XML and SLDC. Each result is re-opened and compared with the source before you can download it.' })),
    h('div', { class: 'two-col' },
      h('div', { class: 'col' }, dz.el, set.picker(), sectionTitle('Documents'), docList,
        h('div', { class: 'card options' }, field('Convert to:', target), limits, h('details', { class: 'more' }, h('summary', { text: 'Options' }), advanced), h('div', { class: 'row-actions' }, convertBtn)),
        prog.el),
      h('div', { class: 'col wide' }, results)));
  renderDocs();
  return { id: 'converter', title: 'Converter', el };
}
