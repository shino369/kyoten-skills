#!/usr/bin/env node

import fs from 'node:fs';
import http from 'node:http';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const MIME_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.wasm': 'application/wasm',
  '.p2lrt': 'application/octet-stream',
  '.json': 'application/json',
  '.png': 'image/png',
  '.css': 'text/css; charset=utf-8',
};

/**
 * @param {unknown} error
 * @returns {string}
 */
export function getErrorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}

/**
 * Escape special regular expression metacharacters in a literal string.
 * @param {string} value
 * @returns {string}
 */
export function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Safely resolve a URL path inside rootDir, rejecting directory traversal.
 * @param {string} rootDir
 * @param {string} rawUrl
 * @returns {string | null}
 */
export function resolveSafeFilePath(rootDir, rawUrl) {
  let decoded;
  try {
    decoded = decodeURIComponent((rawUrl || '/').split('?')[0]);
  } catch {
    return null;
  }
  const rel = decoded === '/' ? 'index.html' : decoded.replace(/^\/+/, '');
  const resolvedRoot = path.resolve(rootDir);
  const candidate = path.resolve(resolvedRoot, rel);
  const rootPrefix = resolvedRoot.endsWith(path.sep)
    ? resolvedRoot
    : `${resolvedRoot}${path.sep}`;
  if (candidate !== resolvedRoot && !candidate.startsWith(rootPrefix)) {
    return null;
  }
  return candidate;
}

/**
 * Create a local read-only HTTP server for an export_web directory.
 * @param {string} rootDir
 * @returns {http.Server}
 */
export function createStaticServer(rootDir) {
  return http.createServer((req, res) => {
    const filePath = resolveSafeFilePath(rootDir, req.url);
    if (filePath === null) {
      res.writeHead(403);
      res.end('forbidden');
      return;
    }
    if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
      res.writeHead(404);
      res.end('not found');
      return;
    }
    const ext = path.extname(filePath).toLowerCase();
    res.writeHead(200, {
      'Content-Type': MIME_TYPES[ext] || 'application/octet-stream',
    });
    fs.createReadStream(filePath).pipe(res);
  });
}

/**
 * Parse CLI arguments for verify_web.mjs.
 * @param {string[]} argv
 */
export function parseArgs(argv) {
  /** @type {{ dir: string, expression: string | null, toggles: string[], screenshot: string | null, channel: string | null, timeout: number, json: boolean }} */
  const opts = {
    dir: '',
    expression: null,
    toggles: [],
    screenshot: null,
    channel: null,
    timeout: 15000,
    json: false,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--json') {
      opts.json = true;
      continue;
    }
    if (
      arg === '--dir' ||
      arg === '--expression' ||
      arg === '--toggle' ||
      arg === '--screenshot' ||
      arg === '--channel' ||
      arg === '--timeout'
    ) {
      const val = argv[i + 1];
      if (val === undefined) {
        throw new Error(`Missing value for ${arg}`);
      }
      i += 1;
      if (arg === '--dir') opts.dir = val;
      else if (arg === '--expression') opts.expression = val;
      else if (arg === '--toggle') opts.toggles.push(val);
      else if (arg === '--screenshot') opts.screenshot = val;
      else if (arg === '--channel') opts.channel = val;
      else {
        const ms = Number(val);
        if (!Number.isFinite(ms) || ms <= 0) {
          throw new Error(`--timeout must be a positive number, got ${val}`);
        }
        opts.timeout = ms;
      }
      continue;
    }
    throw new Error(`Unknown option: ${arg}`);
  }

  if (!opts.dir) {
    throw new Error('Missing required option: --dir <export_web_dir>');
  }
  return opts;
}

/* v8 ignore start - dynamic optional peer import */
async function defaultLoadPlaywright() {
  try {
    const cwdRequire = createRequire(path.join(process.cwd(), 'package.json'));
    const resolved = cwdRequire.resolve('playwright');
    const mod = await import(pathToFileURL(resolved).href);
    return mod.chromium ? mod : mod.default;
  } catch {
    const mod = await import('playwright');
    return mod.chromium ? mod : mod.default;
  }
}
/* v8 ignore stop */

/**
 * Smoke-test a PSD2Live export_web viewer in headless Chromium/Chrome.
 * @param {{ dir: string, expression?: string | null, toggles?: string[], screenshot?: string | null, channel?: string | null, timeout?: number }} options
 * @param {{ loadPlaywright?: () => Promise<{ chromium: { launch: (opts: object) => Promise<any> } }> }} [deps]
 */
export async function verifyWebViewer(options, deps = {}) {
  const rootDir = path.resolve(options.dir);
  if (!fs.existsSync(rootDir) || !fs.statSync(rootDir).isDirectory()) {
    throw new Error(`Directory not found: ${rootDir}`);
  }

  const timeout = options.timeout ?? 15000;
  /* v8 ignore next */
  const loadPlaywright = deps.loadPlaywright ?? defaultLoadPlaywright;
  const { chromium } = await loadPlaywright();

  const server = createStaticServer(rootDir);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = /** @type {import('node:net').AddressInfo} */ (
    server.address()
  );
  const url = `http://127.0.0.1:${port}/index.html`;

  /** @type {string[]} */
  const errors = [];
  const launchOpts = { headless: true };
  if (options.channel) {
    launchOpts.channel = options.channel;
  }
  let browser = null;
  try {
    browser = await chromium.launch(launchOpts);
    const page = await browser.newPage({
      viewport: { width: 1200, height: 900 },
    });
    page.on('console', (m) => {
      if (m.type() === 'error') {
        errors.push(m.text());
      }
    });
    page.on('pageerror', (e) => errors.push(String(e)));

    await page.goto(url, { waitUntil: 'networkidle', timeout });
    /* v8 ignore next 8 */
    await page.waitForFunction(
      () =>
        document.documentElement?.dataset?.p2lReady === '1' ||
        document.documentElement?.dataset?.p2lReady === 'error' ||
        document.querySelectorAll('#expression option').length > 1 ||
        document.querySelectorAll('#clip option').length > 1 ||
        Boolean((document.getElementById('error')?.textContent || '').trim()),
      { timeout }
    );
    await page.waitForTimeout(400);

    const expressions = await page.$$eval('#expression option', (els) =>
      els.map((e) => e.value)
    );
    const toggles = await page.$$eval('.toggle-btn', (els) =>
      els.map((e) => e.textContent)
    );

    const requestedToggles = options.toggles ?? [];
    const initialErrorText = ((await page.textContent('#error')) || '').trim();
    if (initialErrorText.length === 0 && errors.length === 0) {
      if (options.expression) {
        await page.selectOption('#expression', options.expression, { timeout });
      }
      for (const label of requestedToggles) {
        await page
          .locator('button.toggle-btn', {
            hasText: new RegExp(`^${escapeRegExp(label)}$`),
          })
          .first()
          .click({ timeout });
      }

      await page.mouse.move(1080, 360);
      await page.waitForTimeout(500);
    }

    const activeToggles = await page.$$eval('.toggle-btn', (els) =>
      els
        .filter((e) => e.getAttribute('aria-pressed') === 'true')
        .map((e) => e.textContent)
    );
    const missingToggles = requestedToggles.filter(
      (label) => !activeToggles.includes(label)
    );
    const errorText = ((await page.textContent('#error')) || '').trim();

    if (options.screenshot) {
      await page.screenshot({ path: path.resolve(options.screenshot) });
    }

    const ok =
      errors.length === 0 &&
      errorText.length === 0 &&
      missingToggles.length === 0;
    return {
      ok,
      url,
      expressions,
      toggles,
      activeToggles,
      missingToggles,
      errorText,
      errors,
      screenshot: options.screenshot ? path.resolve(options.screenshot) : null,
    };
  } finally {
    if (browser) {
      await browser.close();
    }
    await new Promise((resolve) => server.close(resolve));
  }
}

/**
 * @param {string[]} [argv]
 * @param {{ loadPlaywright?: () => Promise<any>, log?: (msg: string) => void, error?: (msg: string) => void }} [deps]
 * @returns {Promise<number>}
 */
export async function main(argv = process.argv.slice(2), deps = {}) {
  const log = deps.log ?? console.log;
  const errLog = deps.error ?? console.error;
  try {
    const opts = parseArgs(argv);
    const res = await verifyWebViewer(opts, deps);
    if (opts.json) {
      log(JSON.stringify(res, null, 2));
    } else {
      log(
        `${res.ok ? 'PASS' : 'FAIL'} | expressions=${res.expressions.length} toggles=${res.toggles.length} active=[${res.activeToggles.join(', ')}] errors=${res.errors.length}`
      );
    }
    return res.ok ? 0 : 1;
  } catch (error) {
    errLog(`ERROR: ${getErrorMessage(error)}`);
    return 2;
  }
}

function realOrResolved(targetPath) {
  try {
    return fs.realpathSync(targetPath);
  } catch {
    return path.resolve(targetPath);
  }
}

/**
 * Return true when this module is the CLI entry point (resolving symlinks/junctions).
 * @param {string} [moduleUrl]
 * @param {string} [argv1]
 * @returns {boolean}
 */
export function isMainModule(
  moduleUrl = import.meta.url,
  argv1 = process.argv[1]
) {
  if (!moduleUrl.startsWith('file:') || !argv1) return false;
  const modulePath = realOrResolved(fileURLToPath(moduleUrl));
  const scriptPath = realOrResolved(argv1);
  return modulePath === scriptPath || modulePath === `${scriptPath}.mjs`;
}

/* v8 ignore start - CLI entry point */
if (isMainModule()) {
  main()
    .then((code) => {
      process.exitCode = code;
    })
    .catch((error) => {
      console.error(`ERROR: ${getErrorMessage(error)}`);
      process.exitCode = 2;
    });
}
/* v8 ignore stop */
