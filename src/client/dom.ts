// A tiny DOM builder. Text always goes in through textContent / text nodes - never innerHTML - so nothing that comes from a
// document (names, text, metadata) can inject markup.

export type Child = Node | string | number | null | undefined | false;
export interface Props {
  class?: string;
  id?: string;
  text?: string;
  title?: string;
  attrs?: Record<string, string | number | boolean | undefined>;
  data?: Record<string, string | number | undefined>;
  style?: Partial<CSSStyleDeclaration> | Record<string, string>;
  on?: { [K in keyof HTMLElementEventMap]?: (ev: HTMLElementEventMap[K]) => void };
  value?: string;
  checked?: boolean;
  disabled?: boolean;
  hidden?: boolean;
}

export function h<K extends keyof HTMLElementTagNameMap>(tag: K, props: Props = {}, ...children: Child[]): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  if (props.class) el.className = props.class;
  if (props.id) el.id = props.id;
  if (props.title) el.title = props.title;
  if (props.text !== undefined) el.textContent = props.text;
  if (props.attrs) {
    for (const [k, v] of Object.entries(props.attrs)) {
      if (v === undefined || v === false) continue;
      el.setAttribute(k, v === true ? '' : String(v));
    }
  }
  if (props.data) for (const [k, v] of Object.entries(props.data)) if (v !== undefined) el.dataset[k] = String(v);
  if (props.style) {
    for (const [k, v] of Object.entries(props.style)) {
      if (v === undefined) continue;
      if (k.startsWith('--') || k.includes('-')) el.style.setProperty(k, String(v));
      else (el.style as unknown as Record<string, string>)[k] = String(v);
    }
  }
  if (props.on) for (const [k, fn] of Object.entries(props.on)) el.addEventListener(k, fn as EventListener);
  if (props.value !== undefined && 'value' in el) (el as unknown as HTMLInputElement).value = props.value;
  if (props.checked !== undefined && 'checked' in el) (el as unknown as HTMLInputElement).checked = props.checked;
  if (props.disabled !== undefined && 'disabled' in el) (el as unknown as HTMLButtonElement).disabled = props.disabled;
  if (props.hidden) el.hidden = true;
  append(el, children);
  return el;
}

export function append(parent: Node, children: Child[]): void {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    parent.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
  }
}

export function clear(el: Node): void {
  while (el.firstChild) el.removeChild(el.firstChild);
}

export function replace(el: Node, ...children: Child[]): void {
  clear(el);
  append(el, children);
}

export function button(label: string, onClick: () => void, cls = '', extra: Props = {}): HTMLButtonElement {
  return h('button', { ...extra, class: `btn ${cls}`.trim(), text: label, attrs: { type: 'button', ...(extra.attrs ?? {}) }, on: { click: onClick } });
}

export function field(label: string, control: HTMLElement, hint?: string): HTMLLabelElement {
  return h('label', { class: 'field' }, h('span', { class: 'field-label', text: label }), control, hint ? h('span', { class: 'hint', text: hint }) : null);
}

export function select(options: { value: string; label: string; disabled?: boolean; title?: string }[], value: string, onChange?: (v: string) => void): HTMLSelectElement {
  const el = h('select', { class: 'input' });
  for (const o of options) {
    const opt = h('option', { text: o.label, attrs: { value: o.value, disabled: o.disabled, title: o.title } });
    el.appendChild(opt);
  }
  el.value = value;
  if (onChange) el.addEventListener('change', () => onChange(el.value));
  return el;
}

export function checkbox(label: string, checked: boolean, onChange?: (v: boolean) => void, hint?: string): HTMLLabelElement {
  const box = h('input', { attrs: { type: 'checkbox' }, checked });
  if (onChange) box.addEventListener('change', () => onChange(box.checked));
  return h('label', { class: 'check' }, box, h('span', { text: label }), hint ? h('span', { class: 'hint', text: hint }) : null);
}

export function badge(text: string, kind: 'neutral' | 'ok' | 'warn' | 'bad' | 'info' = 'neutral', title?: string): HTMLSpanElement {
  return h('span', { class: `badge badge-${kind}`, text, title });
}

// ---- toasts and dialogs --------------------------------------------------------------------------------------------
let toastHost: HTMLElement | null = null;
export function toast(message: string, kind: 'info' | 'ok' | 'error' = 'info', ms = 5000): void {
  if (!toastHost) {
    toastHost = h('div', { class: 'toasts', attrs: { 'aria-live': 'polite' } });
    document.body.appendChild(toastHost);
  }
  const t = h('div', { class: `toast toast-${kind}`, text: message, attrs: { role: kind === 'error' ? 'alert' : 'status' } });
  toastHost.appendChild(t);
  window.setTimeout(() => t.remove(), kind === 'error' ? Math.max(ms, 8000) : ms);
}

export interface DialogHandle { close(): void; el: HTMLElement }

/** A modal dialog. ``onClose`` runs once however the dialog ends (button, Escape, backdrop click, close()). */
export function dialog(title: string, body: HTMLElement, actions: { label: string; kind?: string; run: () => void | boolean | Promise<void | boolean> }[] = [],
                       opts: { wide?: boolean; onClose?: () => void } = {}): DialogHandle {
  const previous = document.activeElement as HTMLElement | null;
  let closed = false;
  const close = (): void => {
    if (closed) return;
    closed = true;
    overlay.remove();
    previous?.focus?.();
    opts.onClose?.();
  };
  const buttons = actions.map((a) => button(a.label, async () => { const r = await a.run(); if (r !== false) close(); }, a.kind ?? ''));
  const box = h('div', { class: `dialog${opts.wide ? ' dialog-wide' : ''}`, attrs: { role: 'dialog', 'aria-modal': 'true', 'aria-label': title } },
    h('div', { class: 'dialog-head' }, h('h2', { text: title }), button('✕', close, 'btn-icon', { attrs: { 'aria-label': 'Close' } })),
    h('div', { class: 'dialog-body' }, body),
    buttons.length ? h('div', { class: 'dialog-actions' }, ...buttons) : null);
  const overlay = h('div', { class: 'overlay', on: { mousedown: (e) => { if (e.target === overlay) close(); } } }, box);
  overlay.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  document.body.appendChild(overlay);
  (box.querySelector('input,select,textarea,button.btn-primary') as HTMLElement | null)?.focus();
  return { close, el: box };
}

export function confirmDialog(title: string, message: string, confirmLabel: string, danger = false): Promise<boolean> {
  return new Promise((resolve) => {
    let answer = false;
    dialog(title, h('p', { text: message }), [
      { label: 'Cancel', run: () => { answer = false; } },
      { label: confirmLabel, kind: danger ? 'btn-danger' : 'btn-primary', run: () => { answer = true; } },
    ], { onClose: () => resolve(answer) });
  });
}

export function promptPassword(title: string, message: string): Promise<string | null> {
  return new Promise((resolve) => {
    let answer: string | null = null;
    const input = h('input', { class: 'input', attrs: { type: 'password', autocomplete: 'off', 'aria-label': 'Password' } });
    const d = dialog(title, h('div', {}, h('p', { text: message }), input), [
      { label: 'Cancel', run: () => { answer = null; } },
      { label: 'OK', kind: 'btn-primary', run: () => { if (!input.value) return false; answer = input.value; } },
    ], { onClose: () => resolve(answer) });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && input.value) { answer = input.value; d.close(); } });
  });
}

export function errorMessage(e: unknown): string {
  if (e && typeof e === 'object' && 'message' in e) return String((e as { message: unknown }).message);
  return String(e);
}

/** Debounce a function (trailing). */
export function debounce<A extends unknown[]>(fn: (...a: A) => void, ms: number): (...a: A) => void {
  let t: number | undefined;
  return (...a: A) => { window.clearTimeout(t); t = window.setTimeout(() => fn(...a), ms); };
}

/** Ask for a piece of text in a dialog. Resolves with the text, or null if cancelled. */
export function promptText(title: string, label: string, initial = '', multiline = true): Promise<string | null> {
  return new Promise((resolve) => {
    let answer: string | null = null;
    const input = multiline
      ? h('textarea', { class: 'input', attrs: { rows: 4, 'aria-label': label }, value: initial })
      : h('input', { class: 'input', attrs: { type: 'text', 'aria-label': label }, value: initial });
    const d = dialog(title, h('label', { class: 'field' }, h('span', { class: 'field-label', text: label }), input), [
      { label: 'Cancel', run: () => { answer = null; } },
      { label: 'OK', kind: 'btn-primary', run: () => { if (!input.value.trim()) return false; answer = input.value; } },
    ], { onClose: () => resolve(answer) });
    if (!multiline) input.addEventListener('keydown', (e) => { if ((e as KeyboardEvent).key === 'Enter' && input.value.trim()) { answer = input.value; d.close(); } });
  });
}
