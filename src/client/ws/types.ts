import type { AppContext } from '../store.js';

export interface Workspace {
  readonly id: string;
  readonly title: string;
  readonly el: HTMLElement;
  /** Called every time the workspace is shown. */
  activate?(arg?: { docId?: string; page?: number }): void;
}

export type WorkspaceFactory = (ctx: AppContext) => Workspace;
