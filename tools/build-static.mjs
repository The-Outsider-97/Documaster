// Copies the static client assets next to the compiled JavaScript (dist/js). Run after `tsc -p tsconfig.build.json`.
import { copyFileSync, cpSync, mkdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const dist = join(root, 'dist');
if (!existsSync(join(dist, 'js', 'client', 'main.js'))) {
  console.error('dist/js/client/main.js is missing: run `tsc -p tsconfig.build.json` first');
  process.exit(1);
}
mkdirSync(dist, { recursive: true });
for (const f of ['index.html', 'styles.css']) copyFileSync(join(root, 'src', 'client', f), join(dist, f));
if (existsSync(join(root, 'assets'))) cpSync(join(root, 'assets'), join(dist, 'assets'), { recursive: true });
console.log('static assets copied to dist/');
