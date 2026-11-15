// Mixed-format batch converter. Each file is an independent, cancellable, verified server job.
import { dropzone, docRow, DOCUMENT_ACCEPT, IMAGE_ACCEPT, downloadLink, emptyState, ingest, listBlock, sectionTitle, verificationView } from '../components.js';
import { badge, button, checkbox, errorMessage, field, h, replace, select, toast } from '../dom.js';
import { formatBytes, formatDuration, plural } from '../format.js';
import { WorkingSet } from '../workingset.js';
export function createConverter(ctx) {
    const set = new WorkingSet(ctx);
    const docList = h('div', { class: 'doc-list' });
    const results = h('div', { class: 'results' });
    const limits = h('div', { class: 'limits' });
    const advanced = h('div', { class: 'advanced' });
    const batchStatus = h('div', { class: 'muted small', attrs: { role: 'status', 'aria-live': 'polite' } });
    const folderInput = h('input', { class: 'input', attrs: { type: 'text', 'aria-label': 'Output folder on this computer' } });
    const opts = { target: 'pdf', engine: 'model', title: '', pageSize: '', fontSize: 11, pageNumbers: true, includeNotes: true, ocr: false, lineEnding: '\n', encrypt: '' };
    const assigned = new Map();
    const jobs = new Map();
    let formats = null;
    let mode = 'common';
    let concurrency = 2;
    let running = false;
    let cancelled = false;
    const chooseMode = select([{ value: 'common', label: 'All files to one format' }, { value: 'individual', label: 'Choose a format for each file' }], 'common', (v) => { if (running)
        return; mode = v === 'individual' ? 'individual' : 'common'; renderDocs(); });
    const target = select([{ value: 'pdf', label: 'PDF' }], 'pdf', (v) => { opts.target = v; showLimits(); syncOptions(); renderDocs(); });
    const convertBtn = button('Convert All', () => void run(), 'btn-primary');
    const cancelBtn = button('Cancel All', () => void cancelAll(), 'btn-small');
    const retryBtn = button('Retry Failed', () => void run(true), 'btn-small');
    const clearBtn = button('Clear Queue', () => { if (running)
        return; for (const doc of set.docs())
        set.remove(doc.id); jobs.clear(); assigned.clear(); renderDocs(); renderResults(); }, 'btn-small');
    function targetOptions(doc) {
        if (!formats)
            return [{ value: opts.target, label: opts.target.toUpperCase() }];
        return formats.targets.filter((t) => t.available && (t.id === doc.format ||
            formats.routes.some((r) => r.from === doc.format && r.to === t.id && (opts.engine === 'model' ? r.model.available : r.libreoffice?.available))))
            .map((t) => ({ value: t.id, label: t.label }));
    }
    function refreshCommonTargets() {
        if (!formats)
            return;
        const docs = set.docs();
        const old = opts.target;
        replace(target, ...formats.targets.map((t) => {
            const compatible = docs.filter((d) => valid(d, t.id)).length;
            const label = !t.available ? `${t.label} (unavailable)` : !docs.length ? t.label
                : `${t.label} (${compatible}/${docs.length} compatible)`;
            return h('option', { text: label, attrs: { value: t.id,
                    disabled: !t.available || (!!docs.length && compatible === 0) } });
        }));
        target.value = old;
    }
    function forDoc(doc) {
        if (mode === 'common')
            return opts.target;
        const choices = targetOptions(doc);
        const requested = assigned.get(doc.id);
        if (requested && choices.some((x) => x.value === requested))
            return requested;
        return choices.find((x) => x.value !== doc.format)?.value ?? choices[0]?.value ?? '';
    }
    function valid(doc, to) {
        return !!to && (doc.format === to || !!formats?.routes.find((r) => r.from === doc.format && r.to === to &&
            (opts.engine === 'model' ? r.model.available : r.libreoffice?.available)));
    }
    function showLimits() {
        const t = formats?.targets.find((x) => x.id === opts.target);
        replace(limits, t ? listBlock('What to expect from this format', t.limits) : null);
    }
    function syncOptions() {
        const eng = formats?.engines.libreoffice
            ? field('Conversion engine', select([{ value: 'model', label: 'Documaster (structure-preserving, checked)' }, { value: 'libreoffice', label: 'LibreOffice (preserves available layout)' }], opts.engine, (v) => { opts.engine = v; renderDocs(); }), 'Available conversion pairs depend on the engine.') : null;
        const paged = ['pdf', 'docx', 'odt', 'rtf'].includes(opts.target) || mode === 'individual';
        replace(advanced, h('div', { class: 'row' }, field('Title (optional)', h('input', { class: 'input', value: opts.title, attrs: { type: 'text', maxlength: 200 }, on: { input: (e) => { opts.title = e.target.value; } } }))), eng, paged ? h('div', { class: 'row' }, field('Page size', select([{ value: '', label: 'Automatic' }, { value: 'A4', label: 'A4' }, { value: 'Letter', label: 'US Letter' }], opts.pageSize, (v) => { opts.pageSize = v; })), field('Font size (pt)', h('input', { class: 'input short', value: String(opts.fontSize), attrs: { type: 'number', min: 6, max: 36, step: 0.5 },
            on: { input: (e) => { opts.fontSize = Number(e.target.value) || 11; } } }))) : null, paged ? checkbox('Page numbers', opts.pageNumbers, (v) => { opts.pageNumbers = v; }) : null, checkbox('Include footnotes and citations', opts.includeNotes, (v) => { opts.includeNotes = v; }), formats?.engines.ocr ? checkbox('Read scanned PDFs with OCR', opts.ocr, (v) => { opts.ocr = v; }) : null, field('SLDC password (optional)', h('input', { class: 'input', attrs: { type: 'password', autocomplete: 'new-password' },
            on: { input: (e) => { opts.encrypt = e.target.value; } } })), field('Text line endings', select([{ value: '\n', label: 'LF' }, { value: '\r\n', label: 'CRLF' }], opts.lineEnding, (v) => { opts.lineEnding = v; })));
    }
    void Promise.all([ctx.formats(), ctx.api.get('/api/settings')]).then(([f, s]) => {
        formats = f;
        folderInput.value = s.output_dir;
        refreshCommonTargets();
        showLimits();
        syncOptions();
        renderDocs();
    }).catch((e) => toast(errorMessage(e), 'error'));
    function active() {
        return [...jobs].filter(([id, j]) => set.docs().some((d) => d.id === id) && j.state === 'completed' && j.report)
            .map(([id, job]) => ({ id, job }));
    }
    async function run(failuresOnly = false, onlyId) {
        if (running)
            return;
        const docs = set.docs().filter((d) => !d.locked && (!onlyId || d.id === onlyId) && (!failuresOnly || ['failed', 'cancelled'].includes(jobs.get(d.id)?.state ?? '')));
        if (!docs.length) {
            toast(failuresOnly ? 'No failed files to retry' : 'Add at least one unlocked document');
            return;
        }
        cancelled = false;
        running = true;
        // Preserve successful jobs and downloads when a retry starts.
        for (const d of docs)
            jobs.set(d.id, { state: 'queued', progress: null, target: forDoc(d) });
        renderDocs();
        const options = {
            engine: opts.engine, title: opts.title || undefined, page_size: opts.pageSize || undefined,
            font_size_pt: opts.fontSize, page_numbers: opts.pageNumbers, include_notes: opts.includeNotes,
            ocr: opts.ocr, line_ending: opts.lineEnding, encrypt_password: opts.encrypt || undefined,
        };
        let index = 0;
        const worker = async () => {
            while (index < docs.length && !cancelled) {
                const doc = docs[index++];
                if (!doc)
                    break;
                const job = jobs.get(doc.id);
                if (!valid(doc, job.target)) {
                    job.state = 'skipped';
                    job.error = 'Unsupported conversion path; this file was skipped while compatible files continued';
                    renderDocs();
                    continue;
                }
                job.state = 'validating';
                renderDocs();
                try {
                    job.handle = await ctx.api.startJob('/api/convert', { docs: [doc.id], options: { ...options, target: job.target, retain_same_format: true } });
                    if (cancelled || jobs.get(doc.id)?.state === 'cancelled') {
                        await job.handle.cancel();
                        job.state = 'cancelled';
                        renderDocs();
                        continue;
                    }
                    job.state = 'converting';
                    const result = await job.handle.wait((j) => {
                        job.progress = j.state === 'running' && j.progress > 0 ? j.progress : null;
                        renderDocs();
                    });
                    const entry = result.results[0];
                    if (entry?.ok && entry.report) {
                        job.state = 'completed';
                        job.report = entry.report;
                    }
                    else {
                        job.state = 'failed';
                        job.error = entry?.error?.message ?? 'Conversion failed';
                    }
                }
                catch (e) {
                    job.state = e.cancelled ? 'cancelled' : 'failed';
                    job.error = errorMessage(e);
                }
                finally {
                    job.handle = undefined;
                    renderDocs();
                    renderResults();
                }
            }
        };
        try {
            await Promise.all(Array.from({ length: Math.min(concurrency, docs.length) }, () => worker()));
        }
        finally {
            running = false;
            renderDocs();
            renderResults();
        }
    }
    async function cancelAll() {
        cancelled = true;
        await Promise.all([...jobs.values()].filter((j) => j.handle).map((j) => j.handle.cancel().catch(() => undefined)));
        for (const j of jobs.values())
            if (j.state === 'queued')
                j.state = 'cancelled';
        renderDocs();
    }
    function renderStatus() {
        const ids = new Set(set.docs().map((d) => d.id));
        const values = [...jobs].filter(([id]) => ids.has(id)).map(([, job]) => job);
        const done = values.filter((j) => ['completed', 'failed', 'cancelled', 'skipped'].includes(j.state)).length;
        const activeJobs = values.filter((j) => j.state === 'converting');
        batchStatus.textContent = `${done} of ${values.length} finished${activeJobs.length ? ` · ${activeJobs.length} converting` : ''}`;
    }
    function renderDocs() {
        const docs = set.docs();
        refreshCommonTargets();
        replace(docList, ...(docs.length ? docs.map((d) => {
            const j = jobs.get(d.id);
            const chosen = forDoc(d);
            const choices = targetOptions(d);
            const selector = mode === 'individual' ? (() => {
                const choice = select(choices, chosen, (v) => {
                    if (running)
                        return;
                    assigned.set(d.id, v);
                    jobs.delete(d.id);
                    renderDocs();
                    renderResults();
                });
                choice.disabled = running || !choices.length;
                return field('Output', choice);
            })() : h('span', { class: 'muted small', text: `→ ${chosen.toUpperCase()}` });
            const status = j ? badge(j.state === 'completed' ? 'Completed' : j.state, j.state === 'completed' ? 'ok' : j.state === 'failed' ? 'bad' : 'neutral') : null;
            const progressText = j?.state === 'converting' ? (j.progress !== null ? `${Math.round(j.progress * 100)}%` : 'Working…') : '';
            return h('div', { class: 'convert-row' }, docRow(ctx, d, { remove: false, actions: [
                    button('↑', () => set.move(d.id, -1), 'btn-icon', { disabled: running }),
                    button('↓', () => set.move(d.id, 1), 'btn-icon', { disabled: running }),
                    button('✕', () => { jobs.delete(d.id); assigned.delete(d.id); set.remove(d.id); renderResults(); }, 'btn-icon', { disabled: running, attrs: { 'aria-label': `Remove ${d.name}` } })
                ] }), h('div', { class: 'convert-row-meta' }, selector, status, h('span', { class: 'muted small', text: progressText }), j?.handle ? button('Cancel file', () => { j.state = 'cancelled'; void j.handle?.cancel(); renderDocs(); }, 'btn-small') : null), j?.error ? h('div', { class: j.state === 'skipped' ? 'muted small' : 'error small', text: j.error }) : null, !j && formats && !valid(d, chosen) ? h('div', { class: 'notice warn small',
                text: 'No available conversion path for this source and selected target; this file will be skipped.' }) : null);
        }) : [emptyState('No documents yet', 'Drop several files above. Each can have a different source format.')]));
        convertBtn.disabled = running || !docs.length;
        cancelBtn.disabled = !running;
        retryBtn.disabled = running || ![...jobs.values()].some((j) => j.state === 'failed' || j.state === 'cancelled');
        clearBtn.disabled = running || !docs.length;
        target.disabled = running || mode === 'individual';
        chooseMode.disabled = running;
        renderStatus();
    }
    set.changed.on(renderDocs);
    function outputs() { return active().map(({ job }) => job.report.output); }
    function downloadable() {
        return outputs().flatMap((file) => [
            { name: file.name, url: ctx.api.downloadUrl(file.id) },
            ...(file.extras ?? []).map((name, i) => ({ name: name.split('/').pop() ?? name,
                url: `/api/outputs/${file.id}/extras/${i}/download` })),
        ]);
    }
    async function zip() {
        const files = outputs();
        if (!files.length)
            return;
        try {
            const archive = await ctx.api.post('/api/outputs/zip', { outputs: files.map((x) => x.id) });
            const a = downloadLink(ctx, archive);
            results.prepend(a);
            a.click();
        }
        catch (e) {
            toast(errorMessage(e), 'error');
        }
    }
    function downloaded(name) {
        // Anchor downloads must be initiated by a user gesture; browsers may ask for multi-download permission.
        const a = h('a', { attrs: { href: name } });
        document.body.appendChild(a);
        a.click();
        a.remove();
    }
    async function downloadAll() {
        const files = downloadable();
        if (!files.length)
            return;
        const picker = window.showDirectoryPicker;
        if (picker && window.isSecureContext) {
            try {
                const dir = await picker.call(window);
                const taken = new Set();
                for (const file of files) {
                    const res = await fetch(file.url);
                    if (!res.ok || !res.body)
                        throw new Error(`Cannot download ${file.name}`);
                    const original = file.name.replace(/[\\/:*?"<>|]/g, '_');
                    let filename = original;
                    for (let n = 1;; n++) {
                        if (!taken.has(filename)) {
                            try {
                                await dir.getFileHandle(filename);
                            }
                            catch (e) {
                                if (e.name === 'NotFoundError')
                                    break;
                                throw e;
                            }
                        }
                        const dot = original.lastIndexOf('.');
                        filename = `${original.slice(0, dot < 0 ? undefined : dot)} (${n})${dot < 0 ? '' : original.slice(dot)}`;
                    }
                    taken.add(filename);
                    const writable = await (await dir.getFileHandle(filename, { create: true })).createWritable();
                    try {
                        const reader = res.body.getReader();
                        for (;;) {
                            const { value, done } = await reader.read();
                            if (done)
                                break;
                            await writable.write(value);
                        }
                        await writable.close();
                    }
                    catch (e) {
                        await writable.abort();
                        throw e;
                    }
                }
                toast(`Saved ${plural(files.length, 'file')} to the selected folder`, 'ok');
                return;
            }
            catch (e) {
                if (e.name === 'AbortError')
                    return;
                toast(`Folder saving failed: ${errorMessage(e)}`, 'error');
                return;
            }
        }
        toast('Your browser controls the download folder and may ask you to allow multiple downloads.', 'info', 9000);
        for (const file of files) {
            downloaded(file.url);
            await new Promise((r) => window.setTimeout(r, 300));
        }
    }
    function renderResults() {
        const completed = active();
        const present = new Set(set.docs().map((d) => d.id));
        const failed = [...jobs].filter(([id, j]) => present.has(id) && j.state === 'failed');
        const skipped = [...jobs].filter(([id, j]) => present.has(id) && j.state === 'skipped');
        replace(results, sectionTitle('Results'), h('div', { class: 'summary-line' }, badge(`${completed.length} converted`, completed.length ? 'ok' : 'neutral'), failed.length ? badge(`${failed.length} failed`, 'bad') : null, skipped.length ? badge(`${skipped.length} skipped`, 'neutral') : null), completed.length ? h('div', { class: 'row-actions wrap' }, button('Download as ZIP', () => void zip(), 'btn-primary'), button(`Download All Separately (${downloadable().length})`, () => void downloadAll(), 'btn-small')) : null, ...completed.map(({ job }) => {
            const rep = job.report;
            return h('section', { class: 'card result' }, h('div', { class: 'card-head' }, h('h3', { text: `${rep.input.name} → ${rep.output.name}` }), badge(rep.verification.ok ? 'Checked' : 'Check failed', rep.verification.ok ? 'ok' : 'bad')), h('div', { class: 'row-actions wrap' }, downloadLink(ctx, rep.output), h('span', { class: 'muted small',
                text: `${rep.engine} · ${formatDuration(rep.durationMs)} · ${formatBytes(rep.output.bytes)}` })), rep.output.extras?.length ? h('div', { class: 'small' }, 'Supporting files: ', ...rep.output.extras.map((name, n) => h('a', { class: 'btn btn-small',
                text: name, attrs: { href: `/api/outputs/${rep.output.id}/extras/${n}/download`, download: name.split('/').pop() } }))) : null, listBlock('Warnings', rep.warnings, 'warn'), h('div', { class: 'two-lists' }, listBlock('Kept', rep.kept, 'ok'), listBlock('Not carried over', rep.lost, 'lost')), verificationView(rep.verification));
        }), ...failed.map(([id, j]) => h('section', { class: 'card result bad' }, h('h3', { text: set.docs().find((d) => d.id === id)?.name ?? 'File' }), h('div', { class: 'error', text: j.error ?? 'Conversion failed' }), button('Retry', () => { void retryOne(id); }, 'btn-small'))), ...skipped.map(([id, j]) => h('section', { class: 'card result' }, h('h3', { text: set.docs().find((d) => d.id === id)?.name ?? 'File' }), h('div', { class: 'muted', text: j.error ?? 'Conversion not supported' }))));
    }
    async function retryOne(id) {
        if (running)
            return;
        await run(true, id);
    }
    const dz = dropzone({ title: 'Drop files here', hint: 'Select multiple PDF, Word, OpenDocument, text or SLDC files',
        accept: `${DOCUMENT_ACCEPT},${IMAGE_ACCEPT}`, onFiles: (files) => { void ingest(ctx, files, { images: true, tray: dz.tray }).then((docs) => set.add(...docs)); } });
    const el = h('div', { class: 'workspace converter' }, h('header', { class: 'ws-head' }, h('h1', { text: 'Converter' }), h('p', { class: 'muted', text: 'Convert mixed document formats together, or choose a target for each file. Results are independently verified.' })), h('div', { class: 'two-col' }, h('div', { class: 'col' }, dz.el, set.picker(), sectionTitle('Conversion queue', clearBtn), docList, h('div', { class: 'card options' }, field('Conversion mode', chooseMode), field('Common output format', target), limits, field('Parallel conversions', select([{ value: '1', label: '1 (lowest resource use)' },
        { value: '2', label: '2 (default)' }, { value: '3', label: '3 (faster on capable systems)' }], '2', (v) => { if (!running)
        concurrency = Number(v); })), h('details', { class: 'more' }, h('summary', { text: 'Options' }), advanced), h('div', { class: 'row-actions wrap' }, convertBtn, cancelBtn, retryBtn), batchStatus), h('div', { class: 'card options' }, field('Local output folder', folderInput, 'Outputs are also stored here by the local Documaster service. Choose a folder path on this computer.'), button('Set output folder', () => {
        void ctx.api.put('/api/settings', { output_dir: folderInput.value })
            .then(() => toast('Output folder updated', 'ok')).catch((e) => toast(errorMessage(e), 'error'));
    }, 'btn-small'))), h('div', { class: 'col wide' }, results)));
    syncOptions();
    renderDocs();
    renderResults();
    return { id: 'converter', title: 'Converter', el };
}
//# sourceMappingURL=converter.js.map