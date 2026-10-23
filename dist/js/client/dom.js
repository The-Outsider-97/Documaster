// A tiny DOM builder. Text always goes in through textContent / text nodes - never innerHTML - so nothing that comes from a
// document (names, text, metadata) can inject markup.
export function h(tag, props = {}, ...children) {
    const el = document.createElement(tag);
    if (props.class)
        el.className = props.class;
    if (props.id)
        el.id = props.id;
    if (props.title)
        el.title = props.title;
    if (props.text !== undefined)
        el.textContent = props.text;
    if (props.attrs) {
        for (const [k, v] of Object.entries(props.attrs)) {
            if (v === undefined || v === false)
                continue;
            el.setAttribute(k, v === true ? '' : String(v));
        }
    }
    if (props.data)
        for (const [k, v] of Object.entries(props.data))
            if (v !== undefined)
                el.dataset[k] = String(v);
    if (props.style) {
        for (const [k, v] of Object.entries(props.style)) {
            if (v === undefined)
                continue;
            if (k.startsWith('--') || k.includes('-'))
                el.style.setProperty(k, String(v));
            else
                el.style[k] = String(v);
        }
    }
    if (props.on)
        for (const [k, fn] of Object.entries(props.on))
            el.addEventListener(k, fn);
    if (props.value !== undefined && 'value' in el)
        el.value = props.value;
    if (props.checked !== undefined && 'checked' in el)
        el.checked = props.checked;
    if (props.disabled !== undefined && 'disabled' in el)
        el.disabled = props.disabled;
    if (props.hidden)
        el.hidden = true;
    append(el, children);
    return el;
}
export function append(parent, children) {
    for (const c of children) {
        if (c === null || c === undefined || c === false)
            continue;
        parent.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
    }
}
export function clear(el) {
    while (el.firstChild)
        el.removeChild(el.firstChild);
}
export function replace(el, ...children) {
    clear(el);
    append(el, children);
}
export function button(label, onClick, cls = '', extra = {}) {
    return h('button', { ...extra, class: `btn ${cls}`.trim(), text: label, attrs: { type: 'button', ...(extra.attrs ?? {}) }, on: { click: onClick } });
}
export function field(label, control, hint) {
    return h('label', { class: 'field' }, h('span', { class: 'field-label', text: label }), control, hint ? h('span', { class: 'hint', text: hint }) : null);
}
export function select(options, value, onChange) {
    const el = h('select', { class: 'input' });
    for (const o of options) {
        const opt = h('option', { text: o.label, attrs: { value: o.value, disabled: o.disabled, title: o.title } });
        el.appendChild(opt);
    }
    el.value = value;
    if (onChange)
        el.addEventListener('change', () => onChange(el.value));
    return el;
}
export function checkbox(label, checked, onChange, hint) {
    const box = h('input', { attrs: { type: 'checkbox' }, checked });
    if (onChange)
        box.addEventListener('change', () => onChange(box.checked));
    return h('label', { class: 'check' }, box, h('span', { text: label }), hint ? h('span', { class: 'hint', text: hint }) : null);
}
export function badge(text, kind = 'neutral', title) {
    return h('span', { class: `badge badge-${kind}`, text, title });
}
// ---- toasts and dialogs --------------------------------------------------------------------------------------------
let toastHost = null;
export function toast(message, kind = 'info', ms = 5000) {
    if (!toastHost) {
        toastHost = h('div', { class: 'toasts', attrs: { 'aria-live': 'polite' } });
        document.body.appendChild(toastHost);
    }
    const t = h('div', { class: `toast toast-${kind}`, text: message, attrs: { role: kind === 'error' ? 'alert' : 'status' } });
    toastHost.appendChild(t);
    window.setTimeout(() => t.remove(), kind === 'error' ? Math.max(ms, 8000) : ms);
}
/** A modal dialog. ``onClose`` runs once however the dialog ends (button, Escape, backdrop click, close()). */
export function dialog(title, body, actions = [], opts = {}) {
    const previous = document.activeElement;
    let closed = false;
    const close = () => {
        if (closed)
            return;
        closed = true;
        overlay.remove();
        previous?.focus?.();
        opts.onClose?.();
    };
    const buttons = actions.map((a) => button(a.label, async () => { const r = await a.run(); if (r !== false)
        close(); }, a.kind ?? ''));
    const box = h('div', { class: `dialog${opts.wide ? ' dialog-wide' : ''}`, attrs: { role: 'dialog', 'aria-modal': 'true', 'aria-label': title } }, h('div', { class: 'dialog-head' }, h('h2', { text: title }), button('✕', close, 'btn-icon', { attrs: { 'aria-label': 'Close' } })), h('div', { class: 'dialog-body' }, body), buttons.length ? h('div', { class: 'dialog-actions' }, ...buttons) : null);
    const overlay = h('div', { class: 'overlay', on: { mousedown: (e) => { if (e.target === overlay)
                close(); } } }, box);
    overlay.addEventListener('keydown', (e) => { if (e.key === 'Escape')
        close(); });
    document.body.appendChild(overlay);
    box.querySelector('input,select,textarea,button.btn-primary')?.focus();
    return { close, el: box };
}
export function confirmDialog(title, message, confirmLabel, danger = false) {
    return new Promise((resolve) => {
        let answer = false;
        dialog(title, h('p', { text: message }), [
            { label: 'Cancel', run: () => { answer = false; } },
            { label: confirmLabel, kind: danger ? 'btn-danger' : 'btn-primary', run: () => { answer = true; } },
        ], { onClose: () => resolve(answer) });
    });
}
export function promptPassword(title, message) {
    return new Promise((resolve) => {
        let answer = null;
        const input = h('input', { class: 'input', attrs: { type: 'password', autocomplete: 'off', 'aria-label': 'Password' } });
        const d = dialog(title, h('div', {}, h('p', { text: message }), input), [
            { label: 'Cancel', run: () => { answer = null; } },
            { label: 'OK', kind: 'btn-primary', run: () => { if (!input.value)
                    return false; answer = input.value; } },
        ], { onClose: () => resolve(answer) });
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && input.value) {
            answer = input.value;
            d.close();
        } });
    });
}
export function errorMessage(e) {
    if (e && typeof e === 'object' && 'message' in e)
        return String(e.message);
    return String(e);
}
/** Debounce a function (trailing). */
export function debounce(fn, ms) {
    let t;
    return (...a) => { window.clearTimeout(t); t = window.setTimeout(() => fn(...a), ms); };
}
/** Ask for a piece of text in a dialog. Resolves with the text, or null if cancelled. */
export function promptText(title, label, initial = '', multiline = true) {
    return new Promise((resolve) => {
        let answer = null;
        const input = multiline
            ? h('textarea', { class: 'input', attrs: { rows: 4, 'aria-label': label }, value: initial })
            : h('input', { class: 'input', attrs: { type: 'text', 'aria-label': label }, value: initial });
        const d = dialog(title, h('label', { class: 'field' }, h('span', { class: 'field-label', text: label }), input), [
            { label: 'Cancel', run: () => { answer = null; } },
            { label: 'OK', kind: 'btn-primary', run: () => { if (!input.value.trim())
                    return false; answer = input.value; } },
        ], { onClose: () => resolve(answer) });
        if (!multiline)
            input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && input.value.trim()) {
                answer = input.value;
                d.close();
            } });
    });
}
//# sourceMappingURL=dom.js.map