// Application state shared by the workspaces: the open documents, which workspace is showing, the theme.
import { Api } from './api.js';
import type { DocInfo, FormatsInfo } from './types.js';

export type Theme = 'dark' | 'light';
export type Listener<T> = (value: T) => void;

export class Emitter<T> {
  private listeners = new Set<Listener<T>>();
  on(fn: Listener<T>): () => void { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(v: T): void { for (const fn of [...this.listeners]) fn(v); }
}

export class Documents {
  private docs = new Map<string, DocInfo>();
  readonly changed = new Emitter<DocInfo[]>();
  constructor(private readonly api: Api) {}

  list(): DocInfo[] { return [...this.docs.values()]; }
  get(id: string): DocInfo | undefined { return this.docs.get(id); }
  pdfs(): DocInfo[] { return this.list().filter((d) => d.format === 'pdf' && !d.locked); }

  set(doc: DocInfo): void { this.docs.set(doc.id, doc); this.changed.emit(this.list()); }

  async refresh(): Promise<void> {
    const list = await this.api.get<DocInfo[]>('/api/documents');
    this.docs = new Map(list.map((d) => [d.id, d]));
    this.changed.emit(this.list());
  }

  async remove(id: string): Promise<void> {
    await this.api.del(`/api/documents/${id}`);
    this.docs.delete(id);
    this.changed.emit(this.list());
  }

  async unlock(id: string, password: string): Promise<DocInfo> {
    const doc = await this.api.post<DocInfo>(`/api/documents/${id}/unlock`, { password });
    this.set(doc);
    return doc;
  }
}

export interface AppContext {
  api: Api;
  docs: Documents;
  formats: () => Promise<FormatsInfo>;
  open: (workspace: WorkspaceId, docId?: string, page?: number) => void;
  openers: Map<WorkspaceId, (docId?: string, page?: number) => void>;
}

export type WorkspaceId = 'counter' | 'converter' | 'merger' | 'editor' | 'reader' | 'assistant' | 'system';

export const THEME_KEY = 'documaster.theme';

export function loadTheme(): Theme {
  try {
    const v = localStorage.getItem(THEME_KEY);
    if (v === 'light' || v === 'dark') return v;
  } catch { /* storage can be unavailable */ }
  return 'dark';
}

export function saveTheme(t: Theme): void {
  try { localStorage.setItem(THEME_KEY, t); } catch { /* ignore */ }
}
