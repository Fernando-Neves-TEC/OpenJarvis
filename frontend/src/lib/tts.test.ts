import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./api', () => ({
  synthesizeSpeech: vi.fn(),
  fetchTtsHealth: vi.fn(),
}));

import { synthesizeSpeech, fetchTtsHealth } from './api';
import {
  useTtsStore,
  __resetTtsForTests,
  buildSilentWav,
  getAudioContext,
  shouldAutoplayFinishedReply,
  unlockAudio,
} from './tts';

const synth = vi.mocked(synthesizeSpeech);
const health = vi.mocked(fetchTtsHealth);

/** Minimal HTMLAudioElement stand-in; jsdom cannot decode or play real audio. */
class FakeAudio {
  static instances: FakeAudio[] = [];
  /** Next play() results; empty means resolve. */
  static playResults: Array<() => Promise<void>> = [];
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  src: string;
  preload = '';
  currentTime = 0;
  paused = true;
  playCalls = 0;
  pauseCalls = 0;
  playedSrcs: string[] = [];

  constructor(src = '') {
    this.src = src;
    FakeAudio.instances.push(this);
  }

  play(): Promise<void> {
    this.playCalls += 1;
    this.playedSrcs.push(this.src);
    const next = FakeAudio.playResults.shift();
    if (next) return next();
    this.paused = false;
    return Promise.resolve();
  }

  pause(): void {
    this.pauseCalls += 1;
    this.paused = true;
  }

  removeAttribute(name: string): void {
    if (name === 'src') this.src = '';
  }

  load(): void {}
}

const created: string[] = [];
const revoked: string[] = [];
let urlCounter = 0;

beforeEach(() => {
  vi.stubGlobal('Audio', FakeAudio as unknown as typeof Audio);
  // Tests run under node: a bare EventTarget stands in for document so the
  // tap-to-unlock listeners (re)installed by the reset below can be driven.
  vi.stubGlobal('document', new EventTarget());
  vi.stubGlobal('URL', {
    createObjectURL: () => {
      const url = `blob:fake/${++urlCounter}`;
      created.push(url);
      return url;
    },
    revokeObjectURL: (url: string) => {
      revoked.push(url);
    },
  });

  synth.mockReset();
  health.mockReset();

  // Reset the store first: it tears down whatever the previous test left
  // playing, and that teardown revokes a URL. Clearing the ledgers afterwards
  // keeps that bookkeeping out of this test's assertions.
  __resetTtsForTests();
  FakeAudio.instances = [];
  FakeAudio.playResults = [];
  created.length = 0;
  revoked.length = 0;
  urlCounter = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('shared voice output', () => {
  it('plays one utterance and reports which message owns it', async () => {
    synth.mockResolvedValue(new Blob(['wav']));

    await useTtsStore.getState().speak('m1', 'Hallo');

    expect(FakeAudio.instances).toHaveLength(1);
    expect(FakeAudio.instances[0].playCalls).toBe(1);
    expect(useTtsStore.getState().state).toBe('speaking');
    expect(useTtsStore.getState().speakingId).toBe('m1');
  });

  it('starting a second message stops the first instead of overlapping it', async () => {
    synth.mockResolvedValue(new Blob(['wav']));

    await useTtsStore.getState().speak('m1', 'Erste');
    const el = FakeAudio.instances[0];
    const pausesBefore = el.pauseCalls;

    await useTtsStore.getState().speak('m2', 'Zweite');

    // Same (unlocked) element, paused and handed the new utterance.
    expect(FakeAudio.instances).toHaveLength(1);
    expect(el.pauseCalls).toBeGreaterThan(pausesBefore);
    expect(el.playedSrcs).toEqual([created[0], created[1]]);
    expect(useTtsStore.getState().speakingId).toBe('m2');
    expect(revoked).toContain(created[0]);
  });

  it('aborts a synthesis that is superseded before it resolves', async () => {
    const pending = deferred<Blob>();
    synth.mockReturnValueOnce(pending.promise);
    synth.mockResolvedValueOnce(new Blob(['wav']));

    const firstCall = useTtsStore.getState().speak('m1', 'Erste');
    const signal = synth.mock.calls[0][1]?.signal as AbortSignal;
    expect(signal.aborted).toBe(false);

    await useTtsStore.getState().speak('m2', 'Zweite');
    expect(signal.aborted).toBe(true);

    // The stale response must not start playback or leak its blob URL.
    pending.resolve(new Blob(['stale']));
    await firstCall;

    expect(FakeAudio.instances).toHaveLength(1);
    expect(useTtsStore.getState().speakingId).toBe('m2');
  });

  it('stop() halts playback and revokes the blob URL', async () => {
    synth.mockResolvedValue(new Blob(['wav']));

    await useTtsStore.getState().speak('m1', 'Hallo');
    useTtsStore.getState().stop();

    expect(FakeAudio.instances[0].paused).toBe(true);
    expect(revoked).toEqual(created);
    expect(useTtsStore.getState().state).toBe('idle');
    expect(useTtsStore.getState().speakingId).toBeNull();
  });

  it('finishing playback leaves no error behind', async () => {
    synth.mockResolvedValue(new Blob(['wav']));

    await useTtsStore.getState().speak('m1', 'Hallo');
    const el = FakeAudio.instances[0];

    el.onended?.();
    // Clearing src fires a media error; the handler must already be detached.
    el.onerror?.();

    expect(useTtsStore.getState().error).toBeNull();
    expect(useTtsStore.getState().state).toBe('idle');
    expect(revoked).toEqual(created);
  });

  it('surfaces a synthesis failure', async () => {
    synth.mockRejectedValue(new Error('No text-to-speech backend available'));

    await useTtsStore.getState().speak('m1', 'Hallo');

    expect(useTtsStore.getState().error).toBe('No text-to-speech backend available');
    expect(useTtsStore.getState().errorId).toBe('m1');
    expect(useTtsStore.getState().state).toBe('idle');
  });

  it('ignores empty text', async () => {
    await useTtsStore.getState().speak('m1', '   ');
    expect(synth).not.toHaveBeenCalled();
  });

  it('probes the backend once no matter how many callers ask', () => {
    health.mockResolvedValue({ available: true });

    useTtsStore.getState().ensureHealth();
    useTtsStore.getState().ensureHealth();
    useTtsStore.getState().ensureHealth();

    expect(health).toHaveBeenCalledTimes(1);
  });

  it('shares a pending health probe so autoplay can wait for model loading', async () => {
    const pending = deferred<{ available: boolean }>();
    health.mockReturnValue(pending.promise);

    const first = useTtsStore.getState().ensureHealth();
    const second = useTtsStore.getState().ensureHealth();
    expect(second).toBe(first);
    expect(health).toHaveBeenCalledTimes(1);

    pending.resolve({ available: true });
    await first;
    expect(useTtsStore.getState().available).toBe(true);
  });

  it('retries health after an unavailable response', async () => {
    health.mockResolvedValueOnce({ available: false });
    health.mockResolvedValueOnce({ available: true });

    await useTtsStore.getState().ensureHealth();
    expect(useTtsStore.getState().available).toBe(false);
    await useTtsStore.getState().ensureHealth();

    expect(health).toHaveBeenCalledTimes(2);
    expect(useTtsStore.getState().available).toBe(true);
  });

  it('retries health after a network error', async () => {
    health.mockRejectedValueOnce(new Error('offline'));
    health.mockResolvedValueOnce({ available: true });

    await useTtsStore.getState().ensureHealth();
    expect(useTtsStore.getState().available).toBe(false);
    await useTtsStore.getState().ensureHealth();

    expect(health).toHaveBeenCalledTimes(2);
    expect(useTtsStore.getState().available).toBe(true);
  });
});

describe('autoplay gating', () => {
  it('stays silent when a finished conversation is merely restored', () => {
    expect(
      shouldAutoplayFinishedReply(null, 'chat-1', false, { role: 'assistant', id: 'old' }, null),
    ).toBe(false);
  });

  it('speaks when a stream finishes', () => {
    expect(
      shouldAutoplayFinishedReply('chat-1', 'chat-1', false, { role: 'assistant', id: 'fresh' }, null),
    ).toBe(true);
  });

  it('stays silent while the reply is still streaming', () => {
    expect(
      shouldAutoplayFinishedReply('chat-1', 'chat-1', true, { role: 'assistant', id: 'fresh' }, null),
    ).toBe(false);
  });

  it('never repeats a message it already spoke', () => {
    expect(
      shouldAutoplayFinishedReply('chat-1', 'chat-1', false, { role: 'assistant', id: 'fresh' }, 'fresh'),
    ).toBe(false);
  });

  it('ignores a trailing user message', () => {
    expect(
      shouldAutoplayFinishedReply('chat-1', 'chat-1', false, { role: 'user', id: 'u1' }, null),
    ).toBe(false);
  });

  it('does not speak an old reply when the user switches conversations mid-stream', () => {
    expect(
      shouldAutoplayFinishedReply('chat-1', 'chat-2', true, { role: 'assistant', id: 'old' }, null),
    ).toBe(false);
  });
});

describe('iOS audio unlock', () => {
  const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

  it('a tap primes the same element that later plays the reply', async () => {
    synth.mockResolvedValue(new Blob(['wav']));

    document.dispatchEvent(new Event('touchend'));
    await flush();
    await useTtsStore.getState().speak('m1', 'Olá');

    expect(FakeAudio.instances).toHaveLength(1);
    const [primer, reply] = FakeAudio.instances[0].playedSrcs;
    // blob:, not data: -- the server CSP only allows `media-src 'self' blob:`.
    expect(primer).toBe(created[0]);
    expect(primer.startsWith('blob:')).toBe(true);
    expect(reply).toBe(created[1]);
  });

  it('builds a well-formed 16-bit mono PCM WAV for the primer', () => {
    const bytes = buildSilentWav();
    const view = new DataView(bytes.buffer);
    const ascii = (at: number) => String.fromCharCode(...bytes.slice(at, at + 4));

    expect(ascii(0)).toBe('RIFF');
    expect(view.getUint32(4, true)).toBe(bytes.length - 8);
    expect(ascii(8)).toBe('WAVE');
    expect(ascii(12)).toBe('fmt ');
    expect(view.getUint16(20, true)).toBe(1); // PCM
    expect(view.getUint16(22, true)).toBe(1); // mono
    const rate = view.getUint32(24, true);
    expect(view.getUint32(28, true)).toBe(rate * 2); // byte rate
    expect(view.getUint16(32, true)).toBe(2); // block align
    expect(view.getUint16(34, true)).toBe(16);
    expect(ascii(36)).toBe('data');
    expect(view.getUint32(40, true)).toBe(bytes.length - 44);
  });

  it('reports a play() AbortError it did not cause instead of hanging', async () => {
    synth.mockResolvedValue(new Blob(['wav']));
    FakeAudio.playResults = [
      () => Promise.reject(new DOMException('interrupted', 'AbortError')),
    ];

    await useTtsStore.getState().speak('m1', 'Olá');

    expect(useTtsStore.getState().state).toBe('idle');
    expect(useTtsStore.getState().errorId).toBe('m1');
  });

  it('stops listening for taps once unlocked', async () => {
    document.dispatchEvent(new Event('touchend'));
    await flush();
    document.dispatchEvent(new Event('click'));
    await flush();

    expect(FakeAudio.instances[0].playCalls).toBe(1);
  });

  it('keeps listening after a rejected unlock', async () => {
    FakeAudio.playResults = [
      () => Promise.reject(new DOMException('blocked', 'NotAllowedError')),
    ];

    document.dispatchEvent(new Event('touchend'));
    await flush();
    document.dispatchEvent(new Event('touchend'));
    await flush();

    expect(FakeAudio.instances[0].playCalls).toBe(2);
  });

  it('never interrupts a reply that is already playing', async () => {
    synth.mockResolvedValue(new Blob(['wav']));
    await useTtsStore.getState().speak('m1', 'Olá');
    const el = FakeAudio.instances[0];

    unlockAudio();

    expect(el.playCalls).toBe(1);
    expect(el.paused).toBe(false);
  });

  it('a slow primer does not pause a reply that took the element over', async () => {
    synth.mockResolvedValue(new Blob(['wav']));
    let finishPrimer!: () => void;
    FakeAudio.playResults = [
      () => new Promise<void>((resolve) => {
        finishPrimer = resolve;
      }),
    ];

    unlockAudio();
    await useTtsStore.getState().speak('m1', 'Olá');
    const el = FakeAudio.instances[0];
    const pauses = el.pauseCalls;
    finishPrimer();
    await flush();

    expect(el.pauseCalls).toBe(pauses);
    expect(useTtsStore.getState().speakingId).toBe('m1');
  });

  it('creates one shared AudioContext and resumes it inside the tap', () => {
    const resume = vi.fn(() => Promise.resolve());
    class FakeAudioContext {
      static count = 0;
      state = 'suspended';
      resume = resume;
      constructor() {
        FakeAudioContext.count += 1;
      }
    }
    vi.stubGlobal('window', { AudioContext: FakeAudioContext });

    unlockAudio();

    expect(resume).toHaveBeenCalledTimes(1);
    expect(getAudioContext()).toBe(getAudioContext());
    expect(FakeAudioContext.count).toBe(1);
  });
});
