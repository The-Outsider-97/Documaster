export class ApiError extends Error {
    code;
    status;
    details;
    body;
    constructor(code, message, status = 0, details = {}, body = null) {
        super(message);
        this.name = 'ApiError';
        this.code = code;
        this.status = status;
        this.details = details;
        this.body = body;
    }
    get cancelled() { return this.code === 'cancelled'; }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export class Api {
    base;
    fetchFn;
    pollMs;
    constructor(base = '', fetchFn, pollMs = [150, 250, 400, 600]) {
        this.base = base;
        this.fetchFn = fetchFn ?? ((input, init) => fetch(input, init));
        this.pollMs = pollMs;
    }
    async call(method, path, body, signal) {
        const headers = { 'X-Documaster': '1' };
        let payload;
        if (body !== undefined) {
            headers['Content-Type'] = 'application/json';
            payload = JSON.stringify(body);
        }
        let res;
        try {
            res = await this.fetchFn(this.base + path, { method, headers, body: payload, signal });
        }
        catch (e) {
            if (e.name === 'AbortError')
                throw new ApiError('cancelled', 'The request was cancelled');
            throw new ApiError('network', 'Documaster is not reachable. Is it still running?');
        }
        return this.parse(res.status, await res.text());
    }
    parse(status, text) {
        let json = null;
        try {
            json = text ? JSON.parse(text) : null;
        }
        catch {
            json = null;
        }
        if (!json || typeof json !== 'object')
            throw new ApiError('bad_response', `Unexpected response from the server (${status})`, status);
        if (json.ok === false || status >= 400) {
            const e = json.error ?? { code: 'error', message: `The server answered ${status}` };
            throw new ApiError(e.code, e.message, status, e.details ?? {}, e);
        }
        return json.data;
    }
    get(path, signal) { return this.call('GET', path, undefined, signal); }
    post(path, body = {}, signal) { return this.call('POST', path, body, signal); }
    put(path, body = {}) { return this.call('PUT', path, body); }
    del(path) { return this.call('DELETE', path); }
    /** Start a job endpoint (202 + job). */
    async startJob(path, body) {
        const data = await this.post(path, body);
        return this.handle(data.job.id);
    }
    handle(id) {
        const self = this;
        return {
            id,
            async wait(onProgress) {
                for (let i = 0;; i++) {
                    const j = await self.get(`/api/jobs/${id}`);
                    onProgress?.(j);
                    if (j.state === 'done')
                        return j.result;
                    if (j.state === 'failed') {
                        const e = j.error ?? { code: 'error', message: 'The operation failed' };
                        throw new ApiError(e.code, e.message, 0, e.details ?? {}, e);
                    }
                    if (j.state === 'cancelled')
                        throw new ApiError('cancelled', 'The operation was cancelled');
                    await sleep(self.pollMs[Math.min(i, self.pollMs.length - 1)] ?? 400);
                }
            },
            async cancel() { await self.post(`/api/jobs/${id}/cancel`); },
        };
    }
    async runJob(path, body, onProgress) {
        return (await this.startJob(path, body)).wait(onProgress);
    }
    /** Upload one file as a new document. Uses XHR for progress where it exists (browsers), fetch otherwise. */
    upload(file, name, opts = {}) {
        const q = `/api/documents?name=${encodeURIComponent(name)}${opts.images ? '&images=1' : ''}`;
        if (typeof XMLHttpRequest === 'undefined') {
            return this.fetchFn(this.base + q, { method: 'POST', headers: { 'X-Documaster': '1' }, body: file, signal: opts.signal })
                .then(async (r) => this.parse(r.status, await r.text()));
        }
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', this.base + q);
            xhr.setRequestHeader('X-Documaster', '1');
            xhr.upload.onprogress = (e) => { if (e.lengthComputable)
                opts.onProgress?.(e.loaded / e.total); };
            xhr.onerror = () => reject(new ApiError('network', 'Documaster is not reachable. Is it still running?'));
            xhr.onabort = () => reject(new ApiError('cancelled', 'The upload was cancelled'));
            xhr.onload = () => { try {
                resolve(this.parse(xhr.status, xhr.responseText));
            }
            catch (e) {
                reject(e);
            } };
            opts.signal?.addEventListener('abort', () => xhr.abort());
            xhr.send(file);
        });
    }
    /** Upload an image used by the PDF editor (stamps). */
    async uploadAsset(file, name) {
        const r = await this.fetchFn(`${this.base}/api/assets?name=${encodeURIComponent(name)}`, { method: 'POST', headers: { 'X-Documaster': '1' }, body: file });
        return this.parse(r.status, await r.text());
    }
    // URLs the browser loads directly (cookie-authenticated GETs)
    downloadUrl(outputId) { return `${this.base}/api/outputs/${outputId}/download`; }
    fileUrl(docId) { return `${this.base}/api/documents/${docId}/file`; }
    pageImageUrl(docId, page, width, version = '') {
        return `${this.base}/api/documents/${docId}/pages/${page}/image?w=${Math.round(width)}${version ? `&v=${encodeURIComponent(version.slice(0, 12))}` : ''}`;
    }
}
//# sourceMappingURL=api.js.map