// System: what this installation can and cannot do right now (formats, tools, SLAI agents), and its settings.
import { sectionTitle } from '../components.js';
import { badge, button, checkbox, errorMessage, field, h, replace, toast } from '../dom.js';
/** How Documaster uses each SLAI agent. "Not used" is stated, not hidden. */
const ROLES = {
    reader: { name: 'Reader', use: 'Parsing, format detection, integrity checks and lossless PDF optimisation (engines used directly).', used: true },
    privacy: { name: 'Privacy', use: 'Gate: document text goes to other agents only if agents may read documents and the Privacy agent allows it each time.', used: true },
    safety: { name: 'Safety', use: 'Reviews untrusted document text before a language agent sees it.', used: true },
    language: { name: 'Language', use: 'Intent tie-break, passage re-ranking and an optional machine-written summary (always labelled).', used: true },
    quality: { name: 'Quality', use: 'Reviews per-page extraction statistics of a PDF (Reader & Analysis, Structure tab). No text is sent.', used: true },
    observability: { name: 'Observability', use: 'Receives job timings and outcomes (off unless enabled below).', used: true },
    handler: { name: 'Handler', use: 'Describes job failures in a standard form: category, severity, retryable (off unless enabled below).', used: true },
    planning: { name: 'Planning', use: 'Not used. Documaster’s steps have fixed dependencies, so no planner is needed.', used: false },
};
const stateBadge = (state) => badge(state, state === 'ready' ? 'ok' : state === 'failed' || state === 'unavailable' ? 'bad' : state === 'partial' ? 'warn' : 'neutral');
export function createSystem(ctx) {
    const body = h('div', { class: 'system' });
    let agentsStatus = null;
    async function load(probe = '') {
        try {
            const [caps, agents, settings] = await Promise.all([ctx.api.get('/api/capabilities'), ctx.api.get(`/api/agents${probe === 'test' ? '?test=1' : probe === 'probe' ? '?probe=1' : ''}`), ctx.api.get('/api/settings')]);
            agentsStatus = agents;
            render(caps, agents, settings);
        }
        catch (e) {
            replace(body, h('div', { class: 'error', text: errorMessage(e) }));
        }
    }
    function formatTable(rows) {
        return h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ...['Format', 'Status', 'Handled by', 'Limits'].map((t) => h('th', { text: t })))), h('tbody', {}, ...rows.map((r) => h('tr', {}, h('td', { text: r.label }), h('td', {}, badge(r.available ? 'available' : 'unavailable', r.available ? 'ok' : 'bad', r.needs.join(', '))), h('td', { class: 'small', text: r.backend }), h('td', { class: 'small' }, r.limits.length ? h('ul', { class: 'tight' }, ...r.limits.map((l) => h('li', { text: l }))) : '–')))));
    }
    function render(c, a, s) {
        const outDir = h('input', { class: 'input', value: s.output_dir, attrs: { type: 'text', 'aria-label': 'Output folder' } });
        const mods = Object.entries(c.tools.modules);
        replace(body, h('section', { class: 'card' }, h('h3', { text: `${c.app.name} ${c.app.version}` }), h('div', { class: 'chips' }, ...[['LibreOffice', c.tools.libreoffice], ['Tesseract OCR', c.tools.tesseract], ['qpdf', c.tools.qpdf]].map(([n, p]) => badge(`${n}: ${p ? 'found' : 'not found'}`, p ? 'ok' : 'neutral', String(p ?? '')))), h('div', { class: 'chips' }, ...mods.map(([n, ok]) => badge(`${n}: ${ok ? 'yes' : 'no'}`, ok ? 'ok' : 'warn'))), c.tools.tesseract ? h('div', { class: 'muted small', text: `OCR languages: ${c.tools.tesseractLanguages.join(', ')}` }) : h('div', { class: 'muted small', text: 'OCR is not available: install Tesseract to read scanned pages.' })), h('section', { class: 'card' }, sectionTitle('Settings'), field('Output folder', outDir, 'Where converted, merged and saved files are written. Existing files are never overwritten.'), button('Save', async () => { try {
            await ctx.api.put('/api/settings', { output_dir: outDir.value });
            toast('Saved', 'ok');
        }
        catch (e) {
            toast(errorMessage(e), 'error');
        } }, 'btn-small'), checkbox('Let SLAI agents read document text', s.agent_read_default, async (v) => { try {
            await ctx.api.put('/api/settings', { agent_read_default: v });
            void load();
        }
        catch (e) {
            toast(errorMessage(e), 'error');
        } }, 'The agents run on this computer and the Privacy agent still decides each time. You can also turn it off for one document in Reader & Analysis.'), checkbox('Send job timings and failures to the SLAI Observability and Handler agents', s.agent_telemetry, async (v) => { try {
            await ctx.api.put('/api/settings', { agent_telemetry: v });
            void load();
        }
        catch (e) {
            toast(errorMessage(e), 'error');
        } }, 'Loads those agents; off by default'), h('div', { class: 'muted small', text: `Agent mode: ${s.agents} (set when starting Documaster) · upload limit ${s.max_upload_mb} MB · PDF page limit ${s.max_pdf_pages} · job time limit ${s.job_timeout_s} s` })), h('section', { class: 'card' }, sectionTitle('SLAI agents', button('Test agents now', () => void load('test'), 'btn-small', { title: 'Loads each agent and makes one real, harmless call with a sample text (this can take a minute the first time)' })), a.fatal ? h('div', { class: 'notice warn', text: a.fatal }) : null, h('div', { class: 'muted small', text: a.root ? `SLAI checkout: ${a.root}` : 'No SLAI checkout was found. Reading SLDC files and the agents need one (--slai-root).' }), h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ...['Agent', 'How Documaster uses it', 'State', 'Self-test', 'Calls', 'Notes'].map((t) => h('th', { text: t })))), h('tbody', {}, ...Object.entries(ROLES).map(([k, r]) => {
            const st = a.agents[k];
            return h('tr', {}, h('td', { text: r.name }), h('td', { class: 'small', text: r.use }), h('td', {}, r.used ? (st ? stateBadge(st.state) : badge('unknown', 'neutral')) : badge('not used', 'neutral')), h('td', { class: 'small' }, st?.test ? h('div', {}, badge(st.test.ok === null ? 'not tested' : st.test.ok ? 'passed' : 'failed', st.test.ok === null ? 'neutral' : st.test.ok ? 'ok' : 'bad'), h('div', { class: 'muted small', text: st.test.detail })) : badge('not run', 'neutral', 'Press “Test agents now”')), h('td', { class: 'num', text: st ? `${st.calls}${st.failures ? ` (${st.failures} failed)` : ''}` : '–' }), h('td', { class: 'small muted', text: st?.error ?? st?.lastError ?? '' }));
        })))), h('section', { class: 'card' }, sectionTitle('Reading formats'), formatTable(Object.values(c.read))), h('section', { class: 'card' }, sectionTitle('Writing formats'), formatTable(Object.values(c.write))), h('section', { class: 'card' }, sectionTitle('SLAI Reader engines'), h('div', { class: 'chips' }, ...Object.entries(c.reader.engines).map(([n, e]) => badge(`${n}: ${e.available ? 'ready' : 'unavailable'}`, e.available ? 'ok' : 'warn', e.error ?? '')))));
    }
    const el = h('div', { class: 'workspace system-ws' }, h('header', { class: 'ws-head' }, h('h1', { text: 'System' }), h('p', { class: 'muted', text: 'What works on this computer right now. Nothing here is assumed: it is checked when Documaster starts.' })), body);
    void load();
    return { id: 'system', title: 'System', el, activate: () => { if (!agentsStatus)
            void load(); } };
}
//# sourceMappingURL=system.js.map