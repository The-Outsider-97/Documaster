// Minimal ambient declarations so the project type-checks even where @types/node is not installed.
// When @types/node IS installed these are harmless (module declarations merge as any).
declare const process: { argv: string[]; env: Record<string, string | undefined>; exit(code?: number): never };
declare module 'node:test' { const test: any; export const describe: any; export const it: any; export default test; }
declare module 'node:assert/strict' { const assert: any; export default assert; }
declare module 'node:fs' { const fs: any; export = fs; }
declare module 'node:path' { const p: any; export = p; }
declare module 'node:url' { export const fileURLToPath: (u: string) => string; }
