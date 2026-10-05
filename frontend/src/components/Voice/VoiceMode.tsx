import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Mic, MicOff, X } from 'lucide-react';
import { ParticleOrb, type OrbMode } from './ParticleOrb';
import { transcribeAudio } from '../../lib/api';
import { useAppStore } from '../../lib/store';
import { getAudioContext, getTtsLevel, useTtsStore } from '../../lib/tts';
import { toSpeakableText } from '../../lib/message-text';
import { VoiceActivityDetector, isMeaningfulTranscript, rmsLevel } from '../../lib/vad';

type Phase =
  | 'starting'
  | 'listening'
  | 'hearing'
  | 'transcribing'
  | 'thinking'
  | 'speaking'
  | 'paused'
  | 'error';

interface VoiceModeProps {
  /** Sends one user turn through the normal chat pipeline; resolves when the reply is complete. */
  onSend: (text: string) => Promise<void>;
  onClose: () => void;
}

const PHASE_LABEL: Record<Phase, string> = {
  starting: 'Iniciando microfone…',
  listening: 'Ouvindo',
  hearing: 'Ouvindo',
  transcribing: 'Transcrevendo…',
  thinking: 'Pensando…',
  speaking: 'Falando',
  paused: 'Pausado — toque no orbe para retomar',
  error: 'Microfone indisponível',
};

const ORB_MODE: Record<Phase, OrbMode> = {
  starting: 'idle',
  listening: 'listening',
  hearing: 'listening',
  transcribing: 'thinking',
  thinking: 'thinking',
  speaking: 'speaking',
  paused: 'idle',
  error: 'idle',
};

/** Restart the idle recorder this often so silence never piles up in one blob. */
const IDLE_RECORDER_RESET_MS = 10_000;
/** Mic and speech RMS rarely exceed ~0.15; scale that into the orb's 0..1 range. */
const LEVEL_GAIN = 7;

/**
 * Hands-free conversation: listen until the user stops talking, transcribe,
 * send through the regular chat pipeline, speak the reply, listen again.
 *
 * Half-duplex on purpose -- the microphone is ignored while Jarvis thinks or
 * speaks, so it cannot hear and answer its own voice. Tapping the orb while it
 * speaks interrupts the reply and hands the turn back to the user.
 */
export default function VoiceMode({ onSend, onClose }: VoiceModeProps) {
  const [phase, setPhaseState] = useState<Phase>('starting');
  const [youSaid, setYouSaid] = useState('');
  const [jarvisSaid, setJarvisSaid] = useState('');
  const [notice, setNotice] = useState('');
  const selectedModel = useAppStore((s) => s.selectedModel);

  const phaseRef = useRef<Phase>('starting');
  const closedRef = useRef(false);
  const levelRef = useRef(0);
  const streamRef = useRef<MediaStream | null>(null);
  const micSourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const recorderStartedRef = useRef(0);
  const vadRef = useRef(new VoiceActivityDetector());
  const spokenIdRef = useRef<string | null>(null);

  const setPhase = useCallback((next: Phase) => {
    phaseRef.current = next;
    setPhaseState(next);
  }, []);

  const startRecorder = useCallback(() => {
    const stream = streamRef.current;
    if (!stream || closedRef.current) return;
    const recorder = new MediaRecorder(stream);
    chunksRef.current = [];
    recorder.ondataavailable = (e) => {
      if (e.data.size > 0) chunksRef.current.push(e.data);
    };
    recorder.start();
    recorderRef.current = recorder;
    recorderStartedRef.current = performance.now();
  }, []);

  /** Stop the current recorder and resolve with everything it captured. */
  const stopRecorder = useCallback((): Promise<Blob | null> => {
    const recorder = recorderRef.current;
    recorderRef.current = null;
    if (!recorder || recorder.state === 'inactive') return Promise.resolve(null);
    return new Promise((resolve) => {
      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' });
        chunksRef.current = [];
        resolve(blob);
      };
      recorder.stop();
    });
  }, []);

  const startListening = useCallback(() => {
    if (closedRef.current) return;
    vadRef.current.reset();
    startRecorder();
    setPhase('listening');
  }, [setPhase, startRecorder]);

  /** Speak the finished reply, then hand the turn back to the user. */
  const speakReply = useCallback(async (id: string, text: string) => {
    const tts = useTtsStore.getState();
    // Claim the reply first so chat autoplay does not start it a second time.
    tts.markAutoSpoken(id);
    spokenIdRef.current = id;
    setPhase('speaking');
    await tts.ensureHealth();
    if (closedRef.current) return;
    if (useTtsStore.getState().available !== true) {
      setNotice('A saída de voz está indisponível; as respostas serão exibidas, mas não faladas.');
      startListening();
      return;
    }
    await useTtsStore.getState().speak(id, text);
    if (closedRef.current) return;
    if (useTtsStore.getState().speakingId !== id) {
      // speak() gave up (synthesis failed or was interrupted) without playing.
      if (phaseRef.current === 'speaking') startListening();
      return;
    }
    await new Promise<void>((resolve) => {
      const unsubscribe = useTtsStore.subscribe((s) => {
        if (s.speakingId !== id) {
          unsubscribe();
          resolve();
        }
      });
    });
    if (!closedRef.current && phaseRef.current === 'speaking') startListening();
  }, [setPhase, startListening]);

  /** One full turn: transcribe what was heard, ask, and answer aloud. */
  const handleUtterance = useCallback(async () => {
    setPhase('transcribing');
    const blob = await stopRecorder();
    if (closedRef.current) return;
    if (!blob || blob.size === 0) {
      startListening();
      return;
    }

    let text = '';
    try {
      text = (await transcribeAudio(blob)).text.trim();
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Transcription failed');
    }
    if (closedRef.current) return;
    if (!isMeaningfulTranscript(text)) {
      startListening();
      return;
    }

    setNotice('');
    setYouSaid(text);
    setJarvisSaid('');
    setPhase('thinking');

    const sent = useAppStore.getState().messages;
    const before = sent[sent.length - 1]?.id;
    await onSend(text);
    if (closedRef.current) return;

    const after = useAppStore.getState().messages;
    const last = after[after.length - 1];
    if (!last || last.id === before || last.role !== 'assistant') {
      // The chat pipeline refused the turn (e.g. no model picked) and has
      // already toasted why; keep listening rather than stalling.
      if (!useAppStore.getState().selectedModel) setNotice('Pick a model first (⌘K), then speak again.');
      startListening();
      return;
    }
    const reply = toSpeakableText(last.content);
    setJarvisSaid(reply);
    if (!reply) {
      startListening();
      return;
    }
    await speakReply(last.id, reply);
  }, [onSend, setPhase, speakReply, startListening, stopRecorder]);

  // Microphone, analysers and the per-frame loop live for the whole session.
  useEffect(() => {
    closedRef.current = false;
    let raf = 0;
    const micBuf = new Float32Array(1024);

    (async () => {
      let stream: MediaStream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
      } catch {
        if (!closedRef.current) {
          setNotice('Allow microphone access for this page in your browser, then reopen voice mode.');
          setPhase('error');
        }
        return;
      }
      if (closedRef.current) {
        stream.getTracks().forEach((t) => t.stop());
        return;
      }
      streamRef.current = stream;

      // The shared context was created and resumed by the tap that opened voice
      // mode; one created here, after awaiting the mic, can stay suspended on
      // iOS and the analyser would read silence forever.
      const ctx = getAudioContext();
      if (!ctx) {
        setNotice('This browser has no Web Audio support, so voice mode cannot listen.');
        setPhase('error');
        return;
      }
      await ctx.resume().catch(() => {});
      if (closedRef.current) return;
      const micAnalyser = ctx.createAnalyser();
      micAnalyser.fftSize = 1024;
      const micSource = ctx.createMediaStreamSource(stream);
      micSource.connect(micAnalyser);
      micSourceRef.current = micSource;

      startListening();

      const tick = () => {
        raf = requestAnimationFrame(tick);
        const now = performance.now();
        const current = phaseRef.current;

        if (current === 'listening' || current === 'hearing') {
          micAnalyser.getFloatTimeDomainData(micBuf);
          const rms = rmsLevel(micBuf);
          levelRef.current = Math.min(1, rms * LEVEL_GAIN);
          const ev = vadRef.current.update(rms, now);
          if (ev === 'speech-start') setPhase('hearing');
          else if (ev === 'speech-end') void handleUtterance();
          else if (
            current === 'listening'
            && !vadRef.current.inSpeech
            && now - recorderStartedRef.current > IDLE_RECORDER_RESET_MS
          ) {
            void stopRecorder().then(() => {
              if (phaseRef.current === 'listening') startRecorder();
            });
            recorderStartedRef.current = now;
          }
        } else if (current === 'speaking') {
          // The reply plays straight from its <audio> element (never routed
          // through Web Audio, which could mute it); its level comes from the
          // decoded WAV at the current playback position.
          const ttsLevel = getTtsLevel();
          levelRef.current = ttsLevel !== null
            ? Math.min(1, ttsLevel * LEVEL_GAIN)
            : 0.3 + 0.2 * Math.sin(now / 90) * Math.sin(now / 230);
        } else {
          levelRef.current = 0;
        }
      };
      raf = requestAnimationFrame(tick);
    })();

    return () => {
      closedRef.current = true;
      cancelAnimationFrame(raf);
      const recorder = recorderRef.current;
      recorderRef.current = null;
      if (recorder && recorder.state !== 'inactive') recorder.stop();
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      // Closing voice mode ends the reply it started.
      const tts = useTtsStore.getState();
      if (spokenIdRef.current && tts.speakingId === spokenIdRef.current) tts.stop();
      // The context is app-wide and stays unlocked; only detach the microphone.
      micSourceRef.current?.disconnect();
      micSourceRef.current = null;
    };
    // The session is set up once; handlers read live state through refs.
  }, []);

  const handleOrbClick = useCallback(() => {
    const current = phaseRef.current;
    if (current === 'speaking') {
      // Barge-in: cut the reply short and listen immediately.
      useTtsStore.getState().stop();
      startListening();
    } else if (current === 'paused') {
      startListening();
    }
  }, [startListening]);

  const togglePause = useCallback(() => {
    const current = phaseRef.current;
    if (current === 'listening' || current === 'hearing') {
      void stopRecorder();
      vadRef.current.reset();
      setPhase('paused');
    } else if (current === 'paused') {
      startListening();
    }
  }, [setPhase, startListening, stopRecorder]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const canPause = phase === 'listening' || phase === 'hearing' || phase === 'paused';

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Modo de voz"
      className="fixed inset-0 z-50 flex flex-col"
      style={{
        background: 'radial-gradient(ellipse at center, #0b1a2e 0%, #050a14 60%, #02040a 100%)',
        color: '#d6e6ff',
      }}
    >
      <div className="flex items-center justify-between px-5 pt-4 text-xs" style={{ color: '#7f9bc4' }}>
        <span>{selectedModel ? `Model: ${selectedModel}` : 'No model selected'}</span>
        <button
          type="button"
          onClick={onClose}
          className="p-2 rounded-full cursor-pointer transition-colors hover:bg-white/10"
          title="Fechar modo de voz (Esc)"
          aria-label="Fechar modo de voz"
        >
          <X size={20} />
        </button>
      </div>

      <button
        type="button"
        onClick={handleOrbClick}
        className="relative flex-1 min-h-0 w-full cursor-pointer outline-none"
        title={phase === 'speaking' ? 'Tap to interrupt' : phase === 'paused' ? 'Tap to resume' : undefined}
        aria-label={PHASE_LABEL[phase]}
      >
        <ParticleOrb levelRef={levelRef} mode={ORB_MODE[phase]} />
      </button>

      <div className="px-6 pb-8 flex flex-col items-center gap-3 text-center">
        <div
          className="text-sm tracking-[0.3em] uppercase"
          style={{ color: phase === 'error' ? '#ff8a8a' : '#8fc4ff' }}
          aria-live="polite"
        >
          {PHASE_LABEL[phase]}
        </div>
        <div className="max-w-2xl w-full min-h-[4.5rem] text-sm leading-relaxed space-y-1">
          {youSaid && (
            <p style={{ color: '#9fb3d1' }}>
              <span style={{ color: '#5f7ca6' }}>Você: </span>
              {youSaid}
            </p>
          )}
          {jarvisSaid && (
            <p className="line-clamp-3" style={{ color: '#e3eeff' }}>
              <span style={{ color: '#5f9be0' }}>Jarvis: </span>
              {jarvisSaid}
            </p>
          )}
          {notice && <p style={{ color: '#ffb86b' }}>{notice}</p>}
        </div>
        <button
          type="button"
          onClick={togglePause}
          disabled={!canPause}
          className="p-3 rounded-full cursor-pointer transition-colors disabled:opacity-30 disabled:cursor-default"
          style={{
            background: phase === 'paused' ? 'rgba(255,138,138,0.15)' : 'rgba(143,196,255,0.12)',
            border: `1px solid ${phase === 'paused' ? 'rgba(255,138,138,0.5)' : 'rgba(143,196,255,0.35)'}`,
          }}
          title={phase === 'paused' ? 'Retomar escuta' : 'Pausar escuta'}
          aria-label={phase === 'paused' ? 'Retomar escuta' : 'Pausar escuta'}
        >
          {phase === 'paused' ? <MicOff size={20} /> : <Mic size={20} />}
        </button>
      </div>
    </div>,
    document.body,
  );
}
