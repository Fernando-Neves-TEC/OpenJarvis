import { describe, expect, it } from 'vitest';
import { VoiceActivityDetector, isMeaningfulTranscript, rmsLevel } from './vad';

const opts = { threshold: 0.1, speechStartMs: 100, silenceMs: 500, maxUtteranceMs: 5000 };

/** Feed a constant level every `step` ms and collect the non-empty events. */
function feed(vad: VoiceActivityDetector, level: number, from: number, to: number, step = 20) {
  const events: Array<[number, string]> = [];
  for (let t = from; t <= to; t += step) {
    const ev = vad.update(level, t);
    if (ev !== 'none') events.push([t, ev]);
  }
  return events;
}

describe('VoiceActivityDetector', () => {
  it('ignores silence', () => {
    const vad = new VoiceActivityDetector(opts);
    expect(feed(vad, 0.01, 0, 2000)).toEqual([]);
    expect(vad.inSpeech).toBe(false);
  });

  it('ignores a blip shorter than speechStartMs', () => {
    const vad = new VoiceActivityDetector(opts);
    expect(feed(vad, 0.5, 0, 60)).toEqual([]);
    expect(feed(vad, 0.01, 80, 1000)).toEqual([]);
  });

  it('starts after sustained speech and ends after silence', () => {
    const vad = new VoiceActivityDetector(opts);
    expect(feed(vad, 0.5, 0, 400)).toEqual([[100, 'speech-start']]);
    expect(vad.inSpeech).toBe(true);
    expect(feed(vad, 0.01, 420, 2000)).toEqual([[920, 'speech-end']]);
    expect(vad.inSpeech).toBe(false);
  });

  it('a short pause mid-sentence does not end the utterance', () => {
    const vad = new VoiceActivityDetector(opts);
    feed(vad, 0.5, 0, 400);
    expect(feed(vad, 0.01, 420, 700)).toEqual([]);
    expect(feed(vad, 0.5, 720, 1000)).toEqual([]);
    expect(vad.inSpeech).toBe(true);
  });

  it('caps an utterance at maxUtteranceMs', () => {
    const vad = new VoiceActivityDetector(opts);
    const events = feed(vad, 0.5, 0, 6000);
    expect(events[0]).toEqual([100, 'speech-start']);
    expect(events[1]).toEqual([5000, 'speech-end']);
  });
});

describe('rmsLevel', () => {
  it('is zero for empty or silent input and matches a constant signal', () => {
    expect(rmsLevel(new Float32Array())).toBe(0);
    expect(rmsLevel(new Float32Array(8))).toBe(0);
    expect(rmsLevel(new Float32Array([0.5, -0.5, 0.5, -0.5]))).toBeCloseTo(0.5);
  });
});

describe('isMeaningfulTranscript', () => {
  it.each(['', '   ', '[BLANK_AUDIO]', '(music)', 'you', 'You.', 'Thank you.', '...'])(
    'drops filler %j',
    (text) => expect(isMeaningfulTranscript(text)).toBe(false),
  );

  it.each(["What's the weather in Frankfurt?", 'Turn on the lights', 'Thank you, Jarvis'])(
    'keeps %j',
    (text) => expect(isMeaningfulTranscript(text)).toBe(true),
  );
});
