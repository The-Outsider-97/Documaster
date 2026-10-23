// The PDF page viewer: the page image with an overlay for drawing tools, found text, and staged marks; plus the thumbnail strip.
import { h, promptText, replace } from '../dom.js';
import { clamp01, isTiny, rectFromPoints, rectToCss, simplify, toNormalised } from '../geometry.js';
const DRAG_TOOLS = ['highlight', 'underline', 'strikeout', 'rect', 'ellipse', 'redact', 'textbox', 'imagebox', 'crop'];
export const TOOL_HINTS = {
    view: 'Browse the document.',
    highlight: 'Drag over text to highlight it.',
    underline: 'Drag over text to underline it.',
    strikeout: 'Drag over text to strike it out.',
    note: 'Click where the comment should go.',
    rect: 'Drag to draw a rectangle.',
    ellipse: 'Drag to draw an ellipse.',
    ink: 'Draw freehand with the pointer.',
    redact: 'Drag over what must be removed. Nothing changes until you apply the redactions.',
    textbox: 'Drag a box, then type the text. It becomes part of the page.',
    imagebox: 'Drag the area for the image.',
    crop: 'Drag the area to keep.',
    stamp: 'Click where the text goes.',
};
const SVG_NS = 'http://www.w3.org/2000/svg';
const svg = (tag, attrs) => {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs))
        el.setAttribute(k, String(v));
    return el;
};
export function createViewer(core, onStampPoint) {
    const img = h('img', { class: 'page-img', attrs: { alt: '', draggable: false } });
    const overlay = h('div', { class: 'overlay-layer', attrs: { tabindex: 0, 'aria-label': 'Page. Use the tools on the right to mark it.' } });
    const frame = h('div', { class: 'page-frame' }, img, overlay);
    const scroller = h('div', { class: 'viewer-scroll' }, frame);
    const hint = h('div', { class: 'viewer-hint muted small' });
    const el = h('div', { class: 'viewer' }, hint, scroller);
    let loadedKey = '';
    let draft = null;
    const widthPx = () => Math.max(240, (scroller.clientWidth || 700) - 32) * core.zoom;
    function layoutFrame() {
        const info = core.info?.pages[core.page];
        const w = widthPx();
        frame.style.width = `${w}px`;
        if (info) {
            const turned = info.rotation % 180 !== 0;
            const pw = turned ? info.height : info.width;
            const ph = turned ? info.width : info.height;
            frame.style.aspectRatio = `${pw} / ${ph}`;
        }
    }
    function loadImage() {
        const d = core.doc;
        if (!d || d.locked) {
            img.removeAttribute('src');
            loadedKey = '';
            return;
        }
        const target = Math.min(2400, Math.round(widthPx() * Math.min(2, window.devicePixelRatio || 1)));
        const bucket = Math.ceil(target / 200) * 200;
        const key = `${d.id}:${d.sha256}:${core.page}:${bucket}`;
        if (key === loadedKey)
            return;
        loadedKey = key;
        img.alt = `${d.name}, page ${core.page + 1}`;
        img.src = core.ctx.api.pageImageUrl(d.id, core.page, bucket, d.sha256);
    }
    // ---- overlay content -----------------------------------------------------------------------------------------------------
    function drawOverlay() {
        const parts = [];
        // found text
        core.hits.forEach((hit, i) => {
            if (hit.page !== core.page)
                return;
            for (const r of hit.rects)
                parts.push(h('div', { class: `hit${i === core.hitIndex ? ' current' : ''}`, style: rectToCss(r) }));
        });
        // staged marks
        const vec = svg('svg', { class: 'ink-layer', viewBox: '0 0 1 1', preserveAspectRatio: 'none' });
        for (const s of core.staged) {
            const onPage = s.kind === 'annotation' ? s.page - 1 === core.page : s.page === core.page;
            if (!onPage)
                continue;
            const selected = core.selectedStaged === s.id;
            const select = (e) => { e.stopPropagation(); core.selectedStaged = s.id; core.changed.emit('staged'); };
            if (s.kind === 'redaction')
                parts.push(h('div', { class: `staged redaction${selected ? ' selected' : ''}`, style: rectToCss(s.rect), title: 'Will be permanently removed. Click to select, Delete to unmark.', on: { pointerdown: select } }, h('span', { text: 'REDACT' })));
            else if (s.kind === 'textbox')
                parts.push(h('div', { class: `staged textbox${selected ? ' selected' : ''}`, style: { ...rectToCss(s.rect), color: s.color }, on: { pointerdown: select } }, s.text));
            else if (s.kind === 'image')
                parts.push(h('div', { class: `staged imagebox${selected ? ' selected' : ''}`, style: rectToCss(s.rect), on: { pointerdown: select } }, s.name));
            else if (s.kind === 'crop')
                parts.push(h('div', { class: `staged crop${selected ? ' selected' : ''}`, style: rectToCss(s.rect), on: { pointerdown: select } }, 'Keep'));
            else {
                const p = s.preview;
                const stroke = p.color;
                const mk = (rect) => {
                    if (p.type === 'highlight')
                        vec.appendChild(svg('rect', { x: rect[0], y: rect[1], width: rect[2] - rect[0], height: rect[3] - rect[1], fill: stroke, 'fill-opacity': 0.35 }));
                    else if (p.type === 'underline')
                        vec.appendChild(svg('line', { x1: rect[0], x2: rect[2], y1: rect[3], y2: rect[3], stroke, 'stroke-width': 2, 'vector-effect': 'non-scaling-stroke' }));
                    else if (p.type === 'strikeout')
                        vec.appendChild(svg('line', { x1: rect[0], x2: rect[2], y1: (rect[1] + rect[3]) / 2, y2: (rect[1] + rect[3]) / 2, stroke, 'stroke-width': 2, 'vector-effect': 'non-scaling-stroke' }));
                    else if (p.type === 'rect')
                        vec.appendChild(svg('rect', { x: rect[0], y: rect[1], width: rect[2] - rect[0], height: rect[3] - rect[1], fill: 'none', stroke, 'stroke-width': 2, 'vector-effect': 'non-scaling-stroke' }));
                    else if (p.type === 'ellipse')
                        vec.appendChild(svg('ellipse', { cx: (rect[0] + rect[2]) / 2, cy: (rect[1] + rect[3]) / 2, rx: (rect[2] - rect[0]) / 2, ry: (rect[3] - rect[1]) / 2, fill: 'none', stroke, 'stroke-width': 2, 'vector-effect': 'non-scaling-stroke' }));
                };
                for (const r of p.rects ?? (p.rect ? [p.rect] : []))
                    mk(r);
                for (const stroke2 of p.strokes ?? [])
                    vec.appendChild(svg('polyline', { points: stroke2.map((q) => `${q[0]},${q[1]}`).join(' '), fill: 'none', stroke, 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke' }));
                if (p.point)
                    parts.push(h('div', { class: `staged note${selected ? ' selected' : ''}`, style: { left: `${p.point[0] * 100}%`, top: `${p.point[1] * 100}%` }, title: String(s.spec.text ?? ''), on: { pointerdown: select } }, '✎'));
                if (selected && (p.rect || p.rects?.[0])) {
                    const r = (p.rect ?? p.rects[0]);
                    parts.push(h('div', { class: 'staged selected-outline', style: rectToCss(r) }));
                }
            }
        }
        // the shape being drawn
        if (draft) {
            if (core.tool === 'ink' && draft.points)
                vec.appendChild(svg('polyline', { points: draft.points.map((q) => `${q.x},${q.y}`).join(' '), fill: 'none', stroke: core.style.color, 'stroke-width': core.style.width, 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke' }));
            else
                parts.push(h('div', { class: `draft draft-${core.tool}`, style: rectToCss(rectFromPoints(draft.a, draft.b)) }));
        }
        replace(overlay, vec, ...parts);
        overlay.className = `overlay-layer tool-${core.tool}`;
        hint.textContent = core.tool === 'view' ? '' : TOOL_HINTS[core.tool];
    }
    // ---- pointer handling --------------------------------------------------------------------------------------------------------
    const point = (e) => toNormalised(e.clientX, e.clientY, overlay.getBoundingClientRect());
    overlay.addEventListener('pointerdown', (e) => {
        if (e.button !== 0 || !core.canEdit)
            return;
        const t = core.tool;
        if (t === 'view') {
            core.selectedStaged = null;
            core.changed.emit('staged');
            return;
        }
        const p = point(e);
        if (t === 'note') {
            void addNote(p);
            return;
        }
        if (t === 'stamp') {
            onStampPoint?.(core.page, p);
            return;
        }
        if (t !== 'ink' && !DRAG_TOOLS.includes(t))
            return;
        overlay.setPointerCapture(e.pointerId);
        draft = { a: p, b: p, points: t === 'ink' ? [p] : undefined };
        drawOverlay();
    });
    overlay.addEventListener('pointermove', (e) => {
        if (!draft)
            return;
        const p = point(e);
        draft.b = p;
        draft.points?.push(p);
        drawOverlay();
    });
    const finish = (e) => {
        if (!draft)
            return;
        const d = draft;
        draft = null;
        try {
            overlay.releasePointerCapture(e.pointerId);
        }
        catch { /* not captured */ }
        const t = core.tool;
        if (t === 'ink') {
            const pts = simplify(d.points ?? [], 0.0015);
            if (pts.length >= 2) {
                const strokes = [pts.map((q) => [round(q.x), round(q.y)])];
                core.stage({ kind: 'annotation', page: core.page + 1, spec: { type: 'ink', page: core.page + 1, strokes, color: core.style.color, width: core.style.width, opacity: core.style.opacity }, preview: { type: 'ink', strokes, color: core.style.color } });
            }
        }
        else {
            const r = rectFromPoints(d.a, d.b);
            if (!isTiny(r))
                void commitRect(t, r);
        }
        drawOverlay();
    };
    overlay.addEventListener('pointerup', finish);
    overlay.addEventListener('pointercancel', finish);
    overlay.addEventListener('keydown', (e) => {
        if ((e.key === 'Delete' || e.key === 'Backspace') && core.selectedStaged !== null) {
            e.preventDefault();
            core.unstage(core.selectedStaged);
        }
        else if (e.key === 'Escape') {
            draft = null;
            if (core.tool !== 'view')
                core.setTool('view');
            drawOverlay();
        }
    });
    const round = (v) => Math.round(clamp01(v) * 10000) / 10000;
    const R = (r) => [round(r[0]), round(r[1]), round(r[2]), round(r[3])];
    async function commitRect(t, r) {
        const page1 = core.page + 1;
        const rect = R(r);
        const s = core.style;
        switch (t) {
            case 'highlight':
            case 'underline':
            case 'strikeout': {
                const color = t === 'highlight' && s.color === '#e11d48' ? '#facc15' : s.color;
                core.stage({ kind: 'annotation', page: page1, spec: { type: t, page: page1, rects: [rect], color, opacity: t === 'highlight' ? 0.4 : 1 }, preview: { type: t, rect, color } });
                break;
            }
            case 'rect':
            case 'ellipse':
                core.stage({ kind: 'annotation', page: page1, spec: { type: t, page: page1, rect, color: s.color, width: s.width, opacity: s.opacity, ...(s.fill ? { fill: s.fill } : {}) }, preview: { type: t, rect, color: s.color } });
                break;
            case 'redact':
                core.stage({ kind: 'redaction', page: core.page, rect });
                break;
            case 'crop':
                core.stagedOf('crop').filter((c) => c.page === core.page).forEach((c) => core.unstage(c.id));
                core.stage({ kind: 'crop', page: core.page, rect });
                break;
            case 'textbox': {
                const text = await promptText('Text on the page', 'Text', '');
                if (text)
                    core.stage({ kind: 'textbox', page: core.page, rect, text, size: 12, color: '#111827' });
                break;
            }
            case 'imagebox': {
                const asset = core.pendingAsset;
                if (!asset) {
                    core.setTool('view');
                    break;
                }
                core.stage({ kind: 'image', page: core.page, rect, asset: asset.id, name: asset.name });
                break;
            }
            default: break;
        }
    }
    async function addNote(p) {
        const text = await promptText('Comment', 'Comment text', '');
        if (!text)
            return;
        const page1 = core.page + 1;
        const point = [round(p.x), round(p.y)];
        core.stage({ kind: 'annotation', page: page1, spec: { type: 'note', page: page1, point, text, color: '#facc15' }, preview: { type: 'note', point, color: '#facc15' } });
    }
    function refresh() {
        layoutFrame();
        loadImage();
        drawOverlay();
        if (core.hitIndex >= 0 && core.hits[core.hitIndex]?.page === core.page) {
            window.setTimeout(() => overlay.querySelector('.hit.current')?.scrollIntoView({ block: 'center', behavior: 'smooth' }), 50);
        }
    }
    let resizeTimer;
    new ResizeObserver(() => { window.clearTimeout(resizeTimer); resizeTimer = window.setTimeout(() => refresh(), 120); }).observe(scroller);
    img.addEventListener('error', () => { img.alt = 'The page could not be drawn'; });
    return { el, refresh };
}
// ---- thumbnails -------------------------------------------------------------------------------------------------------------------
export function createThumbs(core, org, onDrop) {
    const el = h('div', { class: 'thumbs', attrs: { role: 'listbox', 'aria-label': 'Pages', 'aria-multiselectable': 'true' } });
    const cards = new Map();
    function make(uid) {
        const card = h('div', { class: 'thumb', attrs: { draggable: true, tabindex: 0, role: 'option', 'data-uid': uid } }, h('div', { class: 'thumb-img' }), h('div', { class: 'thumb-num' }));
        card.addEventListener('click', (e) => {
            const item = org.items.find((i) => i.uid === uid);
            if (!item)
                return;
            if (e.shiftKey)
                org.selectThrough(uid);
            else if (e.ctrlKey || e.metaKey)
                org.toggleSelect(uid);
            else
                org.selectOnly(uid);
            if (!item.blank && item.included)
                core.setPage(item.page);
        });
        card.addEventListener('dragstart', (e) => {
            if (!org.selected.has(uid))
                org.selectOnly(uid);
            e.dataTransfer?.setData('text/plain', 'documaster-thumbs');
            card.classList.add('dragging');
        });
        card.addEventListener('dragend', () => { card.classList.remove('dragging'); for (const c of cards.values())
            c.classList.remove('drop-before', 'drop-after'); });
        card.addEventListener('dragover', (e) => {
            if (org.selected.has(uid))
                return;
            e.preventDefault();
            const r = card.getBoundingClientRect();
            const before = e.clientY < r.top + r.height / 2;
            card.classList.toggle('drop-before', before);
            card.classList.toggle('drop-after', !before);
        });
        card.addEventListener('dragleave', () => card.classList.remove('drop-before', 'drop-after'));
        card.addEventListener('drop', (e) => {
            e.preventDefault();
            const before = card.classList.contains('drop-before');
            card.classList.remove('drop-before', 'drop-after');
            if (org.selected.has(uid))
                return;
            onDrop(before ? uid : (card.nextElementSibling?.dataset['uid'] ?? null));
        });
        cards.set(uid, card);
        return card;
    }
    function refresh() {
        const d = core.doc;
        const live = new Set(org.items.map((i) => i.uid));
        for (const [uid, c] of cards)
            if (!live.has(uid)) {
                c.remove();
                cards.delete(uid);
            }
        let n = 0;
        for (const item of org.items) {
            n++;
            const c = cards.get(item.uid) ?? make(item.uid);
            c.classList.toggle('selected', org.selected.has(item.uid));
            c.classList.toggle('current', !item.blank && item.page === core.page);
            c.classList.toggle('excluded', !item.included);
            c.setAttribute('aria-selected', String(org.selected.has(item.uid)));
            c.querySelector('.thumb-num').textContent = item.blank ? `${n} · blank` : String(n);
            const box = c.querySelector('.thumb-img');
            if (item.blank || !d) {
                replace(box, h('div', { class: 'blank-page', text: '' }));
            }
            else {
                const url = core.ctx.api.pageImageUrl(d.id, item.page, 160, d.sha256);
                let im = box.querySelector('img');
                if (!im || im.getAttribute('src') !== url) {
                    im = h('img', { attrs: { src: url, alt: `Page ${item.page + 1}`, loading: 'lazy', draggable: false } });
                    replace(box, im);
                }
                im.style.transform = item.rotation ? `rotate(${item.rotation}deg)${item.rotation % 180 ? ' scale(0.7)' : ''}` : '';
            }
            if (el.children[n - 1] !== c)
                el.insertBefore(c, el.children[n - 1] ?? null);
        }
    }
    return { el, refresh };
}
//# sourceMappingURL=editor-view.js.map