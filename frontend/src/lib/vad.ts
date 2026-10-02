/**
 * Energy-based voice activity detection for hands-free voice mode.
 *
 * Pure and clock-driven so it can be unit tested without a microphone: the
 * caller feeds one RMS sample per animation frame and acts on the returned
 * event. An utterance starts only after the level stays above the threshold
 * for `speechStartMs` (a cough or a click is shorter), and ends after
 * `silenceMs` of quiet once speech has started.
 */

export interface VadOptions {
  /** RMS (0..1) above which a frame counts as speech. */
  threshold: number;
  /** Continuous speech needed before an utterance is considered started. */
  speechStartMs: number;
  /** Silence after speech that ends the utterance. */
  silenceMs: number;
  /** Hard ceiling on one utterance, so a noisy room cannot record forever. */
  maxUtteranceMs: number;
}

export const DEFAULT_VAD_OPTIONS: VadOptions = {
  threshold: 0.02,
  speechStartMs: 180,
  silenceMs: 1200,
  maxUtteranceMs: 30_000,
};

export type VadEvent = 'none' | 'speech-start' | 'speech-end';

export class VoiceActivityDetector {
  private readonly opts: VadOptions;
  private aboveSince: number | null = null;
  private quietSince: number | null = null;
  private startedAt: number | null = null;

  constructor(opts: Partial<VadOptions> = {}) {
    this.opts = { ...DEFAULT_VAD_OPTIONS, ...opts };
  }

  /** True between `speech-start` and `speech-end`. */
  get inSpeech(): boolean {
    return this.startedAt !== null;
  }

  reset(): void {
    this.aboveSince = null;
    this.quietSince = null;
    this.startedAt = null;
  }

  update(rms: number, now: number): VadEvent {
    const loud = rms >= this.opts.threshold;

    if (this.startedAt === null) {
      if (!loud) {
        this.aboveSince = null;
        return 'none';
      }
      if (this.aboveSince === null) this.aboveSince = now;
      if (now - this.aboveSince >= this.opts.speechStartMs) {
        this.startedAt = this.aboveSince;
        this.quietSince = null;
        return 'speech-start';
      }
      return 'none';
    }

    if (now - this.startedAt >= this.opts.maxUtteranceMs) {
      this.reset();
      return 'speech-end';
    }
    if (loud) {
      this.quietSince = null;
      return 'none';
    }
    if (this.quietSince === null) this.quietSince = now;
    if (now - this.quietSince >= this.opts.silenceMs) {
      this.reset();
      return 'speech-end';
    }
    return 'none';
  }
}

/** Root-mean-square of time-domain samples centred on zero (-1..1). */
export function rmsLevel(samples: Float32Array): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return Math.sqrt(sum / samples.length);
}

/**
 * Whisper transcribes non-speech (breath, keyboard, room tone) into filler
 * such as "[BLANK_AUDIO]", "(music)" or a lone "you". Sending those to the
 * model would make Jarvis answer noise, so voice mode drops them.
 */
export function isMeaningfulTranscript(text: string): boolean {
  const cleaned = text
    .replace(/\[[^\]]*\]|\([^)]*\)|\*[^*]*\*/g, ' ')
    .replace(/[^\p{L}\p{N}]+/gu, ' ')
    .trim()
    .toLowerCase();
  if (cleaned.length < 2) return false;
  return !['you', 'thank you', 'thanks for watching', 'bye'].includes(cleaned);
}
