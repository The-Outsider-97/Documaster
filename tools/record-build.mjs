// Writes dist/build.json: the SHA-256 of every file the client build reads, so `python -m slai --check` can tell a current build from a
// stale one by content rather than by file times. slai/startup.py computes the same map independently (client_inputs); a test compares the
// two on the real tree. Files: everything under src/, typings/ and assets/ (skipping node_modules) plus the two TypeScript configurations.
// Keys are paths relative to the Documaster folder with forward slashes. Text files are hashed with CRLF folded to LF (a file is text when
// its first 8192 bytes hold no NUL), so a Windows checkout with autocrlf hashes like one with LF.
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

const INPUT_DIRS = ['src', 'typings', 'assets'];
const INPUT_FILES = ['tsconfig.json', 'tsconfig.build.json'];

function walk(dir, out) {
  if (!existsSync(dir)) return;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules') continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) walk(full, out);
    else if (entry.isFile() || (entry.isSymbolicLink() && statSync(full).isFile())) out.push(full);
  }
}

export function digest(file) {
  let data = readFileSync(file);
  if (!data.subarray(0, 8192).includes(0)) {
    const kept = [];
    for (let i = 0; i < data.length; i++) if (!(data[i] === 13 && data[i + 1] === 10)) kept.push(data[i]);
    data = Buffer.from(kept);
  }
  return createHash('sha256').update(data).digest('hex');
}

export function inputFiles(appDir) {
  const files = [];
  for (const d of INPUT_DIRS) walk(join(appDir, d), files);
  for (const f of INPUT_FILES) if (existsSync(join(appDir, f))) files.push(join(appDir, f));
  const key = (f) => relative(appDir, f).split(sep).join('/');
  return files.map((f) => [key(f), f]).sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
}

export function recordBuild(appDir, dist) {
  const files = {};
  for (const [key, f] of inputFiles(appDir)) files[key] = digest(f);
  writeFileSync(join(dist, 'build.json'), JSON.stringify({ format: 1, built: new Date().toISOString().replace(/\.\d+Z$/, '+00:00'), files }, null, 1) + '\n');
}
