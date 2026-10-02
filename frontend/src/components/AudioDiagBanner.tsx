/**
 * TEMPORARY on-screen audio diagnostics (iOS Safari has no hover tooltips and
 * no easy console). Remove together with lib/audio-diag.ts once resolved.
 */
import { useEffect } from 'react';
import { createPortal } from 'react-dom';
import { audioDiag, useAudioDiag } from '../lib/audio-diag';

export function AudioDiagBanner() {
  const lines = useAudioDiag((s) => s.lines);
  const clear = useAudioDiag((s) => s.clear);

  useEffect(() => {
    const probe = document.createElement('audio');
    audioDiag(`canPlayType("audio/wav") = "${probe.canPlayType('audio/wav') || 'vazio'}"`);
  }, []);

  if (lines.length === 0) return null;
  return createPortal(
    <div
      role="status"
      onClick={clear}
      style={{
        position: 'fixed',
        left: 8,
        right: 8,
        top: 'calc(env(safe-area-inset-top, 0px) + 8px)',
        zIndex: 2147483647,
        background: 'rgba(40, 10, 10, 0.92)',
        color: '#ffd7d7',
        border: '1px solid #ff6b6b',
        borderRadius: 8,
        padding: '6px 8px',
        font: '11px/1.35 ui-monospace, monospace',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
      }}
    >
      <div style={{ fontWeight: 700 }}>Diagnóstico de áudio (temporário, toque para limpar)</div>
      {lines.map((line, i) => (
        <div key={i}>{line}</div>
      ))}
    </div>,
    document.body,
  );
}
