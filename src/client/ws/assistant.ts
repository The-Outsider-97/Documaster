// AI Assistant: asks about and works on the open documents. Answers are quoted from the documents with their positions; anything that
// would change or create a file is shown as a proposal first and only happens when you confirm it.
import { downloadLink, listBlock, progress, verificationView, type Progress } from '../components.js';
import { ApiError } from '../api.js';
import { badge, button, errorMessage, h, replace, toast } from '../dom.js';
import { formatBytes } from '../format.js';
import type { AppContext } from '../store.js';
import type { AgentNote, AssistantReply, ConvertResult, Evidence, MergeResult, OpResult, Proposal } from '../types.js';
import type { Workspace } from './types.js';

interface Tool { name: string; title: string; description: string; changesFiles: boolean }

export function createAssistant(ctx: AppContext): Workspace {
  const log = h('div', { class: 'chat-log', attrs: { role: 'log', 'aria-live': 'polite', 'aria-label': 'Conversation' } });
  const input = h('textarea', { class: 'input chat-input', attrs: { rows: 2, placeholder: 'Ask about a document, or tell me what to do: “summarise report.pdf”, “redact the email addresses”, “merge these into one PDF”…', 'aria-label': 'Message' } });
  const send = button('Send', () => void submit(), 'btn-primary');
  const docSelect = h('select', { class: 'input', attrs: { 'aria-label': 'Document I am working with' } });
  const chips = h('div', { class: 'chips suggestions' });
  const prog = progress();
  let sending = false;
  let tools: Tool[] = [];

  const scroll = (): void => { log.scrollTop = log.scrollHeight; };
  const user = (text: string): void => { log.appendChild(h('div', { class: 'msg user' }, h('div', { class: 'bubble', text }))); scroll(); };

  function refCell(e: Evidence): HTMLElement {
    const doc = ctx.docs.get(e.docId);
    const label = `${e.doc}${e.page ? `, p. ${e.page}` : `, part ${e.part}`}`;
    const quote = e.quote ? h('span', { class: 'quote', text: `“${e.quote}”` }) : null;
    if (doc?.format === 'pdf' && e.page) return h('div', { class: 'ev' }, h('button', { class: 'link-btn', text: label, attrs: { type: 'button' }, on: { click: () => ctx.open('editor', e.docId, e.page ?? 1) } }), quote);
    return h('div', { class: 'ev' }, h('span', { class: 'muted', text: label }), quote);
  }

  function agentNotes(notes: AgentNote[]): HTMLElement | null {
    if (!notes.length) return null;
    return h('div', { class: 'agent-notes' }, ...notes.map((n) => h('div', { class: 'agent-note' }, badge(`${n.role}${n.agent ? ` (${n.agent})` : ''}: ${n.status}`, n.status === 'used' ? 'ok' : n.status === 'failed' ? 'bad' : 'neutral'), n.reason ? h('span', { class: 'muted small', text: ` ${n.reason}` }) : null)));
  }

  function proposalCard(p: Proposal, holder: HTMLElement): HTMLElement {
    const inputs: Record<string, string> = {};
    const fields = p.needs.map((n) => h('label', { class: 'field' }, h('span', { class: 'field-label', text: n[0]!.toUpperCase() + n.slice(1) }),
      h('input', { class: 'input', attrs: { type: n === 'password' ? 'password' : 'text', autocomplete: 'new-password' }, on: { input: (e) => { inputs[n] = (e.target as HTMLInputElement).value; } } })));
    const confirmBtn = button('Confirm', async () => {
      confirmBtn.disabled = cancelBtn.disabled = true;
      await runProposal(p, inputs, holder);
    }, 'btn-primary btn-small');
    const cancelBtn = button('Cancel', async () => { try { await ctx.api.post('/api/assistant/reject', { id: p.id }); } catch { /* already gone */ } replace(holder, h('div', { class: 'muted small', text: 'Cancelled. Nothing was changed.' })); }, 'btn-small');
    return h('div', { class: 'proposal card' }, h('div', { class: 'card-head' }, badge('Needs your confirmation', 'warn'), h('h4', { text: p.title })), h('p', { text: p.description }), listBlock('Please note', p.consequences), ...fields, h('div', { class: 'row-actions' }, confirmBtn, cancelBtn));
  }

  async function runProposal(p: Proposal, inputs: Record<string, string>, holder: HTMLElement): Promise<void> {
    try {
      const result = await prog.run<unknown>(p.title, () => ctx.api.startJob('/api/assistant/confirm', { id: p.id, inputs }));
      replace(holder, resultView(p, result));
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : errorMessage(e);
      replace(holder, h('div', { class: 'error', text: msg }));
      toast(msg, 'error');
    }
    scroll();
  }

  function resultView(p: Proposal, result: unknown): HTMLElement {
    if (p.tool === 'convert') {
      const r = result as ConvertResult;
      return h('div', { class: 'result-box' }, h('div', { text: `${r.succeeded} converted${r.failed ? `, ${r.failed} failed` : ''}.` }),
        ...r.results.map((it) => it.ok && it.report ? h('div', {}, downloadLink(ctx, it.report.output), verificationView(it.report.verification), listBlock('Not carried over', it.report.lost, 'lost')) : h('div', { class: 'error', text: `${it.name ?? 'Document'}: ${it.error?.message ?? 'failed'}` })));
    }
    if (p.tool === 'merge') {
      const r = result as MergeResult;
      return h('div', { class: 'result-box' }, h('div', { text: `Merged into ${r.output.name} (${r.plan.pages} pages, ${formatBytes(r.output.bytes)}).` }), downloadLink(ctx, r.output), verificationView(r.verification), listBlock('Warnings', r.warnings, 'warn'));
    }
    const r = result as OpResult;
    if (r.document) ctx.docs.set(r.document);
    return h('div', { class: 'result-box' }, h('div', { text: `${r.label}.` }), verificationView(r.verification), listBlock('Notes', r.warnings, 'warn'),
      r.changed ? h('div', { class: 'muted small', text: 'You can undo this in the PDF Editor.' }) : null);
  }

  function assistantMsg(r: AssistantReply): void {
    const holder = h('div', { class: 'proposal-holder' });
    const lines = r.text.split('\n').map((l) => h('div', { text: l }));
    const bubble = h('div', { class: 'bubble' }, ...lines);
    const msg = h('div', { class: `msg assistant${r.error ? ' error' : ''}` }, bubble);
    if (r.understood) msg.appendChild(h('div', { class: 'muted small understood', text: `Understood as: ${r.understood.title ?? r.understood.tool} (${r.understood.via})` }));
    if (r.evidence.length) msg.appendChild(h('details', { class: 'evidence-box', attrs: { open: r.evidence.length <= 3 } }, h('summary', { text: `Where this comes from (${r.evidence.length})` }), ...r.evidence.slice(0, 20).map(refCell)));
    const notes = agentNotes(r.agents);
    if (notes) msg.appendChild(notes);
    if (r.proposal) { holder.appendChild(proposalCard(r.proposal, holder)); msg.appendChild(holder); }
    if (r.suggestions.length) msg.appendChild(h('div', { class: 'chips suggestions' }, ...r.suggestions.map((s) => button(s, () => { input.value = s; void submit(); }, 'chip chip-btn'))));
    log.appendChild(msg);
    scroll();
  }

  async function submit(): Promise<void> {
    const text = input.value.trim();
    if (!text || sending) return;
    sending = true;
    send.disabled = true;
    input.value = '';
    user(text);
    const typing = h('div', { class: 'msg assistant typing' }, h('div', { class: 'bubble muted', text: 'Working…' }));
    log.appendChild(typing);
    scroll();
    try {
      const r = await ctx.api.post<AssistantReply>('/api/assistant/message', { text, activeDoc: docSelect.value || undefined });
      typing.remove();
      assistantMsg(r);
    } catch (e) {
      typing.remove();
      assistantMsg({ text: errorMessage(e), evidence: [], proposal: null, understood: null, agents: [], suggestions: [], error: { code: 'error', message: errorMessage(e) } });
    } finally { sending = false; send.disabled = false; input.focus(); }
  }

  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void submit(); } });

  function refreshDocs(): void {
    const docs = ctx.docs.list().filter((d) => !d.locked);
    const keep = docSelect.value;
    replace(docSelect, h('option', { text: docs.length ? 'All open documents' : 'No documents open', attrs: { value: '' } }), ...docs.map((d) => h('option', { text: d.name, attrs: { value: d.id } })));
    docSelect.value = docs.some((d) => d.id === keep) ? keep : (docs.length === 1 ? docs[0]!.id : '');
    replace(chips, ...(docs.length ? [`Summarise ${docs[0]!.name}`, `How many words are in ${docs[0]!.name}?`, `Check the structure of ${docs[0]!.name}`, `Find personal data in ${docs[0]!.name}`].map((s) => button(s, () => { input.value = s; void submit(); }, 'chip chip-btn')) : []));
  }
  ctx.docs.changed.on(refreshDocs);

  async function showTools(): Promise<void> {
    if (!tools.length) { try { tools = (await ctx.api.get<{ tools: (Tool & { changesFiles?: boolean })[] }>('/api/assistant/tools')).tools.map((t) => ({ ...t, changesFiles: !!(t as { changesFiles?: boolean }).changesFiles })); } catch (e) { toast(errorMessage(e), 'error'); return; } }
    log.appendChild(h('div', { class: 'msg assistant' }, h('div', { class: 'bubble' },
      h('div', { text: 'Things I can do. Those marked “asks first” never change anything until you confirm:' }),
      h('ul', {}, ...tools.map((t) => h('li', {}, h('b', { text: t.title }), ` — ${t.description} `, t.changesFiles ? badge('asks first', 'warn') : badge('immediate', 'ok')))))));
    scroll();
  }

  const el = h('div', { class: 'workspace assistant' },
    h('header', { class: 'ws-head' }, h('h1', { text: 'AI Assistant' }), h('p', { class: 'muted', text: 'Ask about your documents or tell me what to do. I quote the document for every answer, say which SLAI agents were involved and why others were not, and ask before changing anything. Text inside a document is never treated as an instruction.' })),
    h('div', { class: 'chat' }, h('div', { class: 'chat-top' }, h('label', { class: 'field inline' }, h('span', { class: 'field-label', text: 'Working with' }), docSelect), button('What can you do?', () => void showTools(), 'btn-small')),
      log, prog.el, chips, h('div', { class: 'chat-compose' }, input, send)));
  log.appendChild(h('div', { class: 'msg assistant' }, h('div', { class: 'bubble', text: 'Hello. Open a document in any workspace (or drop one in the Word Counter), then ask me about it.' })));
  refreshDocs();
  return { id: 'assistant', title: 'AI Assistant', el, activate: (arg) => { refreshDocs(); if (arg?.docId) docSelect.value = arg.docId; input.focus(); } };
}

export type { Progress };
