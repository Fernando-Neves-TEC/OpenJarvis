/**
 * TEMPORARY audio diagnostics for the iOS Safari "no sound" investigation.
 * Remove together with components/AudioDiagBanner.tsx once resolved.
 */
import { create } from 'zustand';

interface AudioDiagStore {
  lines: string[];
  push: (line: string) => void;
  clear: () => void;
}

export const useAudioDiag = create<AudioDiagStore>((set) => ({
  lines: [],
  push: (line) => set((s) => ({ lines: [...s.lines.slice(-11), line] })),
  clear: () => set({ lines: [] }),
}));

let lastGestureAt: number | null = null;
if (typeof document !== 'undefined') {
  const mark = () => {
    lastGestureAt = performance.now();
  };
  document.addEventListener('pointerdown', mark, { capture: true });
  document.addEventListener('keydown', mark, { capture: true });
}

function sinceGesture(): string {
  if (lastGestureAt === null) return 'sem toque registrado';
  return `${((performance.now() - lastGestureAt) / 1000).toFixed(1)} s após o último toque`;
}

function stamp(): string {
  return new Date().toLocaleTimeString('pt-BR', { hour12: false });
}

export function audioDiag(line: string): void {
  useAudioDiag.getState().push(`${stamp()} ${line}`);
}

// Once per page load (not per banner mount): which build is running and
// whether a service worker controls the page, so a stale cached copy shows.
if (typeof document !== 'undefined') {
  const chunk = new URL(import.meta.url).pathname.split('/').pop() ?? '?';
  const sw = typeof navigator !== 'undefined' && navigator.serviceWorker?.controller
    ? 'service worker ativo'
    : 'sem service worker';
  const probe = document.createElement('audio');
  audioDiag(
    `versão ${chunk} | ${sw} | canPlayType("audio/wav")="${probe.canPlayType('audio/wav') || 'vazio'}"`,
  );
}

/** Describe a play()/resume() rejection, including how long since the last tap. */
export function audioDiagError(what: string, err: unknown): void {
  const name = err instanceof Error ? err.name : typeof err;
  const message = err instanceof Error ? err.message : String(err);
  audioDiag(`${what} FALHOU: ${name}: ${message} (${sinceGesture()})`);
}

export function audioDiagOk(what: string): void {
  audioDiag(`${what} ok (${sinceGesture()})`);
}

const MEDIA_ERRORS: Record<number, string> = {
  1: 'MEDIA_ERR_ABORTED',
  2: 'MEDIA_ERR_NETWORK',
  3: 'MEDIA_ERR_DECODE',
  4: 'MEDIA_ERR_SRC_NOT_SUPPORTED',
};

export function audioDiagMediaError(el: HTMLAudioElement): void {
  const e = el.error;
  const code = e ? MEDIA_ERRORS[e.code] ?? `code ${e.code}` : 'sem MediaError';
  audioDiag(`elemento <audio> erro: ${code}${e?.message ? `: ${e.message}` : ''}`);
}
