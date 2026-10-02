import { describe, expect, it } from 'vitest';
import { stripThinkTags, toSpeakableText } from './message-text';

describe('stripThinkTags', () => {
  it('removes reasoning blocks', () => {
    expect(stripThinkTags('<think>plan</think>Answer')).toBe('Answer');
  });
});

describe('toSpeakableText', () => {
  it('drops markdown syntax but keeps the words', () => {
    const md = [
      '<think>hidden</think>',
      '## Weather in **Frankfurt**',
      '- Temperature: *18°C*',
      '- See [AccuWeather](https://accuweather.com) or https://example.com',
      '> Bring an `umbrella`.',
    ].join('\n');
    expect(toSpeakableText(md)).toBe(
      'Weather in Frankfurt\nTemperature: 18°C\nSee AccuWeather or\nBring an umbrella.',
    );
  });

  it('replaces code blocks and flattens tables', () => {
    const md = 'Run this:\n```bash\nls -la\n```\n| Day | Temp |\n|---|---|\n| Mon | 18 |';
    const spoken = toSpeakableText(md);
    expect(spoken).toContain('(code omitted)');
    expect(spoken).not.toMatch(/ls -la|---|\|/);
    expect(spoken).toContain('Mon, 18');
  });

  it('leaves snake_case and arithmetic alone', () => {
    expect(toSpeakableText('Use file_read, 2 * 3 = 6')).toBe('Use file_read, 2 * 3 = 6');
  });
});
