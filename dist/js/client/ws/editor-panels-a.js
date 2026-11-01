// Right-hand panels of the PDF editor, part 1: pages, annotations, stamps, and find.
import { downloadLink, listBlock, verificationView } from '../components.js';
import { badge, button, checkbox, errorMessage, field, h, promptText, replace, select, toast } from '../dom.js';
import { parseRanges, plural } from '../format.js';
function toolButton(env, tool, label, title) {
    const b = button(label, () => env.core.setTool(env.core.tool === tool ? 'view' : tool), 'btn-tool', { title, attrs: { 'aria-pressed': 'false', 'data-tool': tool } });
    return b;
}
function syncTools(root, env) {
    for (const b of root.querySelectorAll('button[data-tool]')) {
        const on = b.dataset['tool'] === env.core.tool;
        b.classList.toggle('active', on);
        b.setAttribute('aria-pressed', String(on));
    }
}
const needs = (env, perm, what) => env.core.can(perm) ? null : h('div', { class: 'notice warn', text: `This PDF's permissions do not allow ${what}. Open it with the owner password to continue.` });
/** A notice that follows the document: the panel is built before a PDF is open, so it is refreshed on every change. */
function noticeSlot(env, perm, what) {
    const el = h('div', { class: 'notice-slot' });
    const sync = () => replace(el, needs(env, perm, what));
    env.core.changed.on((k) => { if (k === 'doc')
        sync(); });
    sync();
    return { el, sync };
}
// ============================================================================================ pages
export function pagesPanel(env) {
    const { core, org } = env;
    const root = h('div', { class: 'panel-body' });
    const rangeInput = h('input', { class: 'input', attrs: { type: 'text', placeholder: '1-3, 7', 'aria-label': 'Pages' } });
    const insertDoc = h('select', { class: 'input' });
    const insertPages = h('input', { class: 'input', attrs: { type: 'text', placeholder: 'all pages, or 1-3', 'aria-label': 'Pages to insert' } });
    const insertAfter = h('input', { class: 'input short', attrs: { type: 'number', min: 0, value: '0' } });
    const exportOut = h('div', {});
    const act = (label, title, fn, danger = false) => button(label, () => { env.ensureSelection(); fn(); }, `btn-small${danger ? ' btn-danger' : ''}`, { title, attrs: { 'aria-label': title } });
    async function exportPages(split) {
        const d = core.doc;
        if (!d)
            return;
        const sel = org.selectedItems().filter((i) => !i.blank).map((i) => i.page + 1);
        const pages = sel.length ? sel.join(',') : 'all';
        try {
            const r = await core.progress.run('Exporting pages', () => core.ctx.api.startJob(`/api/documents/${d.id}/export`, { pages, splitEach: split }));
            replace(exportOut, h('div', { class: 'row-actions wrap' }, ...r.outputs.slice(0, 20).map((o) => downloadLink(core.ctx, o))), r.outputs.length > 20 ? h('div', { class: 'muted small', text: `${r.outputs.length - 20} more files are in your output folder` }) : null);
        }
        catch (e) {
            toast(errorMessage(e), 'error');
        }
    }
    async function insertFrom() {
        const d = core.doc;
        if (!d || !insertDoc.value)
            return;
        const spec = { op: 'insert', from: insertDoc.value, after: Number(insertAfter.value) || 0 };
        if (insertPages.value.trim())
            spec['pages'] = insertPages.value.trim();
        await core.run(spec, 'Inserting pages');
    }
    function render() {
        const others = core.ctx.docs.pdfs().filter((d) => d.id !== core.doc?.id);
        replace(insertDoc, h('option', { text: others.length ? 'Choose a PDF…' : 'Open another PDF to insert from it', attrs: { value: '' } }), ...others.map((d) => h('option', { text: d.name, attrs: { value: d.id } })));
        const n = org.selected.size;
        replace(root, needs(env, 'assemble', 'rearranging pages'), h('p', { class: 'muted small', text: n ? `${plural(n, 'page')} selected in the strip on the left.` : 'Select pages in the strip on the left (Ctrl/Shift-click for several). With nothing selected, actions apply to the page you are viewing.' }), h('div', { class: 'btn-grid' }, act('⟲ Left', 'Rotate left', () => void env.pageOp('Rotating pages', () => org.rotateSelected(-90))), act('⟳ Right', 'Rotate right', () => void env.pageOp('Rotating pages', () => org.rotateSelected(90))), act('↑ Earlier', 'Move earlier', () => void env.pageOp('Moving pages', () => org.moveUp())), act('↓ Later', 'Move later', () => void env.pageOp('Moving pages', () => org.moveDown())), act('⧉ Duplicate', 'Duplicate', () => void env.pageOp('Duplicating pages', () => org.duplicateSelected())), act('+ Blank after', 'Insert a blank page after', () => void env.pageOp('Adding a blank page', () => { const last = org.selectedItems().pop(); return org.insertBlankAfter(last?.uid ?? null); })), act('Delete', 'Delete the selected pages', () => void env.pageOp('Deleting pages', () => org.setIncluded([...org.selected], false)), true)), h('div', { class: 'row' }, field('Select by number', rangeInput), button('Select', () => { if (!org.selectRanges(rangeInput.value))
            toast('Use page numbers like 1-3, 7', 'error'); }, 'btn-small')), h('div', { class: 'row-actions wrap' }, button('All', () => org.selectAll(), 'btn-small'), button('Odd', () => org.selectRanges(Array.from({ length: Math.ceil(org.items.length / 2) }, (_, i) => i * 2 + 1).join(',')), 'btn-small'), button('Even', () => org.selectRanges(Array.from({ length: Math.floor(org.items.length / 2) }, (_, i) => i * 2 + 2).join(',') || '0'), 'btn-small'), button('None', () => org.selectNone(), 'btn-small')), h('hr'), h('h4', { text: 'Save pages' }), h('div', { class: 'row-actions wrap' }, button('Extract selected pages', () => void exportPages(false), 'btn-small'), button('Split into one file per page', () => void exportPages(true), 'btn-small')), exportOut, h('hr'), h('h4', { text: 'Insert pages from another PDF' }), field('From', insertDoc), field('Pages', insertPages), field('After page (0 = at the start)', insertAfter), button('Insert', () => void insertFrom(), 'btn-small'), h('hr'), h('h4', { text: 'Crop' }), h('p', { class: 'muted small', text: 'Choose the crop tool, drag the area to keep, then apply it to this page or all pages.' }), cropControls(env));
    }
    org.onChange(render);
    core.changed.on((k) => { if (k === 'doc' || k === 'staged')
        render(); });
    render();
    return { id: 'pages', title: 'Pages', el: root, refresh: render };
}
function cropControls(env) {
    const { core } = env;
    const crops = core.stagedOf('crop').filter((c) => c.page === core.page);
    const c = crops[0];
    return h('div', { class: 'row-actions wrap' }, toolButton(env, 'crop', 'Crop tool', 'Drag the area to keep'), button('Apply to this page', () => { if (c)
        void core.run({ op: 'crop', rect: c.rect, pages: [core.page + 1] }, 'Cropping');
    else
        toast('Draw the area to keep first'); }, 'btn-small', { disabled: !c }), button('Apply to all pages', () => { if (c)
        void core.run({ op: 'crop', rect: c.rect, pages: 'all' }, 'Cropping');
    else
        toast('Draw the area to keep first'); }, 'btn-small', { disabled: !c }));
}
// ============================================================================================ annotate
export function annotatePanel(env) {
    const { core } = env;
    const root = h('div', { class: 'panel-body' });
    const tools = h('div', { class: 'btn-grid' }, toolButton(env, 'highlight', 'Highlight', 'Highlight an area'), toolButton(env, 'underline', 'Underline', 'Underline an area'), toolButton(env, 'strikeout', 'Strike out', 'Strike out an area'), toolButton(env, 'note', 'Comment', 'Add a comment'), toolButton(env, 'rect', 'Rectangle', 'Draw a rectangle'), toolButton(env, 'ellipse', 'Ellipse', 'Draw an ellipse'), toolButton(env, 'ink', 'Freehand', 'Draw freehand'));
    const color = h('input', { class: 'color', attrs: { type: 'color', 'aria-label': 'Colour' }, value: core.style.color, on: { input: (e) => { core.style.color = e.target.value; } } });
    const width = select([1, 2, 3, 4, 6].map((n) => ({ value: String(n), label: `${n} pt` })), String(core.style.width), (v) => { core.style.width = Number(v); });
    const opacity = h('input', { attrs: { type: 'range', min: 0.2, max: 1, step: 0.05, 'aria-label': 'Opacity' }, value: String(core.style.opacity), on: { input: (e) => { core.style.opacity = Number(e.target.value); } } });
    const list = h('div', { class: 'item-list' });
    const existing = h('div', { class: 'item-list' });
    async function apply() {
        const items = core.stagedOf('annotation');
        if (!items.length)
            return;
        await core.run({ op: 'annotate', annotations: items.map((i) => i.spec) }, `Adding ${plural(items.length, 'annotation')}`);
    }
    async function editComment(a) {
        const text = await promptText('Edit comment', 'Comment text', a.text);
        if (text === null || text === a.text)
            return;
        await core.run({ op: 'edit_note', selector: a.id ? { id: a.id } : { page: a.page, index: a.index }, text }, 'Editing comment');
    }
    function render() {
        const staged = core.stagedOf('annotation');
        replace(list, ...(staged.length ? staged.map((s) => h('div', { class: `item${core.selectedStaged === s.id ? ' selected' : ''}` }, h('span', { text: `${String(s.spec['type'])} · page ${s.page}` }), button('✕', () => core.unstage(s.id), 'btn-icon', { attrs: { 'aria-label': 'Remove' } }))) : [h('div', { class: 'muted small', text: 'Nothing staged. Pick a tool and draw on the page.' })]));
        const anns = core.info?.annotations ?? [];
        replace(existing, ...(anns.length ? anns.map((a) => h('div', { class: 'item' }, h('span', { class: 'clip', text: `${a.type} · p. ${a.page + 1}${a.text ? ` · ${a.text.slice(0, 40)}` : ''}` }), h('span', { class: 'item-actions' }, button('Go', () => core.setPage(a.page), 'btn-small'), a.type === 'Text' ? button('Edit', () => void editComment(a), 'btn-small', { title: 'Edit the text of this sticky-note comment' }) : null, button('Remove', () => void core.run({ op: 'remove_annotations', selectors: [a.id ? { id: a.id } : { page: a.page, index: a.index }] }, 'Removing annotation'), 'btn-small btn-danger')))) : [h('div', { class: 'muted small', text: 'This document has no annotations.' })]));
        (apply_.disabled) = !staged.length || core.busy;
        apply_.textContent = staged.length ? `Add ${plural(staged.length, 'annotation')} to the PDF` : 'Add annotations to the PDF';
        syncTools(root, env);
    }
    const apply_ = button('Add annotations to the PDF', () => void apply(), 'btn-primary');
    const notice = noticeSlot(env, 'annotate', 'annotating');
    replace(root, notice.el, tools, h('div', { class: 'row' }, field('Colour', color), field('Line', width), field('Opacity', opacity)), h('p', { class: 'muted small', text: 'Marks stay as drafts on the page until you add them. They become standard PDF annotations that other viewers show and that you can remove again.' }), h('h4', { text: 'Drafts' }), list, h('div', { class: 'row-actions' }, apply_, button('Discard drafts', () => core.clearStaged('annotation'), 'btn-small')), h('hr'), h('h4', { text: 'In the document' }), existing);
    core.changed.on((k) => { if (k === 'staged' || k === 'info' || k === 'tool' || k === 'busy' || k === 'doc')
        render(); });
    render();
    return { id: 'annotate', title: 'Annotate', el: root, refresh: render };
}
// ============================================================================================ stamps
export function stampPanel(env) {
    const { core } = env;
    const root = h('div', { class: 'panel-body' });
    const pagesField = (value, set) => field('Pages', h('input', { class: 'input', value, attrs: { type: 'text', placeholder: 'all, odd, even, 1-3' }, on: { input: (e) => set(e.target.value) } }));
    const wm = { text: 'DRAFT', size: 72, opacity: 0.25, color: '#808080', pages: 'all' };
    const pn = { format: 'Page {n} of {N}', position: 'bottom-center', start: 1, size: 10, pages: 'all' };
    const imageInfo = h('div', { class: 'muted small' });
    const fileInput = h('input', { attrs: { type: 'file', accept: '.png,.jpg,.jpeg', 'aria-label': 'Image file' }, class: 'visually-hidden' });
    const tbList = h('div', { class: 'item-list' });
    const imgList = h('div', { class: 'item-list' });
    const num = (v, d) => Number(v) || d;
    const pagesSpec = (v) => { const t = v.trim().toLowerCase(); return !t ? 'all' : (['all', 'odd', 'even', 'first', 'last'].includes(t) ? t : t); };
    fileInput.addEventListener('change', async () => {
        const f = fileInput.files?.[0];
        fileInput.value = '';
        if (!f)
            return;
        try {
            const a = await core.ctx.api.uploadAsset(f, f.name);
            core.pendingAsset = { id: a.id, name: a.name };
            imageInfo.textContent = `${a.name} (${a.width}×${a.height}). Now choose “Draw image area” and drag where it should go.`;
            core.setTool('imagebox');
        }
        catch (e) {
            toast(errorMessage(e), 'error');
        }
    });
    async function applyTextBoxes() {
        const boxes = core.stagedOf('textbox');
        for (const b of boxes) {
            const r = await core.run({ op: 'stamp', kind: 'textbox', rect: b.rect, text: b.text, size: b.size, color: b.color, pages: [b.page + 1] }, 'Adding text', { keepStaged: true, quiet: true });
            if (!r)
                return;
            core.unstage(b.id);
        }
        toast(`Added ${plural(boxes.length, 'text box', 'text boxes')}`, 'ok');
    }
    async function applyImages() {
        const imgs = core.stagedOf('image');
        for (const b of imgs) {
            const r = await core.run({ op: 'stamp', kind: 'image', asset: b.asset, rect: b.rect, pages: [b.page + 1] }, 'Placing image', { keepStaged: true, quiet: true });
            if (!r)
                return;
            core.unstage(b.id);
        }
        toast(`Placed ${plural(imgs.length, 'image')}`, 'ok');
    }
    function render() {
        const tbs = core.stagedOf('textbox');
        const ims = core.stagedOf('image');
        replace(tbList, ...tbs.map((s) => h('div', { class: 'item' }, h('span', { class: 'clip', text: `p. ${s.page + 1} · ${s.text.slice(0, 40)}` }), button('✕', () => core.unstage(s.id), 'btn-icon'))));
        replace(imgList, ...ims.map((s) => h('div', { class: 'item' }, h('span', { class: 'clip', text: `p. ${s.page + 1} · ${s.name}` }), button('✕', () => core.unstage(s.id), 'btn-icon'))));
        syncTools(root, env);
        for (const b of root.querySelectorAll('button[data-apply="textbox"]'))
            b.disabled = !tbs.length || core.busy;
        for (const b of root.querySelectorAll('button[data-apply="image"]'))
            b.disabled = !ims.length || core.busy;
    }
    const notice = noticeSlot(env, 'modify', 'adding content');
    replace(root, notice.el, h('p', { class: 'muted small', text: 'Stamped content becomes part of the page. Use Undo (top bar) to take it back.' }), h('h4', { text: 'Watermark' }), field('Text', h('input', { class: 'input', value: wm.text, attrs: { type: 'text', maxlength: 60 }, on: { input: (e) => { wm.text = e.target.value; } } })), h('div', { class: 'row' }, field('Size', h('input', { class: 'input short', value: String(wm.size), attrs: { type: 'number', min: 8, max: 300 }, on: { input: (e) => { wm.size = num(e.target.value, 72); } } })), field('Opacity', h('input', { attrs: { type: 'range', min: 0.05, max: 1, step: 0.05 }, value: String(wm.opacity), on: { input: (e) => { wm.opacity = Number(e.target.value); } } })), field('Colour', h('input', { class: 'color', attrs: { type: 'color' }, value: wm.color, on: { input: (e) => { wm.color = e.target.value; } } }))), pagesField(wm.pages, (v) => { wm.pages = v; }), button('Add watermark', () => void core.run({ op: 'stamp', kind: 'watermark', text: wm.text, size: wm.size, opacity: wm.opacity, color: wm.color, pages: pagesSpec(wm.pages) }, 'Adding watermark'), 'btn-small'), h('hr'), h('h4', { text: 'Page numbers' }), field('Format', h('input', { class: 'input', value: pn.format, attrs: { type: 'text' }, on: { input: (e) => { pn.format = e.target.value; } } }), 'Use {n} for the number and {N} for the total'), h('div', { class: 'row' }, field('Position', select([['bottom-center', 'Bottom centre'], ['bottom-left', 'Bottom left'], ['bottom-right', 'Bottom right'], ['top-center', 'Top centre'], ['top-left', 'Top left'], ['top-right', 'Top right']].map(([v, l]) => ({ value: v, label: l })), pn.position, (v) => { pn.position = v; })), field('Start at', h('input', { class: 'input short', value: '1', attrs: { type: 'number', min: 0 }, on: { input: (e) => { pn.start = num(e.target.value, 1); } } }))), pagesField(pn.pages, (v) => { pn.pages = v; }), button('Add page numbers', () => void core.run({ op: 'stamp', kind: 'page_numbers', format: pn.format, position: pn.position, start: pn.start, size: pn.size, pages: pagesSpec(pn.pages) }, 'Adding page numbers'), 'btn-small'), h('hr'), h('h4', { text: 'Text at a point' }), field('Text', h('input', { class: 'input', attrs: { type: 'text', placeholder: 'e.g. APPROVED' }, on: { input: (e) => { core.stampForm.text = e.target.value; } } })), h('div', { class: 'row' }, field('Size', h('input', { class: 'input short', value: '24', attrs: { type: 'number', min: 6, max: 200 }, on: { input: (e) => { core.stampForm.size = num(e.target.value, 24); } } })), field('Angle', h('input', { class: 'input short', value: '0', attrs: { type: 'number', min: -180, max: 180 }, on: { input: (e) => { core.stampForm.rotation = num(e.target.value, 0); } } })), field('Colour', h('input', { class: 'color', attrs: { type: 'color' }, value: core.stampForm.color, on: { input: (e) => { core.stampForm.color = e.target.value; } } }))), toolButton(env, 'stamp', 'Click on the page to place', 'Click on the page where the text should go'), h('hr'), h('h4', { text: 'Text boxes' }), h('p', { class: 'muted small', text: 'Drag a box on the page and type the text. Add several, then apply them together.' }), h('div', { class: 'row-actions wrap' }, toolButton(env, 'textbox', 'Draw text box', 'Drag a box on the page'), button('Add text boxes to the PDF', () => void applyTextBoxes(), 'btn-small', { attrs: { 'data-apply': 'textbox' } })), tbList, h('hr'), h('h4', { text: 'Image' }), h('div', { class: 'row-actions wrap' }, button('Choose image…', () => fileInput.click(), 'btn-small'), fileInput, toolButton(env, 'imagebox', 'Draw image area', 'Drag the area for the image'), button('Place images', () => void applyImages(), 'btn-small', { attrs: { 'data-apply': 'image' } })), imageInfo, imgList);
    core.changed.on((k) => { if (k === 'staged' || k === 'tool' || k === 'busy')
        render(); });
    render();
    return { id: 'stamp', title: 'Add', el: root, refresh: render };
}
// ============================================================================================ find
export function findPanel(env) {
    const { core } = env;
    const root = h('div', { class: 'panel-body' });
    const q = { query: '', regex: false, caseSensitive: false, whole: false };
    const results = h('div', { class: 'item-list' });
    const summary = h('div', { class: 'muted small' });
    const input = h('input', { class: 'input', attrs: { type: 'search', placeholder: 'Find text in this PDF', 'aria-label': 'Find text' }, on: { input: (e) => { q.query = e.target.value; }, keydown: (e) => { if (e.key === 'Enter')
                void search(); } } });
    async function search() {
        const d = core.doc;
        if (!d || !q.query)
            return;
        try {
            const r = await core.ctx.api.post(`/api/documents/${d.id}/find`, { query: q.query, regex: q.regex, case: q.caseSensitive, wholeWord: q.whole });
            core.setHits(r.hits);
            if (r.hits[0])
                core.setPage(r.hits[0].page);
        }
        catch (e) {
            toast(errorMessage(e), 'error');
        }
    }
    function go(i) {
        if (!core.hits.length)
            return;
        core.hitIndex = (i + core.hits.length) % core.hits.length;
        const hit = core.hits[core.hitIndex];
        if (hit.page !== core.page)
            core.setPage(hit.page);
        else
            core.changed.emit('hits');
        core.changed.emit('hits');
    }
    function highlightAll() {
        for (const hit of core.hits) {
            const rects = hit.rects;
            core.stage({ kind: 'annotation', page: hit.page + 1, spec: { type: 'highlight', page: hit.page + 1, rects, color: '#facc15', opacity: 0.4 }, preview: { type: 'highlight', rects, color: '#facc15' } });
        }
        toast(`${plural(core.hits.length, 'highlight')} drafted. Add them in the Annotate tab.`, 'ok');
    }
    function redactAll() {
        let n = 0;
        for (const hit of core.hits)
            for (const r of hit.rects) {
                core.stage({ kind: 'redaction', page: hit.page, rect: r, label: hit.text });
                n++;
            }
        toast(`${plural(n, 'area')} marked for redaction. Review and apply them in the Redact tab.`, 'ok');
    }
    function render() {
        summary.textContent = core.hits.length ? `${plural(core.hits.length, 'match', 'matches')}${core.hitIndex >= 0 ? ` · showing ${core.hitIndex + 1}` : ''}` : (q.query ? 'No matches yet. Press Enter to search.' : '');
        replace(results, ...core.hits.slice(0, 300).map((hit, i) => h('button', { class: `item clickable${i === core.hitIndex ? ' selected' : ''}`, attrs: { type: 'button' }, on: { click: () => go(i) } }, h('span', { class: 'clip', text: hit.text }), h('span', { class: 'muted', text: `p. ${hit.page + 1}` }))));
    }
    replace(root, input, h('div', { class: 'row-actions wrap' }, checkbox('Pattern (regex)', false, (v) => { q.regex = v; }), checkbox('Match case', false, (v) => { q.caseSensitive = v; }), checkbox('Whole word', false, (v) => { q.whole = v; })), h('div', { class: 'row-actions' }, button('Find', () => void search(), 'btn-primary btn-small'), button('◀', () => go(core.hitIndex - 1), 'btn-small', { attrs: { 'aria-label': 'Previous match' } }), button('▶', () => go(core.hitIndex + 1), 'btn-small', { attrs: { 'aria-label': 'Next match' } })), summary, h('div', { class: 'row-actions wrap' }, button('Highlight all', highlightAll, 'btn-small'), button('Mark all for redaction', redactAll, 'btn-small btn-danger')), results, h('p', { class: 'muted small', text: 'Search reads the PDF’s text layer. Scanned pages have none: run OCR in the Document tab first.' }));
    core.changed.on((k) => { if (k === 'hits' || k === 'doc')
        render(); });
    render();
    return { id: 'find', title: 'Find', el: root, refresh: render };
}
export { badge, listBlock, verificationView, parseRanges };
//# sourceMappingURL=editor-panels-a.js.map