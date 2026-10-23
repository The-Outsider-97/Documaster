// Application shell: sidebar navigation, theme, workspace creation, and hand-over between workspaces.
import { Api, ApiError } from './api.js';
import { h, replace, toast } from './dom.js';
import { Documents, loadTheme, saveTheme } from './store.js';
import { createAssistant } from './ws/assistant.js';
import { createConverter } from './ws/converter.js';
import { createCounter } from './ws/counter.js';
import { createEditor } from './ws/editor.js';
import { createMerger } from './ws/merger.js';
import { createReader } from './ws/reader.js';
import { createSystem } from './ws/system.js';
const NAV = [
    { id: 'counter', label: 'Word Counter', icon: '𝐀', make: createCounter },
    { id: 'converter', label: 'Converter', icon: '⇄', make: createConverter },
    { id: 'merger', label: 'Merger', icon: '⧉', make: createMerger },
    { id: 'editor', label: 'PDF Editor', icon: '✎', make: createEditor },
    { id: 'reader', label: 'Reader & Analysis', icon: '◎', make: createReader },
    { id: 'assistant', label: 'AI Assistant', icon: '✦', make: createAssistant },
];
const SYSTEM = { id: 'system', label: 'System', icon: '⚙', make: createSystem };
function applyTheme(t) { document.documentElement.dataset['theme'] = t; }
export function start(root) {
    const api = new Api('');
    const docs = new Documents(api);
    let formatsPromise = null;
    const workspaces = new Map();
    const ctx = {
        api, docs,
        formats: () => (formatsPromise ??= api.get('/api/convert/formats')),
        open: (w, docId, page) => show(w, { docId, page }),
        openers: new Map(),
    };
    let theme = loadTheme();
    applyTheme(theme);
    const main = h('main', { class: 'main', attrs: { id: 'main' } });
    const nav = h('nav', { class: 'nav', attrs: { 'aria-label': 'Workspaces' } });
    const themeBtn = h('button', { class: 'theme-toggle', attrs: { type: 'button', role: 'switch' }, on: { click: () => { theme = theme === 'dark' ? 'light' : 'dark'; saveTheme(theme); applyTheme(theme); syncTheme(); } } });
    const syncTheme = () => {
        themeBtn.setAttribute('aria-checked', String(theme === 'light'));
        replace(themeBtn, h('span', { class: 'toggle-icon', text: theme === 'dark' ? '☾' : '☀', attrs: { 'aria-hidden': 'true' } }), h('span', { text: theme === 'dark' ? 'Dark' : 'Light' }), h('span', { class: 'toggle-track' }, h('span', { class: 'toggle-knob' })));
    };
    syncTheme();
    const items = [...NAV, SYSTEM];
    const links = new Map();
    for (const it of NAV)
        links.set(it.id, h('a', { class: 'nav-item', attrs: { href: `#/${it.id}`, 'data-ws': it.id } }, h('span', { class: 'nav-icon', text: it.icon, attrs: { 'aria-hidden': 'true' } }), h('span', { text: it.label })));
    links.set('system', h('a', { class: 'nav-item subtle', attrs: { href: '#/system', 'data-ws': 'system' } }, h('span', { class: 'nav-icon', text: SYSTEM.icon, attrs: { 'aria-hidden': 'true' } }), h('span', { text: SYSTEM.label })));
    replace(nav, ...NAV.map((i) => links.get(i.id)));
    const side = h('aside', { class: 'sidebar' }, h('div', { class: 'brand' }, h('img', { class: 'brand-logo', attrs: { src: 'assets/documaster.png', alt: '', width: 36, height: 36 } }), h('div', {}, h('div', { class: 'brand-name', text: 'Documaster' }), h('div', { class: 'brand-sub', text: 'SLAI document workspace' }))), nav, h('div', { class: 'spacer' }), links.get('system'), themeBtn);
    replace(root, side, main);
    function show(id, arg) {
        const item = items.find((i) => i.id === id) ?? NAV[0];
        let ws = workspaces.get(item.id);
        if (!ws) {
            ws = item.make(ctx);
            ws.el.hidden = true;
            workspaces.set(item.id, ws);
            main.appendChild(ws.el);
        }
        for (const [k, w] of workspaces)
            w.el.hidden = k !== item.id;
        for (const [k, a] of links) {
            a.classList.toggle('active', k === item.id);
            if (k === item.id)
                a.setAttribute('aria-current', 'page');
            else
                a.removeAttribute('aria-current');
        }
        document.title = `${item.label} · SLAI Documaster`;
        if (location.hash !== `#/${item.id}`)
            history.replaceState(null, '', `#/${item.id}`);
        ws.activate?.(arg);
        main.scrollTop = 0;
    }
    const fromHash = () => {
        const m = /^#\/(\w+)/.exec(location.hash);
        const id = m?.[1];
        return items.some((i) => i.id === id) ? id : 'counter';
    };
    window.addEventListener('hashchange', () => show(fromHash()));
    window.addEventListener('keydown', (e) => {
        if (e.altKey && !e.ctrlKey && !e.metaKey && /^[1-6]$/.test(e.key)) {
            e.preventDefault();
            show(NAV[Number(e.key) - 1].id);
        }
    });
    // A file dropped outside a drop zone must not make the browser navigate away from the app.
    for (const type of ['dragover', 'drop'])
        window.addEventListener(type, (e) => { if (!e.target?.closest?.('.dropzone'))
            e.preventDefault(); });
    api.get('/api/health').then(() => docs.refresh()).then(() => show(fromHash())).catch((e) => {
        if (e instanceof ApiError && e.status === 401) {
            replace(main, h('div', { class: 'workspace' }, h('div', { class: 'card' }, h('h2', { text: 'Open Documaster from its launch link' }), h('p', { text: 'Documaster protects itself with a key that is part of the address it printed when it started. Open that address (it ends with ?token=…) to continue.' }))));
            return;
        }
        toast(e instanceof Error ? e.message : String(e), 'error');
        show(fromHash());
    });
}
start(document.getElementById('app'));
//# sourceMappingURL=main.js.map