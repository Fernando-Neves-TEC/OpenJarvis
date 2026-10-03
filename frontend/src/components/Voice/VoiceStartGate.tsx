import { createPortal } from 'react-dom';
import { Mic } from 'lucide-react';

interface VoiceStartGateProps {
  /** Speech-to-text backend confirmed; the button stays disabled until then. */
  ready: boolean;
  /** Runs inside the tap, so audio/mic unlocking still counts as user-initiated. */
  onStart: () => void;
  onDismiss: () => void;
}

/**
 * Landing screen for `/?voice=1` (the home-screen shortcut's start_url). iOS
 * only grants microphone and audio playback from a user gesture, so voice mode
 * cannot open by itself on load; one big tap target starts it instead.
 */
export function VoiceStartGate({ ready, onStart, onDismiss }: VoiceStartGateProps) {
  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Iniciar modo de voz"
      className="fixed inset-0 z-50 flex flex-col items-center justify-center gap-8 px-6"
      style={{
        background: 'radial-gradient(ellipse at center, #0b1a2e 0%, #050a14 60%, #02040a 100%)',
        color: '#d6e6ff',
        paddingTop: 'env(safe-area-inset-top)',
        paddingBottom: 'env(safe-area-inset-bottom)',
      }}
    >
      <button
        type="button"
        onClick={onStart}
        disabled={!ready}
        className="flex flex-col items-center justify-center gap-4 rounded-full cursor-pointer transition-transform active:scale-95 disabled:cursor-default disabled:opacity-50"
        style={{
          width: 'min(70vw, 280px)',
          height: 'min(70vw, 280px)',
          background: 'radial-gradient(circle, rgba(92,184,255,0.35) 0%, rgba(47,143,255,0.12) 60%, transparent 72%)',
          border: '1px solid rgba(143,196,255,0.45)',
          color: '#e3eeff',
        }}
      >
        <Mic size={56} />
        <span className="text-lg font-medium">{ready ? 'Toque para falar' : 'Conectando…'}</span>
      </button>
      <button
        type="button"
        onClick={onDismiss}
        className="text-sm underline cursor-pointer"
        style={{ color: '#7f9bc4' }}
      >
        Abrir o chat
      </button>
    </div>,
    document.body,
  );
}
