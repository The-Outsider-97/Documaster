// Shared building blocks: the dashed drop zone, upload handling, job progress, and the document list rows.
import { ApiError } from './api.js';
import { badge, button, errorMessage, h, promptPassword, replace, toast } from './dom.js';
import { formatBytes, formatDuration } from './format.js';
export const DOCUMENT_ACCEPT = '.pdf,.sldc,.docx,.doc,.odt,.rtf,.html,.htm,.md,.markdown,.txt,.xml,.csv';
export const IMAGE_ACCEPT = '.png,.jpg,.jpeg';
/** Upload files one after another, show their progress, ask for passwords of protected files, and return what was added. */
export async function ingest(ctx, files, opts = {}) {
    const added = [];
    for (const file of files) {
        const bar = h('div', { class: 'bar' }, h('div', { class: 'bar-fill' }));
        const row = h('div', { class: 'upload-row' }, h('span', { class: 'upload-name', text: file.name }), bar);
        opts.tray?.appendChild(row);
        const fill = bar.firstElementChild;
        try {
            let doc = await ctx.api.upload(file, file.name, { images: opts.images, onProgress: (f) => { fill.style.width = `${Math.round(f * 100)}%`; } });
            fill.style.width = '100%';
            ctx.docs.set(doc);
            while (doc.locked) {
                const pw = await promptPassword(`${doc.name} is protected`, 'Enter the password to open it. It is kept only in memory while Documaster is open.');
                if (pw === null)
                    break;
                try {
                    doc = await ctx.docs.unlock(doc.id, pw);
                }
                catch (e) {
                    toast(errorMessage(e), 'error');
                }
            }
            added.push(doc);
        }
        catch (e) {
            const msg = e instanceof ApiError ? e.message : errorMessage(e);
            toast(`${file.name}: ${msg}`, 'error');
        }
        finally {
            window.setTimeout(() => row.remove(), 800);
        }
    }
    opts.onDone?.(added);
    return added;
}
export function dropzone(opts) {
    const input = h('input', { attrs: { type: 'file', accept: opts.accept, multiple: opts.multiple !== false, 'aria-hidden': 'true', tabindex: -1 }, class: 'visually-hidden' });
    const tray = h('div', { class: 'upload-tray' });
    const zone = h('div', { class: 'dropzone', attrs: { role: 'button', tabindex: 0, 'aria-label': `${opts.title}. Drop files here or press Enter to browse.` } }, h('div', { class: 'dz-icon', text: '⇪', attrs: { 'aria-hidden': 'true' } }), h('div', { class: 'dz-title', text: opts.title }), h('div', { class: 'dz-sub' }, 'or ', h('span', { class: 'link', text: 'browse' }), opts.hint ? ` · ${opts.hint}` : ''));
    const pick = () => input.click();
    zone.addEventListener('click', pick);
    zone.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        pick();
    } });
    input.addEventListener('change', () => { const f = [...(input.files ?? [])]; input.value = ''; if (f.length)
        opts.onFiles(f); });
    let depth = 0;
    zone.addEventListener('dragenter', (e) => { e.preventDefault(); depth++; zone.classList.add('dragover'); });
    zone.addEventListener('dragover', (e) => { e.preventDefault(); if (e.dataTransfer)
        e.dataTransfer.dropEffect = 'copy'; });
    zone.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth)
        zone.classList.remove('dragover'); });
    zone.addEventListener('drop', (e) => {
        e.preventDefault();
        depth = 0;
        zone.classList.remove('dragover');
        const files = [...(e.dataTransfer?.files ?? [])];
        if (files.length)
            opts.onFiles(files);
    });
    return { el: h('div', { class: 'dz-wrap' }, zone, input, tray), tray };
}
export function progress() {
    const label = h('span', { class: 'prog-label' });
    const msg = h('span', { class: 'prog-msg muted' });
    const fill = h('div', { class: 'bar-fill' });
    const cancel = button('Cancel', () => undefined, 'btn-small');
    const el = h('div', { class: 'progress', hidden: true, attrs: { role: 'status', 'aria-live': 'polite' } }, h('div', { class: 'prog-head' }, label, msg, cancel), h('div', { class: 'bar' }, fill));
    let busy = false;
    return {
        el,
        get busy() { return busy; },
        async track(text, work) {
            busy = true;
            label.textContent = text;
            msg.textContent = '';
            fill.style.width = '35%';
            cancel.disabled = true;
            el.hidden = false;
            try {
                return await work;
            }
            finally {
                busy = false;
                el.hidden = true;
            }
        },
        async run(text, start) {
            busy = true;
            label.textContent = text;
            msg.textContent = '';
            fill.style.width = '2%';
            el.hidden = false;
            let handle = null;
            cancel.disabled = false;
            cancel.onclick = () => { cancel.disabled = true; msg.textContent = 'Cancelling…'; void handle?.cancel(); };
            try {
                handle = await start();
                return await handle.wait((j) => {
                    fill.style.width = `${Math.max(2, Math.round(j.progress * 100))}%`;
                    msg.textContent = j.message && j.state !== 'queued' ? j.message : j.state === 'queued' ? 'Waiting…' : '';
                });
            }
            finally {
                busy = false;
                el.hidden = true;
            }
        },
    };
}
export function docRow(ctx, doc, opts = {}) {
    const meta = [doc.formatLabel, formatBytes(doc.bytes), doc.pages ? `${doc.pages} page${doc.pages === 1 ? '' : 's'}` : null].filter(Boolean).join(' · ');
    const flags = [];
    if (doc.encrypted)
        flags.push(badge(doc.locked ? 'Locked' : 'Protected', doc.locked ? 'warn' : 'info', 'This document is password-protected'));
    if (doc.format === 'pdf' && doc.permissions && !doc.owner) {
        const denied = Object.entries(doc.permissions).filter(([, v]) => !v).map(([k]) => k);
        if (denied.length)
            flags.push(badge('Restricted', 'warn', `The document's permissions do not allow: ${denied.join(', ')}`));
    }
    const actions = [...(opts.actions ?? [])];
    if (doc.locked) {
        actions.unshift(button('Unlock', async () => {
            const pw = await promptPassword(`${doc.name} is protected`, 'Enter the password to open it.');
            if (pw === null)
                return;
            try {
                await ctx.docs.unlock(doc.id, pw);
            }
            catch (e) {
                toast(errorMessage(e), 'error');
            }
        }, 'btn-small'));
    }
    if (opts.remove !== false) {
        actions.push(button('✕', async () => { try {
            await ctx.docs.remove(doc.id);
        }
        catch (e) {
            toast(errorMessage(e), 'error');
        } }, 'btn-icon', { attrs: { 'aria-label': `Remove ${doc.name}`, title: 'Remove from Documaster' } }));
    }
    const row = h('div', { class: `doc-row${opts.selected ? ' selected' : ''}`, data: { id: doc.id } }, h('div', { class: 'doc-main' }, h('div', { class: 'doc-name', text: doc.name, title: doc.name }), h('div', { class: 'doc-meta muted' }, meta, ...flags)), h('div', { class: 'doc-actions' }, ...actions));
    if (doc.warnings.length)
        row.appendChild(h('ul', { class: 'doc-warnings' }, ...doc.warnings.slice(0, 3).map((w) => h('li', { text: w }))));
    if (opts.onClick) {
        row.classList.add('clickable');
        row.addEventListener('click', (e) => { if (!e.target.closest('button'))
            opts.onClick?.(); });
    }
    return row;
}
export function emptyState(title, sub = '') {
    return h('div', { class: 'empty' }, h('div', { class: 'empty-title', text: title }), sub ? h('div', { class: 'muted', text: sub }) : null);
}
export function sectionTitle(text, ...right) {
    return h('div', { class: 'section-title' }, h('h3', { text }), h('div', { class: 'spacer' }), ...right);
}
/** A download link for a produced file (the browser downloads it through the authenticated session). */
export function downloadLink(ctx, out) {
    return h('a', { class: 'btn btn-primary btn-small', text: `Download ${out.name} (${formatBytes(out.bytes)})`, attrs: { href: ctx.api.downloadUrl(out.id), download: out.name } });
}
export function verificationView(v) {
    if (!v)
        return h('div', { class: 'muted', text: 'No verification information.' });
    const ran = v.checks.filter((c) => c.ok !== null).length; // a check that does not apply to this format is listed, not counted
    return h('details', { class: 'verify' }, h('summary', {}, badge(v.ok ? 'Verified' : 'Verification failed', v.ok ? 'ok' : 'bad'), ` ${ran} check${ran === 1 ? '' : 's'}`), h('ul', {}, ...v.checks.map((c) => h('li', { class: c.ok === null ? 'na' : c.ok ? 'ok' : 'bad' }, h('b', { text: `${c.ok === null ? '–' : c.ok ? '✓' : '✗'} ${c.name}` }), c.detail ? ` — ${c.ok === null ? 'not applicable: ' : ''}${c.detail}` : ''))));
}
export function listBlock(title, items, cls = '') {
    if (!items.length)
        return null;
    return h('div', { class: `listblock ${cls}`.trim() }, h('div', { class: 'listblock-title', text: title }), h('ul', {}, ...items.map((i) => h('li', { text: i }))));
}
export { formatDuration, replace };
//# sourceMappingURL=components.js.map