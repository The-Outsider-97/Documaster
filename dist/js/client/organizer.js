// The page organiser: an ordered list of page instances with selection, include/exclude, rotation, duplication and undo/redo.
// Pure state (no DOM) so it can be tested; the Merger and the PDF editor's page panel both render from it.
import { parseRanges } from './format.js';
export class Organizer {
    items = [];
    selected = new Set();
    past = [];
    future = [];
    seq = 0;
    listeners = new Set();
    limit;
    constructor(limit = 100) { this.limit = limit; }
    onChange(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
    emit() { for (const fn of [...this.listeners])
        fn(); }
    newUid() { return `p${++this.seq}`; }
    // ---- history -----------------------------------------------------------------------------------------------------
    snapshot() { return { items: this.items.map((i) => ({ ...i })), selected: [...this.selected] }; }
    restore(s) { this.items = s.items.map((i) => ({ ...i })); this.selected = new Set(s.selected); }
    commit(change) {
        const before = this.snapshot();
        if (change() === false)
            return false;
        this.past.push(before);
        if (this.past.length > this.limit)
            this.past.shift();
        this.future = [];
        this.emit();
        return true;
    }
    get canUndo() { return this.past.length > 0; }
    get canRedo() { return this.future.length > 0; }
    undo() {
        const s = this.past.pop();
        if (!s)
            return false;
        this.future.push(this.snapshot());
        this.restore(s);
        this.emit();
        return true;
    }
    redo() {
        const s = this.future.pop();
        if (!s)
            return false;
        this.past.push(this.snapshot());
        this.restore(s);
        this.emit();
        return true;
    }
    /** Replace the whole list (fresh load); history is cleared. */
    load(items) {
        this.items = items.map((i) => ({ ...i, uid: this.newUid() }));
        this.selected.clear();
        this.past = [];
        this.future = [];
        this.emit();
    }
    // ---- selection (not part of history) -----------------------------------------------------------------------------
    indexOf(uid) { return this.items.findIndex((i) => i.uid === uid); }
    anchor = null;
    selectOnly(uid) { this.selected = new Set([uid]); this.anchor = uid; this.emit(); }
    toggleSelect(uid) {
        if (this.selected.has(uid))
            this.selected.delete(uid);
        else
            this.selected.add(uid);
        this.anchor = uid;
        this.emit();
    }
    /** Shift-click: select everything between the anchor and ``uid``. */
    selectThrough(uid) {
        const a = this.anchor ? this.indexOf(this.anchor) : -1;
        const b = this.indexOf(uid);
        if (a < 0 || b < 0) {
            this.selectOnly(uid);
            return;
        }
        const [lo, hi] = a < b ? [a, b] : [b, a];
        this.selected = new Set(this.items.slice(lo, hi + 1).map((i) => i.uid));
        this.emit();
    }
    selectAll() { this.selected = new Set(this.items.map((i) => i.uid)); this.emit(); }
    selectNone() { this.selected.clear(); this.emit(); }
    invertSelection() { this.selected = new Set(this.items.filter((i) => !this.selected.has(i.uid)).map((i) => i.uid)); this.emit(); }
    selectDoc(docId) { this.selected = new Set(this.items.filter((i) => i.docId === docId).map((i) => i.uid)); this.emit(); }
    selectExcluded() { this.selected = new Set(this.items.filter((i) => !i.included).map((i) => i.uid)); this.emit(); }
    /** Select by position in the current order: "1-3, 7". Returns false if the text is not a valid range list. */
    selectRanges(text) {
        const pos = parseRanges(text, this.items.length);
        if (pos === null)
            return false;
        this.selected = new Set(pos.map((p) => this.items[p - 1].uid));
        this.emit();
        return true;
    }
    selectedItems() { return this.items.filter((i) => this.selected.has(i.uid)); }
    position(uid) { return this.indexOf(uid) + 1; }
    // ---- changes ---------------------------------------------------------------------------------------------------------
    /** Move the selection one place towards the start (selected blocks move together; the top of the list stops them). */
    moveUp() {
        return this.commit(() => {
            let moved = false;
            for (let i = 1; i < this.items.length; i++) {
                if (this.selected.has(this.items[i].uid) && !this.selected.has(this.items[i - 1].uid)) {
                    [this.items[i - 1], this.items[i]] = [this.items[i], this.items[i - 1]];
                    moved = true;
                }
            }
            return moved;
        });
    }
    moveDown() {
        return this.commit(() => {
            let moved = false;
            for (let i = this.items.length - 2; i >= 0; i--) {
                if (this.selected.has(this.items[i].uid) && !this.selected.has(this.items[i + 1].uid)) {
                    [this.items[i + 1], this.items[i]] = [this.items[i], this.items[i + 1]];
                    moved = true;
                }
            }
            return moved;
        });
    }
    moveToStart() { return this.moveBlock(0); }
    moveToEnd() { return this.moveBlock(this.items.length); }
    /** Put the selected items (in their current order) before the item ``beforeUid``; ``null`` puts them at the end. Used by drag and drop. */
    dropBefore(beforeUid, uids = [...this.selected]) {
        const moving = new Set(uids);
        if (!moving.size || (beforeUid !== null && moving.has(beforeUid)))
            return false;
        return this.commit(() => {
            const block = this.items.filter((i) => moving.has(i.uid));
            const rest = this.items.filter((i) => !moving.has(i.uid));
            let at = beforeUid === null ? rest.length : rest.findIndex((i) => i.uid === beforeUid);
            if (at < 0)
                at = rest.length;
            const next = [...rest.slice(0, at), ...block, ...rest.slice(at)];
            const same = next.every((i, k) => i.uid === this.items[k].uid);
            if (same)
                return false;
            this.items = next;
            return true;
        });
    }
    moveBlock(index) {
        const block = this.selectedItems();
        if (!block.length)
            return false;
        const rest = this.items.filter((i) => !this.selected.has(i.uid));
        const before = index === 0 ? rest[0]?.uid ?? null : null;
        return index === 0 ? (before === null ? false : this.dropBefore(before)) : this.dropBefore(null);
    }
    /** Include or exclude the selected pages. If every selected page is included they are excluded; otherwise all become included. */
    toggleIncluded() {
        const sel = this.selectedItems();
        if (!sel.length)
            return false;
        const to = !sel.every((i) => i.included);
        return this.setIncluded(sel.map((i) => i.uid), to);
    }
    setIncluded(uids, included) {
        const set = new Set(uids);
        return this.commit(() => {
            let changed = false;
            for (const i of this.items)
                if (set.has(i.uid) && i.included !== included) {
                    i.included = included;
                    changed = true;
                }
            return changed;
        });
    }
    /** Rotate the selected pages; ``only`` limits it to pages that can be rotated. */
    rotateSelected(delta, only = () => true) {
        return this.commit(() => {
            const sel = this.selectedItems().filter(only);
            if (!sel.length)
                return false;
            for (const i of sel)
                i.rotation = (((i.rotation + delta) % 360) + 360) % 360;
            return true;
        });
    }
    /** Duplicate each selected page right after itself; the copies become the selection. */
    duplicateSelected() {
        return this.commit(() => {
            if (!this.selected.size)
                return false;
            const out = [];
            const copies = new Set();
            for (const i of this.items) {
                out.push(i);
                if (this.selected.has(i.uid)) {
                    const c = { ...i, uid: this.newUid() };
                    copies.add(c.uid);
                    out.push(c);
                }
            }
            this.items = out;
            this.selected = copies;
            return true;
        });
    }
    removeSelected() {
        return this.commit(() => {
            if (!this.selected.size)
                return false;
            this.items = this.items.filter((i) => !this.selected.has(i.uid));
            this.selected.clear();
            return true;
        });
    }
    insertBlankAfter(uid, size) {
        return this.commit(() => {
            const idx = uid === null ? this.items.length - 1 : this.indexOf(uid);
            const ref = this.items[idx];
            const blank = { uid: this.newUid(), docId: '', page: -1, blank: true, rotation: 0, included: true, label: 'Blank page', docName: '',
                width: size?.width ?? ref?.width, height: size?.height ?? ref?.height };
            this.items.splice(idx + 1, 0, blank);
            this.selected = new Set([blank.uid]);
            return true;
        });
    }
    reverse() {
        return this.commit(() => {
            if (this.items.length < 2)
                return false;
            this.items.reverse();
            return true;
        });
    }
    // ---- output ----------------------------------------------------------------------------------------------------------
    includedCount() { return this.items.filter((i) => i.included).length; }
    /** The plan sent to the server; ``docIds`` is the document list sent with it (the plan refers to documents by position in it).
     *  Rotation is dropped when ``rotation`` is false (output formats that cannot rotate pages). */
    toPlan(docIds, opts = {}) {
        const rot = opts.rotation !== false;
        return this.items.map((i) => {
            if (i.blank) {
                const e = { blank: true, included: i.included };
                if (i.width && i.height) {
                    e.width = i.width;
                    e.height = i.height;
                }
                return e;
            }
            return { doc: docIds.indexOf(i.docId), page: i.page, rotation: rot ? i.rotation : 0, included: i.included };
        });
    }
    /** Is the list exactly the documents in order, every page once, nothing rotated or excluded? */
    isUntouched(docs) {
        let k = 0;
        for (const d of docs) {
            for (let p = 0; p < d.pages; p++) {
                const i = this.items[k++];
                if (!i || i.blank || i.docId !== d.id || i.page !== p || i.rotation !== 0 || !i.included)
                    return false;
            }
        }
        return k === this.items.length;
    }
}
/** Remove every page of the documents that are no longer open (history is kept; this is not an undoable user action). */
export function dropMissing(o, alive) {
    const keep = o.items.filter((i) => i.blank || alive.has(i.docId));
    if (keep.length !== o.items.length) {
        o.items = keep;
        o.selected = new Set([...o.selected].filter((u) => keep.some((i) => i.uid === u)));
    }
}
//# sourceMappingURL=organizer.js.map