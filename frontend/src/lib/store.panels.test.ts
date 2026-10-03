import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

class MemoryStorage {
  private store = new Map<string, string>();

  getItem(key: string): string | null {
    return this.store.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }
}

function stubViewport(narrow: boolean): void {
  vi.stubGlobal('window', {
    matchMedia: (query: string) => ({ matches: narrow && query === '(max-width: 767px)' }),
  });
}

beforeEach(() => {
  vi.resetModules();
  (globalThis as unknown as { localStorage: MemoryStorage }).localStorage =
    new MemoryStorage();
});

afterEach(() => {
  vi.unstubAllGlobals();
  (globalThis as unknown as { localStorage?: MemoryStorage }).localStorage =
    undefined;
});

describe('initial panel state', () => {
  it('starts with sidebar and system panel closed on phone-width screens', async () => {
    stubViewport(true);
    const { useAppStore } = await import('./store');

    expect(useAppStore.getState().sidebarOpen).toBe(false);
    expect(useAppStore.getState().systemPanelOpen).toBe(false);
  });

  it('keeps both panels open on wider screens', async () => {
    stubViewport(false);
    const { useAppStore } = await import('./store');

    expect(useAppStore.getState().sidebarOpen).toBe(true);
    expect(useAppStore.getState().systemPanelOpen).toBe(true);
  });
});
