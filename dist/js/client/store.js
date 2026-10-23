export class Emitter {
    listeners = new Set();
    on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
    emit(v) { for (const fn of [...this.listeners])
        fn(v); }
}
export class Documents {
    api;
    docs = new Map();
    changed = new Emitter();
    constructor(api) {
        this.api = api;
    }
    list() { return [...this.docs.values()]; }
    get(id) { return this.docs.get(id); }
    pdfs() { return this.list().filter((d) => d.format === 'pdf' && !d.locked); }
    set(doc) { this.docs.set(doc.id, doc); this.changed.emit(this.list()); }
    async refresh() {
        const list = await this.api.get('/api/documents');
        this.docs = new Map(list.map((d) => [d.id, d]));
        this.changed.emit(this.list());
    }
    async remove(id) {
        await this.api.del(`/api/documents/${id}`);
        this.docs.delete(id);
        this.changed.emit(this.list());
    }
    async unlock(id, password) {
        const doc = await this.api.post(`/api/documents/${id}/unlock`, { password });
        this.set(doc);
        return doc;
    }
}
export const THEME_KEY = 'documaster.theme';
export function loadTheme() {
    try {
        const v = localStorage.getItem(THEME_KEY);
        if (v === 'light' || v === 'dark')
            return v;
    }
    catch { /* storage can be unavailable */ }
    return 'dark';
}
export function saveTheme(t) {
    try {
        localStorage.setItem(THEME_KEY, t);
    }
    catch { /* ignore */ }
}
//# sourceMappingURL=store.js.map