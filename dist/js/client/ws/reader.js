// Reader & Analysis: what a document contains and how it is built, with every finding pointing at where it comes from.
import { dropzone, docRow, DOCUMENT_ACCEPT, emptyState, ingest, listBlock, progress, sectionTitle } from '../components.js';
import { badge, button, checkbox, errorMessage, field, h, replace, select, toast } from '../dom.js';
import { formatNumber, plural } from '../format.js';
import { WorkingSet } from '../workingset.js';
const PRESETS = ['report', 'proposal', 'contract', 'research paper'];
export function createReader(ctx) {
    const set = new WorkingSet(ctx, (d) => !d.locked);
    const prog = progress();
    const docList = h('div', { class: 'doc-list' });
    const agentBox = h('div', { class: 'card' });
    const content = h('div', { class: 'reader-content' });
    const tabs = h('div', { class: 'tabs', attrs: { role: 'tablist' } });
    let currentId = null;
    let analysis = null;
    let tab = 'overview';
    let preset = 'report';
    let custom = '';
    let summaryCache = null;
    let compareWith = '';
    let compare = null;
    let searchResult = null;
    let query = { text: '', regex: false, caseSensitive: false };
    const current = () => (currentId ? ctx.docs.get(currentId) : undefined);
    /** A page reference that opens the PDF editor at that page for PDFs, and is plain text otherwise. */
    function ref(ev, label) {
        const doc = ctx.docs.get(ev.docId);
        const text = label ?? (ev.page ? `p. ${ev.page}` : `part ${ev.part}`);
        if (doc?.format === 'pdf' && ev.page)
            return h('button', { class: 'link-btn', text, title: `Open page ${ev.page} in the PDF Editor`, attrs: { type: 'button' }, on: { click: () => ctx.open('editor', ev.docId, ev.page ?? 1) } });
        return h('span', { class: 'muted', text });
    }
    async function analyse() {
        const d = current();
        if (!d || prog.busy)
            return;
        summaryCache = null;
        try {
            analysis = await prog.track(`Reading ${d.name}`, ctx.api.post('/api/analysis/run', { doc: d.id, checks: ['outline', 'structure', 'keywords', 'readability', 'pii', 'duplicates', 'sections'], preset, required: custom.trim() ? custom.split(',').map((x) => x.trim()).filter(Boolean) : undefined }));
        }
        catch (e) {
            toast(errorMessage(e), 'error');
            analysis = null;
        }
        renderAgent();
        renderContent();
    }
    async function loadAgent() {
        const d = current();
        if (!d)
            return null;
        try {
            return await ctx.api.get(`/api/documents/${d.id}/agents`);
        }
        catch {
            return null;
        }
    }
    function renderAgent() {
        const d = current();
        if (!d) {
            replace(agentBox);
            return;
        }
        void loadAgent().then((st) => {
            if (!st)
                return;
            replace(agentBox, h('h4', { text: 'SLAI agents' }), checkbox('Let SLAI agents read this document', st.consent, async (v) => {
                try {
                    const r = await ctx.api.put(`/api/documents/${d.id}/agents`, { allow: v });
                    if (v && r.privacy)
                        toast(r.privacy.status === 'used' ? 'Consent recorded with the Privacy agent' : `Consent kept in this session only (${r.privacy.reason ?? 'Privacy agent not available'})`, 'info', 7000);
                    summaryCache = null;
                    renderAgent();
                }
                catch (e) {
                    toast(errorMessage(e), 'error');
                }
            }), h('p', { class: 'muted small', text: st.explanation }), h('div', { class: 'muted small', text: `Agent mode: ${st.mode}` }));
        });
    }
    // ---- tabs -------------------------------------------------------------------------------------------------------------------
    const TABS = [['overview', 'Overview'], ['structure', 'Structure'], ['text', 'Words & readability'], ['summary', 'Summary'], ['pii', 'Personal data'], ['sections', 'Sections'], ['compare', 'Compare'], ['search', 'Search']];
    for (const [id, label] of TABS)
        tabs.appendChild(h('button', { class: 'tab', text: label, data: { tab: id }, attrs: { type: 'button', role: 'tab' }, on: { click: () => { tab = id; renderContent(); } } }));
    function evidenceList(items, max = 8) {
        return h('div', { class: 'evidence' }, ...items.slice(0, max).map((e) => h('div', { class: 'ev' }, ref(e), e.quote ? h('span', { class: 'quote', text: `“${e.quote}”` }) : null)));
    }
    function outlineView(items, depth = 0) {
        return items.flatMap((n) => [h('div', { class: 'outline-row', style: { paddingLeft: `${depth * 16}px` } }, h('span', { text: n.title }), n.page ? ref({ docId: currentId ?? '', doc: '', blockId: n.blockId, page: n.page, part: n.part, start: 0, end: 0 }) : null), ...outlineView(n.children, depth + 1)]);
    }
    async function ensureSummary(force = false) {
        const d = current();
        if (!d || (summaryCache && !force))
            return;
        try {
            summaryCache = await ctx.api.post('/api/analysis/summary', { doc: d.id, sentences: 6 });
        }
        catch (e) {
            toast(errorMessage(e), 'error');
        }
    }
    function noteView(n) {
        const kind = n.status === 'used' ? 'ok' : n.status === 'failed' ? 'bad' : 'neutral';
        return h('div', { class: 'agent-note' }, badge(`${n.role}: ${n.status}`, kind), n.reason ? h('span', { class: 'muted small', text: ` ${n.reason}` }) : null, n.status === 'used' && n.role === 'quality' && n.output ? h('div', { class: 'muted small', text: `The Quality agent answered: ${JSON.stringify(n.output).slice(0, 400)}` }) : null);
    }
    function renderContent() {
        for (const b of tabs.querySelectorAll('button')) {
            const on = b.dataset['tab'] === tab;
            b.classList.toggle('active', on);
            b.setAttribute('aria-selected', String(on));
        }
        const d = current();
        if (tab === 'compare') {
            renderCompare();
            return;
        }
        if (tab === 'search') {
            renderSearch();
            return;
        }
        if (!d) {
            replace(content, emptyState('Add a document', 'Drop a file to see its structure, language, keywords, readability and more.'));
            return;
        }
        if (!analysis) {
            replace(content, h('div', { class: 'row-actions' }, button('Analyse', () => void analyse(), 'btn-primary')));
            return;
        }
        const a = analysis;
        const head = h('div', { class: 'summary-line' }, badge(a.document.formatLabel, 'info'), a.language.code !== 'und' ? badge(`Language: ${a.language.code} (${Math.round(a.language.confidence * 100)}%)`, 'neutral', a.language.method) : badge('Language not determined', 'neutral'), h('span', { class: 'muted small', text: `read with ${a.backend}` }));
        const warn = listBlock('Reading notes', a.warnings, 'warn');
        if (tab === 'overview') {
            const f = a.structure ?? [];
            replace(content, head, warn, h('div', { class: 'stat-grid' }, ...[['Headings', countOutline(a.outline ?? [])], ['Structure findings', f.length], ['Personal data items', a.pii?.total ?? 0], ['Repeated paragraphs', a.duplicates?.total ?? 0]].map(([l, v]) => h('div', { class: 'stat' }, h('div', { class: 'stat-value', text: formatNumber(Number(v)) }), h('div', { class: 'stat-label', text: String(l) })))), sectionTitle('Outline'), a.outline?.length ? h('div', { class: 'outline' }, ...outlineView(a.outline)) : h('div', { class: 'muted', text: 'This document has no headings.' }));
        }
        else if (tab === 'structure') {
            replace(content, head, warn, a.agents?.length ? h('div', { class: 'agent-notes' }, ...a.agents.map(noteView)) : null, (a.structure ?? []).length ? h('div', {}, ...(a.structure ?? []).map((f) => h('section', { class: `card finding ${f.severity}` }, h('div', { class: 'card-head' }, badge(f.severity, f.severity === 'problem' ? 'bad' : f.severity === 'warning' ? 'warn' : 'info'), h('span', { text: f.message })), f.evidence.length ? evidenceList(f.evidence, 5) : null)))
                : h('div', { class: 'notice ok', text: 'No structure problems found.' }));
        }
        else if (tab === 'text') {
            const r = a.readability ?? {};
            replace(content, head, warn, r['available'] === false ? h('div', { class: 'muted', text: String(r['reason'] ?? 'Not available') }) : h('div', {}, h('div', { class: 'stat-grid' }, ...[['Words per sentence', r['avgWordsPerSentence']], ['LIX', r['lix'] !== undefined ? `${r['lix']} (${r['lixLevel']})` : '–'], ['Flesch reading ease', r['fleschReadingEase'] !== undefined ? `${r['fleschReadingEase']} (${r['fleschLevel']})` : 'English only'], ['Grade level', r['fleschKincaidGrade'] ?? 'English only']]
                .map(([l, v]) => h('div', { class: 'stat' }, h('div', { class: 'stat-value', text: String(v ?? '–') }), h('div', { class: 'stat-label', text: String(l) })))), sectionTitle('Main terms'), h('div', { class: 'chips' }, ...(a.keywords ?? []).map((k) => h('span', { class: 'chip', title: k.evidence[0] ? `First at ${k.evidence[0].page ? `p. ${k.evidence[0].page}` : `part ${k.evidence[0].part}`}` : '' }, k.term, h('b', { text: String(k.count) })))), a.duplicates?.groups.length ? h('div', {}, sectionTitle('Repeated text'), ...a.duplicates.groups.slice(0, 10).map((g) => h('div', { class: 'card finding' }, badge(`${g.kind} ×${g.count}`, 'warn'), h('div', { class: 'quote', text: `“${g.text.slice(0, 200)}”` }), evidenceList(g.evidence, 4)))) : null));
        }
        else if (tab === 'summary') {
            replace(content, head, h('div', { class: 'muted', text: 'Loading…' }));
            void ensureSummary().then(() => {
                const s = summaryCache;
                if (!s) {
                    replace(content, head, h('div', { class: 'error', text: 'The summary could not be made.' }));
                    return;
                }
                const machine = s.text.split('\n\nMachine-written summary')[1];
                replace(content, head, h('div', { class: 'notice', text: s.summary.note ?? 'Sentences are quoted from the document.' }), ...s.summary.sentences.map((x) => h('blockquote', { class: 'quote-card' }, x.text, h('div', {}, ref(x.evidence)))), machine ? h('section', { class: 'card' }, h('h4', { text: 'Machine-written summary' }), badge('Written by the Language agent - check it against the quoted sentences', 'warn'), h('p', { text: machine.replace(/^[^\n]*\n/, '').trim() })) : null, h('div', { class: 'agent-notes' }, ...s.agents.map(noteView)), button('Refresh', () => { summaryCache = null; void ensureSummary(true).then(renderContent); }, 'btn-small'));
            });
        }
        else if (tab === 'pii') {
            const p = a.pii;
            replace(content, head, warn, !p || !p.total ? h('div', { class: 'notice ok', text: `No email addresses, phone numbers, IBANs or card numbers found. ${p?.note ?? ''}` }) : h('div', {}, h('div', { class: 'chips' }, ...Object.entries(p.counts).map(([k, n]) => h('span', { class: 'chip' }, k, h('b', { text: String(n) })))), h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ...['Type', 'Found', 'Where', 'Check'].map((t) => h('th', { text: t })))), h('tbody', {}, ...p.items.slice(0, 200).map((i) => h('tr', {}, h('td', { text: i.label }), h('td', { class: 'mono', text: i.text }), h('td', {}, ref(i.evidence)), h('td', { class: 'muted small', text: i.confidence }))))), h('p', { class: 'muted small', text: p.note }), d?.format === 'pdf' ? button('Redact these in the PDF Editor…', () => ctx.open('editor', d.id), 'btn-small btn-danger') : h('div', { class: 'muted small', text: 'Convert the document to PDF to redact it.' })));
        }
        else if (tab === 'sections') {
            const s = a.sections;
            replace(content, head, h('div', { class: 'card options' }, field('Document type', select(PRESETS.map((p) => ({ value: p, label: p })), preset, (v) => { preset = v; })), field('Or list the sections you need (comma separated)', h('input', { class: 'input', value: custom, attrs: { type: 'text', placeholder: 'Introduction, Scope, Budget' }, on: { input: (e) => { custom = e.target.value; } } })), button('Check sections', () => void analyse(), 'btn-small')), s ? h('div', {}, h('div', { class: `notice ${s.complete ? 'ok' : 'warn'}`, text: s.complete ? 'All expected sections are present.' : `Missing: ${s.missing.join(', ')}` }), h('ul', { class: 'check-list' }, ...s.items.map((i) => h('li', { class: i.found ? 'ok' : 'bad' }, `${i.found ? '✓' : '✗'} ${i.required}`, i.found && i.evidence[0] ? h('span', {}, ' — ', ref(i.evidence[0]), i.match ? ` (${i.match} match)` : '') : null))), h('p', { class: 'muted small', text: s.note })) : null);
        }
    }
    const countOutline = (o) => o.reduce((n, x) => n + 1 + countOutline(x.children), 0);
    // ---- compare / search --------------------------------------------------------------------------------------------------------------
    function renderCompare() {
        const docs = ctx.docs.list().filter((x) => !x.locked);
        const d = current();
        const other = select([{ value: '', label: 'Choose a document to compare with…' }, ...docs.filter((x) => x.id !== currentId).map((x) => ({ value: x.id, label: x.name }))], compareWith, (v) => { compareWith = v; });
        const run = button('Compare', async () => {
            if (!d || !compareWith) {
                toast('Choose two documents');
                return;
            }
            try {
                compare = await ctx.api.post('/api/analysis/compare', { a: d.id, b: compareWith });
            }
            catch (e) {
                toast(errorMessage(e), 'error');
                return;
            }
            renderCompare();
        }, 'btn-primary');
        const c = compare;
        replace(content, h('div', { class: 'card options' }, h('div', { class: 'muted small', text: d ? `Compare ${d.name} (before) with:` : 'Pick a document on the left first.' }), other, run), c ? h('div', {}, h('div', { class: 'stat-grid' }, ...[['Added', c.summary['added']], ['Removed', c.summary['removed']], ['Changed', c.summary['modified']], ['Unchanged', c.summary['unchanged']], ['Word similarity', `${Math.round(c.wordSimilarity * 100)}%`]].map(([l, v]) => h('div', { class: 'stat' }, h('div', { class: 'stat-value', text: String(v ?? 0) }), h('div', { class: 'stat-label', text: String(l) })))), h('div', { class: 'muted small', text: `Method: ${c.method}` }), c.modified.length ? h('div', {}, sectionTitle('Changed'), ...c.modified.slice(0, 40).map((m) => h('div', { class: 'diff' }, h('div', { class: 'del' }, ref(m.before), ' ', m.before.quote ?? ''), h('div', { class: 'ins' }, ref(m.after), ' ', m.after.quote ?? '')))) : null, c.added.length ? h('div', {}, sectionTitle(`Only in ${c.b}`), ...c.added.slice(0, 40).map((e) => h('div', { class: 'diff' }, h('div', { class: 'ins' }, ref(e), ' ', e.quote ?? '')))) : null, c.removed.length ? h('div', {}, sectionTitle(`Only in ${c.a}`), ...c.removed.slice(0, 40).map((e) => h('div', { class: 'diff' }, h('div', { class: 'del' }, ref(e), ' ', e.quote ?? '')))) : null) : null);
    }
    function renderSearch() {
        const docs = set.docs();
        const input = h('input', { class: 'input', value: query.text, attrs: { type: 'search', placeholder: 'Search the open documents', 'aria-label': 'Search' }, on: { input: (e) => { query.text = e.target.value; }, keydown: (e) => { if (e.key === 'Enter')
                    void doSearch(); } } });
        const r = searchResult;
        replace(content, h('div', { class: 'card options' }, input, h('div', { class: 'row-actions wrap' }, checkbox('Pattern (regex)', query.regex, (v) => { query.regex = v; }), checkbox('Match case', query.caseSensitive, (v) => { query.caseSensitive = v; }), button('Search', () => void doSearch(), 'btn-primary btn-small')), h('div', { class: 'muted small', text: docs.length ? `Searching ${plural(docs.length, 'document')}.` : 'Add documents on the left.' })), r ? h('div', {}, h('div', { class: 'muted', text: `${plural(r.total, 'match', 'matches')} for “${r.query}”` }), ...r.results.filter((x) => x.count).map((x) => h('section', { class: 'card' }, h('h4', { text: `${x.name} (${x.count})` }), ...x.hits.slice(0, 30).map((hit) => h('div', { class: 'ev' }, ref(hit.evidence), h('span', { class: 'quote', text: `…${hit.context}…` })))))) : null);
    }
    async function doSearch() {
        const docs = set.docs();
        if (!query.text || !docs.length)
            return;
        try {
            searchResult = await ctx.api.post('/api/analysis/search', { docs: docs.map((d) => d.id), query: query.text, regex: query.regex, case: query.caseSensitive });
        }
        catch (e) {
            toast(errorMessage(e), 'error');
            return;
        }
        renderSearch();
    }
    // ---- documents -------------------------------------------------------------------------------------------------------------------------
    function renderDocs() {
        const docs = set.docs();
        if (docs.length && (!currentId || !set.has(currentId))) {
            currentId = docs[0].id;
            analysis = null;
            summaryCache = null;
            void analyse();
        }
        if (!docs.length) {
            currentId = null;
            analysis = null;
        }
        replace(docList, ...(docs.length ? docs.map((d) => docRow(ctx, d, { remove: false, selected: d.id === currentId, onClick: () => { if (currentId !== d.id) {
                currentId = d.id;
                analysis = null;
                summaryCache = null;
                compare = null;
                renderDocs();
                void analyse();
            } },
            actions: [button('✕', () => set.remove(d.id), 'btn-icon', { attrs: { 'aria-label': `Remove ${d.name} from this list` } })] })) : [emptyState('No documents yet', 'Drop a file to analyse it.')]));
        renderAgent();
        renderContent();
    }
    set.changed.on(renderDocs);
    const dz = dropzone({ title: 'Drop documents here', hint: 'PDF, Word, ODT, RTF, HTML, Markdown, text, SLDC', accept: DOCUMENT_ACCEPT, onFiles: (f) => { void ingest(ctx, f, { tray: dz.tray }).then((d) => set.add(...d)); } });
    const el = h('div', { class: 'workspace reader' }, h('header', { class: 'ws-head' }, h('h1', { text: 'Reader & Analysis' }), h('p', { class: 'muted', text: 'See how a document is built and what it contains. Everything here is computed from the document; nothing is guessed, and each finding points at its page.' })), h('div', { class: 'two-col' }, h('div', { class: 'col' }, dz.el, set.picker(), sectionTitle('Documents'), docList, agentBox, prog.el), h('div', { class: 'col wide' }, tabs, content)));
    renderDocs();
    return { id: 'reader', title: 'Reader & Analysis', el, activate: (arg) => { if (arg?.docId) {
            const d = ctx.docs.get(arg.docId);
            if (d) {
                set.add(d);
                currentId = d.id;
                analysis = null;
                renderDocs();
                void analyse();
            }
        } } };
}
//# sourceMappingURL=reader.js.map