// Shared state of the PDF editor: the open document, the current page, staged (not yet applied) marks, and the way edits are run.
import { ApiError } from '../api.js';
import { button, errorMessage, toast } from '../dom.js';
import { Emitter } from '../store.js';
export class EditorCore {
    ctx;
    progress;
    doc = null;
    info = null;
    page = 0;
    tool = 'view';
    zoom = 1; // 1 = fit width
    style = { color: '#e11d48', width: 2, opacity: 1, fill: '' };
    hits = [];
    hitIndex = -1;
    staged = [];
    selectedStaged = null;
    pendingAsset = null;
    /** Settings of the text stamp placed by clicking on the page. */
    stampForm = { text: '', size: 24, color: '#dc2626', opacity: 1, rotation: 0 };
    busy = false;
    seq = 0;
    changed = new Emitter();
    constructor(ctx, progress) {
        this.ctx = ctx;
        this.progress = progress;
    }
    get pages() { return this.doc?.pages ?? 0; }
    get canEdit() { return !!this.doc && !this.doc.locked; }
    can(permission) {
        const d = this.doc;
        if (!d)
            return false;
        return !d.encrypted || d.owner || !d.permissions || d.permissions[permission] !== false;
    }
    async open(doc, page = 0) {
        this.doc = doc;
        this.page = Math.max(0, Math.min(page, Math.max(0, (doc.pages ?? 1) - 1)));
        this.staged = [];
        this.hits = [];
        this.hitIndex = -1;
        this.info = null;
        this.changed.emit('doc');
        await this.loadInfo();
    }
    close() {
        this.doc = null;
        this.info = null;
        this.staged = [];
        this.hits = [];
        this.changed.emit('doc');
    }
    async loadInfo() {
        const d = this.doc;
        if (!d || d.locked)
            return;
        try {
            this.info = await this.ctx.api.get(`/api/documents/${d.id}/pdfinfo`);
        }
        catch (e) {
            this.info = null;
            toast(errorMessage(e), 'error');
        }
        this.changed.emit('info');
    }
    setPage(n) {
        const p = Math.max(0, Math.min(n, this.pages - 1));
        if (p === this.page)
            return;
        this.page = p;
        this.changed.emit('page');
    }
    setTool(t) { this.tool = t; this.selectedStaged = null; this.changed.emit('tool'); }
    setZoom(z) { this.zoom = Math.max(0.25, Math.min(4, z)); this.changed.emit('page'); }
    // ---- staged marks ------------------------------------------------------------------------------------------------------
    stage(item) {
        const id = ++this.seq;
        this.staged.push({ ...item, id });
        this.changed.emit('staged');
        return id;
    }
    unstage(id) {
        this.staged = this.staged.filter((s) => s.id !== id);
        if (this.selectedStaged === id)
            this.selectedStaged = null;
        this.changed.emit('staged');
    }
    clearStaged(kind) {
        this.staged = kind ? this.staged.filter((s) => s.kind !== kind) : [];
        this.changed.emit('staged');
    }
    stagedOf(kind) {
        return this.staged.filter((s) => s.kind === kind);
    }
    setHits(hits) { this.hits = hits; this.hitIndex = hits.length ? 0 : -1; this.changed.emit('hits'); }
    // ---- running edits ------------------------------------------------------------------------------------------------------
    /** Run an edit as a job. Updates the document, reloads its details, and returns the result (or null if it failed). */
    async run(spec, label, opts = {}) {
        const d = this.doc;
        if (!d || this.busy)
            return null;
        this.busy = true;
        this.changed.emit('busy');
        try {
            const r = await this.progress.run(label, () => this.ctx.api.startJob(`/api/documents/${d.id}/op`, spec));
            this.ctx.docs.set(r.document);
            this.doc = r.document;
            if (!opts.keepStaged)
                this.staged = [];
            this.hits = [];
            this.hitIndex = -1;
            if (this.page >= (r.document.pages ?? 1))
                this.page = Math.max(0, (r.document.pages ?? 1) - 1);
            await this.loadInfo();
            this.changed.emit('doc');
            this.changed.emit('staged');
            if (!opts.quiet)
                toast(r.changed ? r.label : r.label || 'Nothing changed', r.changed ? 'ok' : 'info');
            for (const w of r.warnings.slice(0, 2))
                toast(w, 'info', 9000);
            return r;
        }
        catch (e) {
            if (e instanceof ApiError && e.cancelled)
                toast('Cancelled');
            else
                toast(errorMessage(e), 'error');
            return null;
        }
        finally {
            this.busy = false;
            this.changed.emit('busy');
        }
    }
    async step(direction) {
        const d = this.doc;
        if (!d)
            return;
        try {
            const nd = await this.ctx.api.post(`/api/documents/${d.id}/${direction}`);
            this.ctx.docs.set(nd);
            this.doc = nd;
            this.staged = [];
            this.hits = [];
            if (this.page >= (nd.pages ?? 1))
                this.page = Math.max(0, (nd.pages ?? 1) - 1);
            await this.loadInfo();
            this.changed.emit('doc');
            this.changed.emit('staged');
        }
        catch (e) {
            toast(errorMessage(e), 'error');
        }
    }
    async goto(revision) {
        const d = this.doc;
        if (!d)
            return;
        try {
            const nd = await this.ctx.api.post(`/api/documents/${d.id}/revision/${revision}`);
            this.ctx.docs.set(nd);
            this.doc = nd;
            this.staged = [];
            await this.loadInfo();
            this.changed.emit('doc');
            this.changed.emit('staged');
        }
        catch (e) {
            toast(errorMessage(e), 'error');
        }
    }
    /** Refresh our copy of the document after something else changed it. */
    sync(all) {
        if (!this.doc)
            return;
        const fresh = all.find((d) => d.id === this.doc.id);
        if (!fresh) {
            this.close();
            return;
        }
        if (fresh.sha256 !== this.doc.sha256 || fresh.locked !== this.doc.locked || fresh.revision.current !== this.doc.revision.current) {
            this.doc = fresh;
            void this.loadInfo().then(() => this.changed.emit('doc'));
        }
        else
            this.doc = fresh;
    }
}
export function tinyButton(label, title, fn) {
    return button(label, fn, 'btn-small', { title, attrs: { 'aria-label': title } });
}
//# sourceMappingURL=editor-core.js.map