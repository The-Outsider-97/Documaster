// Shapes of the Documaster HTTP API (see slai/api.py). Everything the client shows comes from these responses.

export interface Permissions { print: boolean; modify: boolean; copy: boolean; annotate: boolean; fillForms: boolean; assemble: boolean }
export interface RevisionState { current: number; count: number; labels: string[]; canUndo: boolean; canRedo: boolean }

export interface DocInfo {
  id: string;
  name: string;
  format: string;
  formatLabel: string;
  bytes: number;
  sha256: string;
  pages: number | null;
  encrypted: boolean;
  locked: boolean;
  owner: boolean;
  warnings: string[];
  permissions: Permissions | null;
  info: { title?: string; author?: string };
  revision: RevisionState;
}

export interface JobInfo<R = unknown> {
  id: string;
  kind: string;
  label: string;
  state: 'queued' | 'running' | 'done' | 'failed' | 'cancelled';
  progress: number;
  message: string;
  error: ApiErrorBody | null;
  elapsedMs: number;
  result?: R;
}

export interface ApiErrorBody { code: string; message: string; details?: Record<string, unknown>; diagnosis?: Record<string, unknown> }

export interface CheckResult { name: string; ok: boolean | null; ran?: boolean; detail: string }
export interface Verification { ok: boolean; checks: CheckResult[]; [k: string]: unknown }

export interface OutputFile { id: string; path: string; name: string; format: string; bytes: number; sha256?: string; pages?: number | null; extras: string[] }

export interface ConvertReport {
  input: { name: string; format: string; bytes: number };
  output: OutputFile;
  engine: string;
  backend: Record<string, string>;
  kept: string[];
  lost: string[];
  warnings: string[];
  ocrPages: number[];
  verification: Verification;
  durationMs: number;
}
export interface ConvertItem { id: string; name?: string; ok: boolean; report?: ConvertReport; error?: ApiErrorBody }
export interface ConvertResult { results: ConvertItem[]; succeeded: number; failed: number }

export interface FormatTarget { id: string; label: string; available: boolean; limits: string[] }
export interface FormatsInfo { targets: FormatTarget[]; engines: { libreoffice: boolean; ocr: boolean } }

export interface CountStats {
  words: number; uniqueWords: number; characters: number; charactersNoSpaces: number; letters: number; digits: number;
  punctuation: number; sentences: number; paragraphs: number; lines: number; avgWordLength: number; longestWord: string;
  readingMinutes: number; speakingMinutes: number;
  language: { code: string; confidence: number; method: string };
  topTerms: { term: string; count: number }[];
  goals?: { goal: string; limit: number; value: number; met: boolean }[];
  document?: { name: string; format: string; pages: number | null; backend: string };
  breakdown?: { kind: 'page' | 'part'; index: number; label: string; words: number; characters: number; sentences: number }[];
  headings?: { level: number; text: string }[];
  notes?: string[];
  tables?: number; images?: number;
}
export interface CountItem { id: string; name?: string; ok: boolean; stats?: CountStats; error?: ApiErrorBody }
export interface CountResult { results: CountItem[]; totals: Record<string, number> | null }

export interface OutlinePage { index: number; label: string; width?: number; height?: number; rotation?: number; words?: number; blocks?: number; kinds?: string[] }
export interface OutlineDoc { id: string; name: string; format: string; kind: 'pdf' | 'model' | 'image'; pages: OutlinePage[]; warnings: string[] }

export interface PlanEntry { doc?: number; page?: number; rotation?: number; included?: boolean; blank?: boolean; width?: number; height?: number }

export interface MergeResult {
  output: OutputFile;
  engine: string;
  backend: Record<string, string>;
  bookmarks?: number;
  kept: string[];
  lost: string[];
  warnings: string[];
  verification: Verification;
  plan: { pages: number; excluded: number };
  inputs: { name: string; format: string; pages: number; sha256: string }[];
  durationMs: number;
}

export interface OpResult { changed: boolean; label: string; details: Record<string, unknown>; warnings: string[]; verification?: Verification; document: DocInfo }

export interface FindHit { page: number; text: string; rects: number[][] }
export interface PdfInfo {
  metadata: Record<string, string | number>;
  outline: OutlineNode[];
  annotations: AnnotationInfo[];
  fields: FormField[];
  pages: { index: number; width: number; height: number; rotation: number }[];
  permissions: Permissions;
}
export interface OutlineNode { title: string; page: number | null; children: OutlineNode[] }
export interface AnnotationInfo { id: string | null; page: number; index: number; type: string; text: string; author: string; rect: number[] | null; ours: boolean }
export interface FormField { name: string; type: string; value: string | null; options: string[] | null; readOnly: boolean; locations: { page: number; rect: number[] | null }[] }
export interface PiiGroup { type: string; label: string; text: string; confidence: string; count: number; hits: { page: number; rects: number[][] }[] }
export interface PiiRegions { items: PiiGroup[]; counts: Record<string, number>; note: string; truncated: boolean }

export interface Evidence { docId: string; doc: string; blockId: string; page: number | null; part: number; start: number; end: number; quote?: string }

export interface AgentNote { role: string; agent?: string | null; status: 'used' | 'skipped' | 'unavailable' | 'failed'; reason?: string | null; output?: unknown; ms?: number }

export interface Proposal { id: string; tool: string; title: string; description: string; consequences: string[]; needs: string[]; expiresInS: number }
export interface AssistantReply {
  text: string;
  data?: unknown;
  evidence: Evidence[];
  proposal: Proposal | null;
  understood: { tool: string; title?: string; via: string; changesFiles?: boolean; arguments: Record<string, unknown> } | null;
  agents: AgentNote[];
  suggestions: string[];
  error?: ApiErrorBody;
}

export interface Settings { output_dir: string; max_upload_mb: number; max_pdf_pages: number; agents: string; allow_remote_processing: boolean; agent_telemetry: boolean; agent_read_default: boolean; persist_recent: boolean; job_timeout_s: number }

export interface AgentSlot { state: string; class: string; loadMs: number; error: string | null; calls: number; failures: number; timeouts: number; skipped: number; breakerOpen: boolean; lastError: string | null; latencyP95Ms: number; test?: { ok: boolean | null; detail: string; ms: number; at: number } | null }
export interface AgentsStatus { mode: string; root: string | null; fatal: string | null; agents: Record<string, AgentSlot> }
