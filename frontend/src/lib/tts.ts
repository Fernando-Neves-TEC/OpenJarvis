import { create } from 'zustand';
import { synthesizeSpeech, fetchTtsHealth } from './api';

export type TtsState = 'idle' | 'loading' | 'speaking';

/**
 * Voice output is a single shared resource: one utterance at a time, for the
 * whole app. The state lives in one store rather than per component, because a
 * per-component hook would give every message its own audio element -- two
 * replies would then talk over each other, and autoplay would make that the
 * normal case rather than the exception.
 */
interface TtsStore {
  state: TtsState;
  /** id of the message currently loading or speaking, if any. */
  speakingId: string | null;
  error: string | null;
  /** Message whose read-aloud attempt failed, if any. */
  errorId: string | null;
  /** null until the health probe has answered. */
  available: boolean | null;
  /** Last message spoken by autoplay, so a re-render never repeats it. */
  autoSpokenId: string | null;
  speak: (id: string, text: string) => Promise<void>;
  stop: () => void;
  ensureHealth: () => Promise<void>;
  markAutoSpoken: (id: string) => void;
}

// Playback handles are not render state -- keeping them out of the store avoids
// re-rendering every subscriber when an audio element is swapped.
let audio: HTMLAudioElement | null = null;
let objectUrl: string | null = null;
let controller: AbortController | null = null;
let token = 0;
let healthProbe: Promise<void> | null = null;

/**
 * One <audio> element for the whole app, reused for every utterance. iOS
 * Safari only lets an element play without a fresh tap once a tap has started
 * it; a reply is synthesized seconds after the tap, so a new element per
 * utterance is rejected with NotAllowedError. `unlockAudio()` primes this one.
 */
let player: HTMLAudioElement | null = null;
let unlocked = false;
let audioContext: AudioContext | null = null;

function getPlayer(): HTMLAudioElement {
  if (!player) {
    player = new Audio();
    player.preload = 'auto';
  }
  return player;
}

/** 50 ms of 16-bit mono PCM silence as a complete WAV file. */
export function buildSilentWav(): Uint8Array<ArrayBuffer> {
  const samples = 400;
  const bytes = new Uint8Array(44 + samples * 2);
  const view = new DataView(bytes.buffer);
  const ascii = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i++) bytes[offset + i] = text.charCodeAt(i);
  };
  ascii(0, 'RIFF');
  view.setUint32(4, 36 + samples * 2, true);
  ascii(8, 'WAVE');
  ascii(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, 8000, true);
  view.setUint32(28, 16000, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  ascii(36, 'data');
  view.setUint32(40, samples * 2, true);
  return bytes;
}

/**
 * The primer as a blob: URL, created synchronously so it fits in a tap. Not a
 * data: URI: the server's CSP (`media-src 'self' blob:`) blocks those, which
 * browsers report as NotSupportedError ("no supported source").
 */
let silentUri: string | null = null;

function silentClipUrl(): string {
  silentUri ??= URL.createObjectURL(new Blob([buildSilentWav()], { type: 'audio/wav' }));
  return silentUri;
}

/**
 * The app-wide AudioContext, created and resumed inside a tap so iOS starts it
 * running. Voice mode reads the microphone level through it; a context created
 * later (after awaiting the mic permission) can stay suspended and read zeros.
 */
export function getAudioContext(): AudioContext | null {
  if (!audioContext) {
    const Ctor = typeof window !== 'undefined' ? window.AudioContext : undefined;
    if (!Ctor) return null;
    audioContext = new Ctor();
  }
  return audioContext;
}

/**
 * Prime the shared player and AudioContext. Must run synchronously inside a
 * user gesture (touchend/click/keydown); calling it again is cheap.
 */
export function unlockAudio(): void {
  const ctx = getAudioContext();
  if (ctx && ctx.state !== 'running') {
    ctx.resume().catch(() => {});
  }
  // Never interrupt a real utterance to play the silent primer.
  if (unlocked || audio) return;
  const el = getPlayer();
  const clip = silentClipUrl();
  el.src = clip;
  const started = el.play();
  Promise.resolve(started).then(
    () => {
      unlocked = true;
      removeUnlockListeners();
      // speak() may have taken the element over while the primer started.
      if (!audio && el.src === clip) el.pause();
    },
    () => {
      // Still locked; the listeners stay installed for the next gesture.
    },
  );
}

const UNLOCK_EVENTS = ['touchend', 'pointerup', 'click', 'keydown'] as const;

function onUnlockGesture(): void {
  unlockAudio();
}

function removeUnlockListeners(): void {
  if (typeof document === 'undefined') return;
  for (const name of UNLOCK_EVENTS) {
    document.removeEventListener(name, onUnlockGesture, true);
  }
}

function installUnlockListeners(): void {
  if (typeof document === 'undefined') return;
  for (const name of UNLOCK_EVENTS) {
    document.addEventListener(name, onUnlockGesture, true);
  }
}

installUnlockListeners();

/**
 * Speech level of the current utterance, 0..1-ish RMS per 20 ms frame, decoded
 * from the WAV itself so the playing element never has to be routed through
 * Web Audio (a MediaElementSource is permanent, mutes the element whenever the
 * context is suspended, and on iOS obeys the silent switch).
 */
const ENVELOPE_FRAME_S = 0.02;
let envelope: Float32Array | null = null;

async function computeEnvelope(blob: Blob, mine: number): Promise<void> {
  const Offline = typeof window !== 'undefined' ? window.OfflineAudioContext : undefined;
  if (!Offline) return;
  try {
    const decoded = await new Offline(1, 1, 24000).decodeAudioData(await blob.arrayBuffer());
    if (mine !== token) return;
    const data = decoded.getChannelData(0);
    const frame = Math.max(1, Math.round(decoded.sampleRate * ENVELOPE_FRAME_S));
    const out = new Float32Array(Math.ceil(data.length / frame));
    for (let f = 0; f < out.length; f++) {
      let sum = 0;
      const end = Math.min(data.length, (f + 1) * frame);
      for (let i = f * frame; i < end; i++) sum += data[i] * data[i];
      out[f] = Math.sqrt(sum / Math.max(1, end - f * frame));
    }
    envelope = out;
  } catch {
    // No level for this utterance; voice mode falls back to a synthetic one.
  }
}

/** Level of the utterance at its current playback position, or null if unknown. */
export function getTtsLevel(): number | null {
  if (!audio || !envelope) return null;
  const frame = Math.floor(audio.currentTime / ENVELOPE_FRAME_S);
  return frame < envelope.length ? envelope[frame] : 0;
}

/** Only a stream ending in the active conversation may trigger autoplay. */
export function shouldAutoplayFinishedReply(
  previousStreamingConversationId: string | null,
  activeId: string | null,
  streamIsActive: boolean,
  lastMessage: { id: string; role: string } | undefined,
  autoSpokenId: string | null,
): boolean {
  return previousStreamingConversationId !== null
    && previousStreamingConversationId === activeId
    && !streamIsActive
    && lastMessage !== undefined
    && lastMessage.role === 'assistant'
    && lastMessage.id !== autoSpokenId;
}

function teardown(): void {
  envelope = null;
  if (audio) {
    // Detach first: clearing src re-runs the media load algorithm, which fails
    // on an empty source and dispatches an `error` event. With the handler
    // still attached that surfaces as a bogus "Playback failed" after every
    // successful utterance. The element itself is kept: it is the unlocked one.
    audio.onended = null;
    audio.onerror = null;
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    audio = null;
  }
  if (objectUrl) {
    URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }
  if (controller) {
    controller.abort();
    controller = null;
  }
}

export const useTtsStore = create<TtsStore>((set, get) => ({
  state: 'idle',
  speakingId: null,
  error: null,
  errorId: null,
  available: null,
  autoSpokenId: null,

  ensureHealth: () => {
    if (healthProbe) return healthProbe;
    healthProbe = fetchTtsHealth()
      .then((health) => {
        set({ available: health.available });
        if (!health.available) healthProbe = null;
      })
      .catch(() => {
        set({ available: false });
        healthProbe = null;
      });
    return healthProbe;
  },

  markAutoSpoken: (id: string) => set({ autoSpokenId: id }),

  speak: async (id: string, text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;

    // Bump before teardown so a synthesis still in flight is both aborted and
    // fenced off by the token, even if the abort loses the race.
    token += 1;
    const mine = token;
    teardown();
    set({ state: 'loading', speakingId: id, error: null, errorId: null });

    const ac = new AbortController();
    controller = ac;

    try {
      const blob = await synthesizeSpeech(trimmed, { signal: ac.signal });
      if (mine !== token) return;

      const url = URL.createObjectURL(blob);
      objectUrl = url;
      const el = getPlayer();
      audio = el;
      el.src = url;
      void computeEnvelope(blob, mine);

      el.onended = () => {
        if (mine !== token) return;
        teardown();
        set({ state: 'idle', speakingId: null });
      };
      el.onerror = () => {
        if (mine !== token) return;
        teardown();
        set({ state: 'idle', speakingId: null, error: 'Playback failed', errorId: id });
      };

      await el.play();
      if (mine === token) set({ state: 'speaking', speakingId: id });
    } catch (err) {
      if (mine !== token) return;
      // Only our own abort is a silent cancel. iOS can reject play() with an
      // AbortError too; swallowing that left the state stuck in 'loading'.
      if (err instanceof DOMException && err.name === 'AbortError' && ac.signal.aborted) return;
      teardown();
      set({
        state: 'idle',
        speakingId: null,
        error: err instanceof Error ? err.message : 'Speech synthesis failed',
        errorId: id,
      });
    }
  },

  stop: () => {
    token += 1;
    teardown();
    set({ state: 'idle', speakingId: null, error: null, errorId: null });
  },
}));

/** Test seam: reset module-level playback handles between cases. */
export function __resetTtsForTests(): void {
  teardown();
  token = 0;
  healthProbe = null;
  player = null;
  unlocked = false;
  audioContext = null;
  silentUri = null;
  removeUnlockListeners();
  installUnlockListeners();
  useTtsStore.setState({
    state: 'idle',
    speakingId: null,
    error: null,
    errorId: null,
    available: null,
    autoSpokenId: null,
  });
}
