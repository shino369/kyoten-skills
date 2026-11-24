/**
 * Tests for verify_web.mjs (100% statement/branch/function/line coverage).
 */
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import {
  MIME_TYPES,
  getErrorMessage,
  escapeRegExp,
  resolveSafeFilePath,
  createStaticServer,
  parseArgs,
  isMainModule,
  verifyWebViewer,
  main,
} from '../verify_web.mjs';

function makeTempWebDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'live2d-web-test-'));
  fs.writeFileSync(
    path.join(dir, 'index.html'),
    '<html><body>ok</body></html>'
  );
  fs.writeFileSync(path.join(dir, 'p2l.js'), 'export const x = 1;');
  fs.writeFileSync(path.join(dir, 'data.bin'), Buffer.from([1, 2, 3]));
  fs.mkdirSync(path.join(dir, 'subdir'));
  return dir;
}

function makeFakePlaywright({
  consoleErrors = [],
  pageErrors = [],
  errText = '',
  exprOptions = ['', 'exp_smile'],
  toggleLabels = ['Headdress OFF', 'Sketchbook'],
  activeToggles = ['Headdress OFF'],
} = {}) {
  const actions = [];
  const listeners = {};
  const page = {
    on(event, fn) {
      listeners[event] = fn;
    },
    async goto(url, opts) {
      actions.push(['goto', url, opts]);
      for (const txt of consoleErrors) {
        listeners.console?.({ type: () => 'error', text: () => txt });
      }
      listeners.console?.({ type: () => 'log', text: () => 'ignored info' });
      for (const err of pageErrors) {
        listeners.pageerror?.(err);
      }
    },
    async waitForFunction(fn, opts) {
      actions.push(['waitForFunction', typeof fn, opts]);
    },
    async waitForTimeout(ms) {
      actions.push(['waitForTimeout', ms]);
    },
    async $$eval(selector, fn) {
      actions.push(['$$eval', selector]);
      if (selector === '#expression option') {
        return fn(exprOptions.map((value) => ({ value })));
      }
      const fakeBtns = toggleLabels.map((textContent) => ({
        textContent,
        getAttribute: (k) =>
          k === 'aria-pressed' && activeToggles.includes(textContent)
            ? 'true'
            : 'false',
      }));
      return fn(fakeBtns);
    },
    async selectOption(selector, value, opts) {
      actions.push(['selectOption', selector, value, opts]);
    },
    locator(selector, opts) {
      return {
        first() {
          return {
            async click(clickOpts) {
              actions.push(['locator.click', selector, opts, clickOpts]);
            },
          };
        },
      };
    },
    mouse: {
      async move(x, y) {
        actions.push(['mouse.move', x, y]);
      },
    },
    async textContent(selector) {
      actions.push(['textContent', selector]);
      return errText;
    },
    async screenshot(opts) {
      actions.push(['screenshot', opts.path]);
    },
  };
  let closed = false;
  const browser = {
    async newPage(opts) {
      actions.push(['newPage', opts]);
      return page;
    },
    async close() {
      closed = true;
    },
  };
  const chromium = {
    async launch(opts) {
      actions.push(['launch', opts]);
      return browser;
    },
  };
  return {
    actions,
    isClosed: () => closed,
    loadPlaywright: async () => ({ chromium }),
  };
}

describe('verify_web', () => {
  const tempDirs = [];

  afterEach(() => {
    while (tempDirs.length > 0) {
      fs.rmSync(tempDirs.pop(), { recursive: true, force: true });
    }
    vi.restoreAllMocks();
  });

  test('getErrorMessage and escapeRegExp format inputs safely', () => {
    expect(getErrorMessage(new Error('fail'))).toBe('fail');
    expect(getErrorMessage(42)).toBe('42');
    expect(escapeRegExp('Book (v1.0)+?')).toBe('Book \\(v1\\.0\\)\\+\\?');
  });

  test('isMainModule resolves symlinks/junctions and handles edge cases', () => {
    const dir = makeTempWebDir();
    tempDirs.push(dir);
    const realFile = path.join(dir, 'p2l.js');
    const url = pathToFileURL(realFile).href;

    expect(isMainModule(url, realFile)).toBe(true);
    expect(isMainModule('https://example.com/a.js', realFile)).toBe(false);
    expect(isMainModule(url, '')).toBe(false);
    expect(isMainModule(url, path.join(dir, 'missing.js'))).toBe(false);

    // Linked directory (junction on Windows, symlink on POSIX) must still match realpath
    const linkedDir = path.join(os.tmpdir(), `live2d-link-${Date.now()}`);
    fs.symlinkSync(
      dir,
      linkedDir,
      process.platform === 'win32' ? 'junction' : 'dir'
    );
    try {
      expect(isMainModule(url, path.join(linkedDir, 'p2l.js'))).toBe(true);
    } finally {
      fs.rmSync(linkedDir, { recursive: true, force: true });
    }
  });

  test('resolveSafeFilePath resolves valid files and blocks traversal', () => {
    const dir = makeTempWebDir();
    tempDirs.push(dir);

    expect(resolveSafeFilePath(dir, '/')).toBe(path.join(dir, 'index.html'));
    expect(resolveSafeFilePath(dir, '')).toBe(path.join(dir, 'index.html'));
    expect(resolveSafeFilePath(dir, '/p2l.js?v=1')).toBe(
      path.join(dir, 'p2l.js')
    );
    expect(resolveSafeFilePath(dir, '/..foo.js')).toBe(
      path.join(dir, '..foo.js')
    );
    expect(resolveSafeFilePath(dir, '/../secret.txt')).toBeNull();
    expect(resolveSafeFilePath(dir, '/%E0%A4%A')).toBeNull();
    // Root path with trailing separator (e.g. filesystem root)
    expect(resolveSafeFilePath(path.parse(dir).root, '/index.html')).toBe(
      path.join(path.parse(dir).root, 'index.html')
    );
  });

  test('createStaticServer serves files, MIME types, 403 and 404', async () => {
    const dir = makeTempWebDir();
    tempDirs.push(dir);

    const server = createStaticServer(dir);
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const { port } = /** @type {import('node:net').AddressInfo} */ (
      server.address()
    );
    const base = `http://127.0.0.1:${port}`;

    try {
      const r1 = await fetch(`${base}/`);
      expect(r1.status).toBe(200);
      expect(r1.headers.get('content-type')).toBe(MIME_TYPES['.html']);

      const r2 = await fetch(`${base}/p2l.js`);
      expect(r2.status).toBe(200);
      expect(r2.headers.get('content-type')).toBe(MIME_TYPES['.js']);

      const r3 = await fetch(`${base}/data.bin`);
      expect(r3.status).toBe(200);
      expect(r3.headers.get('content-type')).toBe('application/octet-stream');

      const r4 = await fetch(`${base}/missing.js`);
      expect(r4.status).toBe(404);

      const r5 = await fetch(`${base}/subdir`);
      expect(r5.status).toBe(404);

      // Raw HTTP request so client URL normalization does not strip %2e%2e
      const status403 = await new Promise((resolve, reject) => {
        http
          .get(
            { hostname: '127.0.0.1', port, path: '/%2e%2e/outside' },
            (res) => {
              res.resume();
              resolve(res.statusCode);
            }
          )
          .on('error', reject);
      });
      expect(status403).toBe(403);
    } finally {
      await new Promise((resolve) => server.close(resolve));
    }
  });

  test('parseArgs parses flags and rejects invalid input', () => {
    const parsed = parseArgs([
      '--dir',
      './web',
      '--expression',
      'exp_cry',
      '--toggle',
      'Headdress OFF',
      '--toggle',
      'Sketchbook',
      '--screenshot',
      'out.png',
      '--channel',
      'chrome',
      '--timeout',
      '8000',
      '--json',
    ]);
    expect(parsed).toEqual({
      dir: './web',
      expression: 'exp_cry',
      toggles: ['Headdress OFF', 'Sketchbook'],
      screenshot: 'out.png',
      channel: 'chrome',
      timeout: 8000,
      json: true,
    });

    expect(() => parseArgs([])).toThrow('--dir');
    expect(() => parseArgs(['--dir'])).toThrow('Missing value');
    expect(() => parseArgs(['--dir', './web', '--timeout', '0'])).toThrow(
      'positive'
    );
    expect(() => parseArgs(['--dir', './web', '--unknown'])).toThrow('Unknown');
  });

  test('verifyWebViewer runs happy path, toggles, screenshot, and error paths', async () => {
    const dir = makeTempWebDir();
    tempDirs.push(dir);

    const pw = makeFakePlaywright();
    const res = await verifyWebViewer(
      {
        dir,
        expression: 'exp_smile',
        toggles: ['Headdress OFF'],
        screenshot: path.join(dir, 'shot.png'),
        channel: 'chrome',
        timeout: 5000,
      },
      { loadPlaywright: pw.loadPlaywright }
    );
    expect(res.ok).toBe(true);
    expect(res.expressions).toEqual(['', 'exp_smile']);
    expect(res.activeToggles).toEqual(['Headdress OFF']);
    expect(res.missingToggles).toEqual([]);
    expect(res.errors).toEqual([]);
    expect(pw.isClosed()).toBe(true);
    // Exact RegExp match and explicit timeout passed to selectOption & click (r1-8, r2-1)
    const selectAction = pw.actions.find((a) => a[0] === 'selectOption');
    expect(selectAction[3]).toEqual({ timeout: 5000 });
    const clickAction = pw.actions.find((a) => a[0] === 'locator.click');
    expect(clickAction[1]).toBe('button.toggle-btn');
    expect(clickAction[2].hasText).toEqual(/^Headdress OFF$/);
    expect(clickAction[3]).toEqual({ timeout: 5000 });

    // When #error is already populated on load, skip selectOption/click and fail fast (r2-1)
    const pwLoadFail = makeFakePlaywright({
      consoleErrors: ['wasm failed'],
      errText: 'Runtime init failed',
      toggleLabels: [],
      activeToggles: [],
    });
    const resLoadFail = await verifyWebViewer(
      {
        dir,
        expression: 'exp_cry',
        toggles: ['Headdress OFF'],
        timeout: 2500,
      },
      { loadPlaywright: pwLoadFail.loadPlaywright }
    );
    expect(resLoadFail.ok).toBe(false);
    expect(resLoadFail.errorText).toBe('Runtime init failed');
    expect(resLoadFail.missingToggles).toEqual(['Headdress OFF']);
    expect(pwLoadFail.actions.some((a) => a[0] === 'selectOption')).toBe(false);
    expect(pwLoadFail.actions.some((a) => a[0] === 'locator.click')).toBe(
      false
    );

    // Requested toggle that fails to stay aria-pressed="true" must fail QA (r1-3)
    const pwUnpressed = makeFakePlaywright({ activeToggles: [] });
    const resUnpressed = await verifyWebViewer(
      { dir, toggles: ['Headdress OFF'] },
      { loadPlaywright: pwUnpressed.loadPlaywright }
    );
    expect(resUnpressed.ok).toBe(false);
    expect(resUnpressed.missingToggles).toEqual(['Headdress OFF']);

    // Failure from console error, pageerror, and #error text (without optional args)
    const pwErr = makeFakePlaywright({
      consoleErrors: ['webgl failed'],
      pageErrors: [new Error('boom')],
      errText: 'Load failed',
    });
    const resErr = await verifyWebViewer(
      { dir },
      { loadPlaywright: pwErr.loadPlaywright }
    );
    expect(resErr.ok).toBe(false);
    expect(resErr.errors).toEqual(['webgl failed', 'Error: boom']);
    expect(resErr.errorText).toBe('Load failed');
    expect(pwErr.isClosed()).toBe(true);

    // Non-existent directory and file-instead-of-directory
    await expect(
      verifyWebViewer(
        { dir: path.join(dir, 'nope') },
        { loadPlaywright: pw.loadPlaywright }
      )
    ).rejects.toThrow('Directory not found');
    await expect(
      verifyWebViewer(
        { dir: path.join(dir, 'index.html') },
        { loadPlaywright: pw.loadPlaywright }
      )
    ).rejects.toThrow('Directory not found');

    // Null #error textContent handled cleanly
    const pwNullErr = makeFakePlaywright({ errText: null });
    const resNull = await verifyWebViewer(
      { dir },
      { loadPlaywright: pwNullErr.loadPlaywright }
    );
    expect(resNull.ok).toBe(true);
    expect(resNull.errorText).toBe('');

    // Browser launch error still shuts down HTTP server cleanly
    await expect(
      verifyWebViewer(
        { dir },
        {
          loadPlaywright: async () => ({
            chromium: {
              launch: async () => {
                throw new Error('launch failed');
              },
            },
          }),
        }
      )
    ).rejects.toThrow('launch failed');
  });

  test('main handles JSON output, human output, failed QA, and bad args', async () => {
    const dir = makeTempWebDir();
    tempDirs.push(dir);
    const logs = [];
    const errs = [];

    const pwOk = makeFakePlaywright();
    const code1 = await main(['--dir', dir, '--json'], {
      loadPlaywright: pwOk.loadPlaywright,
      log: (m) => logs.push(m),
      error: (m) => errs.push(m),
    });
    expect(code1).toBe(0);
    expect(JSON.parse(logs[0]).ok).toBe(true);

    // Human PASS using default console.log/console.error
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const codePass = await main(['--dir', dir], {
      loadPlaywright: pwOk.loadPlaywright,
    });
    expect(codePass).toBe(0);
    expect(logSpy).toHaveBeenCalledWith(expect.stringContaining('PASS'));

    const pwFail = makeFakePlaywright({ consoleErrors: ['bad'] });
    const code2 = await main(['--dir', dir], {
      loadPlaywright: pwFail.loadPlaywright,
      log: (m) => logs.push(m),
      error: (m) => errs.push(m),
    });
    expect(code2).toBe(1);
    expect(logs.some((l) => l.includes('FAIL'))).toBe(true);

    const code3 = await main([], {
      loadPlaywright: pwOk.loadPlaywright,
    });
    expect(code3).toBe(2);
    expect(errSpy).toHaveBeenCalledWith(expect.stringContaining('ERROR'));
  });
});
