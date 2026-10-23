// Typed client for the local Documaster server. The session cookie (set when the launch link is opened) authenticates every call;
// state-changing calls also send the X-Documaster marker the server requires.
import type { ApiErrorBody, DocInfo, JobInfo } from './types.js';

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details: Record<string, unknown>;
  readonly body: ApiErrorBody | null;
  constructor(code: string, message: string, status = 0, details: Record<string, unknown> = {}, body: ApiErrorBody | null = null) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.details = details;
    this.body = body;
  }
  get cancelled(): boolean { return this.code === 'cancelled'; }
}

export type FetchLike = (input: string, init?: { method?: string; headers?: Record<string, string>; body?: any; signal?: AbortSignal }) => Promise<{
  ok: boolean; status: number; text(): Promise<string>;
}>;

export interface JobHandle<R> {
  readonly id: string;
  /** Resolves with the job's result; rejects with ApiError (code "cancelled" when it was cancelled). */
  wait(onProgress?: (j: JobInfo<R>) => void): Promise<R>;
  cancel(): Promise<void>;
}

export interface UploadOptions { images?: boolean; onProgress?: (fraction: number) => void; signal?: AbortSignal }

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

export class Api {
  private readonly base: string;
  private readonly fetchFn: FetchLike;
  readonly pollMs: number[];

  constructor(base = '', fetchFn?: FetchLike, pollMs: number[] = [150, 250, 400, 600]) {
    this.base = base;
    this.fetchFn = fetchFn ?? ((input, init) => fetch(input, init as RequestInit) as unknown as ReturnType<FetchLike>);
    this.pollMs = pollMs;
  }

  async call<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    const headers: Record<string, string> = { 'X-Documaster': '1' };
    let payload: string | undefined;
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      payload = JSON.stringify(body);
    }
    let res: Awaited<ReturnType<FetchLike>>;
    try {
      res = await this.fetchFn(this.base + path, { method, headers, body: payload, signal });
    } catch (e) {
      if ((e as { name?: string }).name === 'AbortError') throw new ApiError('cancelled', 'The request was cancelled');
      throw new ApiError('network', 'Documaster is not reachable. Is it still running?');
    }
    return this.parse<T>(res.status, await res.text());
  }

  private parse<T>(status: number, text: string): T {
    let json: { ok?: boolean; data?: T; error?: ApiErrorBody } | null = null;
    try { json = text ? JSON.parse(text) : null; } catch { json = null; }
    if (!json || typeof json !== 'object') throw new ApiError('bad_response', `Unexpected response from the server (${status})`, status);
    if (json.ok === false || status >= 400) {
      const e = json.error ?? { code: 'error', message: `The server answered ${status}` };
      throw new ApiError(e.code, e.message, status, e.details ?? {}, e);
    }
    return json.data as T;
  }

  get<T>(path: string, signal?: AbortSignal): Promise<T> { return this.call<T>('GET', path, undefined, signal); }
  post<T>(path: string, body: unknown = {}, signal?: AbortSignal): Promise<T> { return this.call<T>('POST', path, body, signal); }
  put<T>(path: string, body: unknown = {}): Promise<T> { return this.call<T>('PUT', path, body); }
  del<T>(path: string): Promise<T> { return this.call<T>('DELETE', path); }

  /** Start a job endpoint (202 + job). */
  async startJob<R>(path: string, body: unknown): Promise<JobHandle<R>> {
    const data = await this.post<{ job: JobInfo<R> }>(path, body);
    return this.handle<R>(data.job.id);
  }

  handle<R>(id: string): JobHandle<R> {
    const self = this;
    return {
      id,
      async wait(onProgress) {
        for (let i = 0; ; i++) {
          const j = await self.get<JobInfo<R>>(`/api/jobs/${id}`);
          onProgress?.(j);
          if (j.state === 'done') return j.result as R;
          if (j.state === 'failed') {
            const e = j.error ?? { code: 'error', message: 'The operation failed' };
            throw new ApiError(e.code, e.message, 0, e.details ?? {}, e);
          }
          if (j.state === 'cancelled') throw new ApiError('cancelled', 'The operation was cancelled');
          await sleep(self.pollMs[Math.min(i, self.pollMs.length - 1)] ?? 400);
        }
      },
      async cancel() { await self.post(`/api/jobs/${id}/cancel`); },
    };
  }

  async runJob<R>(path: string, body: unknown, onProgress?: (j: JobInfo<R>) => void): Promise<R> {
    return (await this.startJob<R>(path, body)).wait(onProgress);
  }

  /** Upload one file as a new document. Uses XHR for progress where it exists (browsers), fetch otherwise. */
  upload(file: Blob & { name?: string }, name: string, opts: UploadOptions = {}): Promise<DocInfo> {
    const q = `/api/documents?name=${encodeURIComponent(name)}${opts.images ? '&images=1' : ''}`;
    if (typeof XMLHttpRequest === 'undefined') {
      return this.fetchFn(this.base + q, { method: 'POST', headers: { 'X-Documaster': '1' }, body: file, signal: opts.signal })
        .then(async (r) => this.parse<DocInfo>(r.status, await r.text()));
    }
    return new Promise<DocInfo>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', this.base + q);
      xhr.setRequestHeader('X-Documaster', '1');
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) opts.onProgress?.(e.loaded / e.total); };
      xhr.onerror = () => reject(new ApiError('network', 'Documaster is not reachable. Is it still running?'));
      xhr.onabort = () => reject(new ApiError('cancelled', 'The upload was cancelled'));
      xhr.onload = () => { try { resolve(this.parse<DocInfo>(xhr.status, xhr.responseText)); } catch (e) { reject(e); } };
      opts.signal?.addEventListener('abort', () => xhr.abort());
      xhr.send(file);
    });
  }

  /** Upload an image used by the PDF editor (stamps). */
  async uploadAsset(file: Blob, name: string): Promise<{ id: string; name: string; mediaType: string; width: number; height: number }> {
    const r = await this.fetchFn(`${this.base}/api/assets?name=${encodeURIComponent(name)}`,
      { method: 'POST', headers: { 'X-Documaster': '1' }, body: file });
    return this.parse(r.status, await r.text());
  }

  // URLs the browser loads directly (cookie-authenticated GETs)
  downloadUrl(outputId: string): string { return `${this.base}/api/outputs/${outputId}/download`; }
  fileUrl(docId: string): string { return `${this.base}/api/documents/${docId}/file`; }
  pageImageUrl(docId: string, page: number, width: number, version = ''): string {
    return `${this.base}/api/documents/${docId}/pages/${page}/image?w=${Math.round(width)}${version ? `&v=${encodeURIComponent(version.slice(0, 12))}` : ''}`;
  }
}
