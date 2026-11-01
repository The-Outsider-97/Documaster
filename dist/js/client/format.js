// Small pure formatting helpers (no DOM) so they can be unit-tested.
export function formatBytes(n) {
    if (!Number.isFinite(n) || n < 0)
        return '–';
    if (n < 1024)
        return `${n} B`;
    const units = ['KB', 'MB', 'GB'];
    let v = n / 1024;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i++;
    }
    return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}
export function formatNumber(n) {
    return Number.isFinite(n) ? n.toLocaleString('en-US') : '–';
}
export function formatDuration(ms) {
    if (ms < 1000)
        return `${Math.round(ms)} ms`;
    const s = ms / 1000;
    return s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}
export function formatMinutes(min) {
    if (min <= 0)
        return '0 min';
    if (min < 1)
        return `${Math.max(1, Math.round(min * 60))} sec`;
    return min < 10 ? `${min.toFixed(1)} min` : `${Math.round(min)} min`;
}
export function stem(name) {
    const i = name.lastIndexOf('.');
    return i > 0 ? name.slice(0, i) : name;
}
export function extension(name) {
    const i = name.lastIndexOf('.');
    return i > 0 ? name.slice(i + 1).toLowerCase() : '';
}
/** "1-3, 5" -> [1,2,3,5] (1-based, ascending, unique). Returns null when the text is not a valid range list. */
export function parseRanges(text, max) {
    const out = new Set();
    const cleaned = text.trim();
    if (!cleaned)
        return [];
    for (const part of cleaned.split(/[,;\s]+/)) {
        if (!part)
            continue;
        const m = /^(\d+)(?:\s*[-–]\s*(\d*))?$/.exec(part);
        if (!m)
            return null;
        const a = Number(m[1]);
        const b = m[2] === undefined ? a : m[2] === '' ? max : Number(m[2]);
        if (a < 1 || b < a || a > max)
            return null;
        for (let i = a; i <= Math.min(b, max); i++)
            out.add(i);
    }
    return [...out].sort((x, y) => x - y);
}
/** [1,2,3,5] -> "1-3, 5". */
export function formatRanges(pages) {
    const sorted = [...new Set(pages)].sort((a, b) => a - b);
    const parts = [];
    for (let i = 0; i < sorted.length;) {
        let j = i;
        while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1)
            j++;
        parts.push(j > i ? `${sorted[i]}-${sorted[j]}` : `${sorted[i]}`);
        i = j + 1;
    }
    return parts.join(', ');
}
export function plural(n, one, many = `${one}s`) {
    return `${formatNumber(n)} ${n === 1 ? one : many}`;
}
/** Spreadsheet-safe UTF-8 CSV. Imported document text is treated as data, never as a formula. */
export function toCsv(rows) {
    const cell = (value) => {
        let text = String(value ?? '');
        if (/^[\s\u0000-\u001f]*[=+@-]/.test(text))
            text = `'${text}`;
        return `"${text.replace(/"/g, '""')}"`;
    };
    return '\uFEFF' + rows.map((row) => row.map(cell).join(',')).join('\r\n') + '\r\n';
}
//# sourceMappingURL=format.js.map