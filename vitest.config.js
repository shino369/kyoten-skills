import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    testTimeout: process.platform === 'win32' ? 20_000 : 5_000,
    include: ['skills/**/__tests__/**/*.test.{js,mjs}'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
      include: ['skills/**/scripts/*.{js,mjs}'],
      exclude: ['**/__tests__/**'],
      reportOnFailure: true,
      thresholds: {
        statements: 100,
        branches: 100,
        functions: 100,
        lines: 100,
      },
    },
    globals: true,
    clearMocks: true,
  },
});
