// Word Counter: real counts for pasted text and documents (computed on the server by the same rules for both).
import { dropzone, docRow, DOCUMENT_ACCEPT, emptyState, ingest, listBlock, progress, sectionTitle } from '../components.js';
import { badge, button, checkbox, debounce, errorMessage, field, h, replace, toast } from '../dom.js';
import { formatMinutes, formatNumber, plural } from '../format.js';
import { WorkingSet } from '../workingset.js';
export function statGrid(s) {
    const cell = (label, value) => h('div', { class: 'stat' }, h('div', { class: 'stat-value', text: value }), h('div', { class: 'stat-label', text: label }));
    return h('div', { class: 'stat-grid' }, cell('Words', formatNumber(s.words)), cell('Characters', formatNumber(s.characters)), cell('Without spaces', formatNumber(s.charactersNoSpaces)), cell('Sentences', formatNumber(s.sentences)), cell('Paragraphs', formatNumber(s.paragraphs)), cell('Unique words', formatNumber(s.uniqueWords)), cell('Reading time', formatMinutes(s.readingMinutes)), cell('Speaking time', formatMinutes(s.speakingMinutes)), cell('Avg. word length', String(s.avgWordLength)));
}
export function statsView(s, title, extra = []) {
    const lang = s.language.code === 'und' ? 'Language not determined' : `Language: ${s.language.code} (${Math.round(s.language.confidence * 100)}% confidence, ${s.language.method})`;
    const card = h('section', { class: 'card result' }, h('div', { class: 'card-head' }, h('h3', { text: title }), h('span', { class: 'muted', text: lang }), ...extra), statGrid(s));
    if (s.goals?.length) {
        card.appendChild(h('div', { class: 'goals' }, ...s.goals.map((g) => h('div', { class: `goal ${g.met ? 'met' : 'unmet'}` }, badge(g.met ? 'Goal met' : 'Goal not met', g.met ? 'ok' : 'warn'), ` ${g.goal.replace(/([A-Z])/g, ' $1').toLowerCase()}: ${formatNumber(g.value)} (limit ${formatNumber(g.limit)})`))));
    }
    const notes = s.notes?.length ? listBlock('Notes', s.notes, 'notes') : null;
    if (notes)
        card.appendChild(notes);
    if (s.topTerms.length) {
        card.appendChild(h('div', { class: 'terms' }, h('div', { class: 'listblock-title', text: 'Most used words' }), h('div', { class: 'chips' }, ...s.topTerms.slice(0, 15).map((t) => h('span', { class: 'chip', title: `${t.count} times` }, t.term, h('b', { text: String(t.count) }))))));
    }
    if (s.breakdown && s.breakdown.length > 1) {
        const kind = s.breakdown[0]?.kind === 'page' ? 'Page' : 'Part';
        card.appendChild(h('details', { class: 'breakdown' }, h('summary', { text: `${kind} by ${kind.toLowerCase()} (${s.breakdown.length})` }), h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ...[kind, 'Starts with', 'Words', 'Characters', 'Sentences'].map((t) => h('th', { text: t })))), h('tbody', {}, ...s.breakdown.map((b) => h('tr', {}, h('td', { text: String(b.index) }), h('td', { class: 'clip', text: b.label }), h('td', { class: 'num', text: formatNumber(b.words) }), h('td', { class: 'num', text: formatNumber(b.characters) }), h('td', { class: 'num', text: formatNumber(b.sentences) })))))));
    }
    if (s.headings?.length) {
        card.appendChild(h('details', { class: 'breakdown' }, h('summary', { text: `Headings (${s.headings.length})` }), h('ul', { class: 'heading-list' }, ...s.headings.slice(0, 100).map((x) => h('li', { style: { paddingLeft: `${(x.level - 1) * 14}px` }, text: x.text })))));
    }
    const bits = [];
    if (s.tables)
        bits.push(plural(s.tables, 'table'));
    if (s.images)
        bits.push(plural(s.images, 'image'));
    if (s.document?.backend)
        bits.push(`read with ${s.document.backend}`);
    if (bits.length)
        card.appendChild(h('div', { class: 'muted small', text: bits.join(' · ') }));
    return card;
}
export function createCounter(ctx) {
    const set = new WorkingSet(ctx);
    const results = h('div', { class: 'results' });
    const docList = h('div', { class: 'doc-list' });
    const prog = progress();
    const textArea = h('textarea', { class: 'input paste', attrs: { rows: 6, placeholder: 'Or paste or type text here…', 'aria-label': 'Text to count' } });
    const textResult = h('div', { class: 'results' });
    const minWords = h('input', { class: 'input short', attrs: { type: 'number', min: 0, placeholder: 'e.g. 500' } });
    const maxWords = h('input', { class: 'input short', attrs: { type: 'number', min: 0, placeholder: 'e.g. 1000' } });
    let ocr = false;
    const ocrBox = checkbox('Read scanned pages with OCR', false, (v) => { ocr = v; }, 'Pages without a text layer count 0 words otherwise');
    ocrBox.hidden = true;
    void ctx.api.get('/api/capabilities').then((c) => { ocrBox.hidden = !c.tools.tesseract; }).catch(() => undefined);
    const goals = () => {
        const g = {};
        if (minWords.value)
            g['minWords'] = Number(minWords.value);
        if (maxWords.value)
            g['maxWords'] = Number(maxWords.value);
        return Object.keys(g).length ? g : undefined;
    };
    async function countText() {
        const text = textArea.value;
        if (!text.trim()) {
            replace(textResult);
            return;
        }
        try {
            const s = await ctx.api.post('/api/count/text', { text, goals: goals() });
            replace(textResult, statsView(s, 'Your text'));
        }
        catch (e) {
            replace(textResult, h('div', { class: 'error', text: errorMessage(e) }));
        }
    }
    textArea.addEventListener('input', debounce(() => void countText(), 300));
    async function countDocs() {
        const docs = set.docs().filter((d) => !d.locked);
        if (!docs.length || prog.busy)
            return;
        try {
            const r = await prog.run(`Counting ${plural(docs.length, 'document')}`, () => ctx.api.startJob('/api/count/documents', { docs: docs.map((d) => d.id), goals: goals(), ocr }));
            renderResults(r);
        }
        catch (e) {
            if (e.cancelled)
                toast('Counting cancelled');
            else
                toast(errorMessage(e), 'error');
        }
    }
    function renderResults(r) {
        const byId = new Map(set.docs().map((d) => [d.id, d]));
        const cards = [];
        if (r.totals) {
            const t = r.totals;
            cards.push(h('section', { class: 'card result totals' }, h('div', { class: 'card-head' }, h('h3', { text: `Total of ${r.results.filter((x) => x.ok).length} documents` })), h('div', { class: 'stat-grid' }, ...[['Words', t['words']], ['Characters', t['characters']], ['Sentences', t['sentences']], ['Paragraphs', t['paragraphs']]]
                .map(([l, v]) => h('div', { class: 'stat' }, h('div', { class: 'stat-value', text: formatNumber(Number(v ?? 0)) }), h('div', { class: 'stat-label', text: String(l) }))))));
        }
        for (const item of r.results) {
            const name = byId.get(item.id)?.name ?? item.name ?? 'Document';
            if (item.ok && item.stats)
                cards.push(statsView(item.stats, name));
            else
                cards.push(h('section', { class: 'card result' }, h('h3', { text: name }), h('div', { class: 'error', text: item.error?.message ?? 'Could not be counted' })));
        }
        replace(results, ...cards);
    }
    function renderDocs() {
        const docs = set.docs();
        replace(docList, ...(docs.length ? docs.map((d) => docRow(ctx, d, { remove: false, actions: [button('✕', () => { set.remove(d.id); replace(results); }, 'btn-icon', { attrs: { 'aria-label': `Remove ${d.name} from this list` } })] })) : [emptyState('No documents yet', 'Drop files above to count their words.')]));
    }
    set.changed.on(renderDocs);
    const dz = dropzone({
        title: 'Drop documents here', hint: 'PDF, Word, ODT, RTF, HTML, Markdown, text, SLDC', accept: DOCUMENT_ACCEPT,
        onFiles: (files) => { void ingest(ctx, files, { tray: dz.tray }).then((docs) => { set.add(...docs); void countDocs(); }); },
    });
    const el = h('div', { class: 'workspace counter' }, h('header', { class: 'ws-head' }, h('h1', { text: 'Word Counter' }), h('p', { class: 'muted', text: 'Counts for pasted text and documents - words, characters, sentences, paragraphs, reading time, language and the words you use most.' })), h('div', { class: 'two-col' }, h('div', { class: 'col' }, dz.el, set.picker(), sectionTitle('Documents'), docList, h('div', { class: 'card options' }, h('h4', { text: 'Goals (optional)' }), h('div', { class: 'row' }, field('At least (words)', minWords), field('At most (words)', maxWords)), ocrBox, button('Count words', () => { void countDocs(); void countText(); }, 'btn-primary')), prog.el, h('div', { class: 'card' }, h('h4', { text: 'Paste text' }), textArea)), h('div', { class: 'col wide' }, textResult, results)));
    renderDocs();
    return { id: 'counter', title: 'Word Counter', el };
}
//# sourceMappingURL=counter.js.map