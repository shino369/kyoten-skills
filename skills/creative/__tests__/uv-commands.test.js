import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

// Guards the uv commands that the creative skills tell agents to run. uv reads
// pyproject.toml, uv.toml, and .venv from the folder it runs in, and an
// unpinned --with takes whatever was published last, so every command must
// keep a received folder's project and config out, pin what it installs, and
// ignore uploads younger than a week.

const creativeDir = fileURLToPath(new URL('..', import.meta.url));
const repoRoot = join(creativeDir, '..', '..');

// The only options a documented command may use. Anything else (short flags,
// --index, --with-requirements, ...) fails, so it cannot hide an unpinned
// install or another index.
const VALUE_OPTIONS = new Set(['--python', '--with', '--exclude-newer']);
const FLAG_OPTIONS = new Set([
  '--no-project',
  '--no-config',
  '--no-build',
  '--quiet',
]);
// Published only as source, so a command that installs one has to build
// (tpk-ar: needed by UnityPy 1.25.3 and later; see the game-image-extraction
// SKILL.md).
const SOURCE_ONLY = new Set(['tpk-ar']);
const MIN_AGE_DAYS = 7;
const PINNED =
  /^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[A-Za-z0-9,._-]+\])?==(\d[\w.+!-]*)$/;
const OTHER_INSTALLERS = /\buvx\b|\buv tool (?:run|install)\b|\bpip install\b/;

function stripQuotes(token) {
  return token.replace(/^['"]|['"]$/g, '');
}

function normalizeName(name) {
  return name.toLowerCase().replace(/[-_.]+/g, '-');
}

/** Lines of each fenced code block in a Markdown file. */
function fencedBlocks(markdown) {
  const blocks = [];
  let block = null;
  for (const line of markdown.split(/\r?\n/)) {
    if (/^\s*```/.test(line)) {
      if (block) blocks.push(block);
      block = block ? null : [];
    } else if (block) {
      block.push(line);
    }
  }
  return blocks;
}

/** The module docstring of a Python file. */
function docstring(python) {
  return python.match(/^(?:#!.*\r?\n)?\s*"""([\s\S]*?)"""/)?.[1] ?? '';
}

/** Joins bash `\`, PowerShell backtick, and Python-source `\\` continuations. */
function logicalLines(lines) {
  const joined = [];
  let pending = '';
  for (const line of lines) {
    const text = pending + line.trim();
    const continuation = text.match(/\s*(?:\\{1,2}|`)$/);
    if (continuation) {
      pending = `${text.slice(0, continuation.index)} `;
    } else {
      joined.push(text);
      pending = '';
    }
  }
  if (pending) joined.push(pending.trimEnd());
  return joined;
}

function uvCommands(lines) {
  return lines.flatMap((line) => {
    const at = line.indexOf('uv run ');
    return at === -1 ? [] : [line.slice(at)];
  });
}

/** Splits `uv run` into its options, the options it does not allow, and the program. */
function parseUvRun(command) {
  const tokens = command.split(/\s+/).slice(2).map(stripQuotes);
  const options = [];
  const unknown = [];
  let i = 0;
  for (; i < tokens.length && tokens[i].startsWith('-'); i += 1) {
    const [flag, inline] = tokens[i].split(/=(.*)/s, 2);
    if (VALUE_OPTIONS.has(flag)) {
      options.push([flag, inline ?? tokens[i + 1]]);
      if (inline === undefined) i += 1;
    } else if (FLAG_OPTIONS.has(flag) && inline === undefined) {
      options.push([flag]);
    } else {
      unknown.push(tokens[i]);
    }
  }
  return { options, unknown, program: tokens[i] };
}

function isOldEnough(value) {
  const days = /^P(\d+)D$/.exec(value ?? '');
  return days !== null && Number(days[1]) >= MIN_AGE_DAYS;
}

/** What a documented uv command gets wrong; empty when it is safe. */
function uvProblems(command, { allowQuiet = false } = {}) {
  const { options, unknown, program } = parseUvRun(command);
  const flags = new Set(options.map(([flag]) => flag));
  const specs = options
    .filter(([flag]) => flag === '--with')
    .map(([, value]) => value ?? '');
  const problems = unknown.map((token) => `unknown option ${token}`);
  for (const flag of ['--no-project', '--no-config']) {
    if (!flags.has(flag)) problems.push(`missing ${flag}`);
  }
  const age = options.find(([flag]) => flag === '--exclude-newer')?.[1];
  if (!isOldEnough(age)) {
    problems.push(`needs --exclude-newer P${MIN_AGE_DAYS}D or longer`);
  }
  const buildsSourceOnly = specs.some((spec) => {
    const name = PINNED.exec(spec)?.[1];
    return name !== undefined && SOURCE_ONLY.has(normalizeName(name));
  });
  if (!flags.has('--no-build') && !buildsSourceOnly) {
    problems.push('missing --no-build');
  }
  if (flags.has('--quiet') && !allowQuiet) {
    problems.push('--quiet hides what uv installs');
  }
  for (const spec of specs) {
    if (!PINNED.test(spec)) problems.push(`unpinned --with ${spec}`);
  }
  if (program !== 'python') {
    problems.push(`runs ${program ?? 'nothing'} instead of python`);
  }
  return problems;
}

/** Packages pinned to more than one version across the given commands. */
function pinConflicts(commands) {
  const versions = new Map();
  for (const command of commands) {
    for (const [flag, value] of parseUvRun(command).options) {
      const pin = flag === '--with' ? PINNED.exec(value ?? '') : null;
      if (pin) {
        const name = normalizeName(pin[1]);
        versions.set(name, new Set([...(versions.get(name) ?? []), pin[3]]));
      }
    }
  }
  return [...versions]
    .filter(([, found]) => found.size > 1)
    .map(([name, found]) => `${name}: ${[...found].sort().join(', ')}`);
}

const SAFE =
  'uv run --no-project --no-config --no-build --exclude-newer P7D --python 3.12';

const docs = readdirSync(creativeDir, { recursive: true })
  .filter((path) => {
    const parts = path.split(/[\\/]/);
    return (
      path.endsWith('.md') ||
      (path.endsWith('.py') && parts.at(-2) === 'scripts')
    );
  })
  .map((path) => {
    const text = readFileSync(join(creativeDir, path), 'utf8');
    const blocks = path.endsWith('.md')
      ? fencedBlocks(text)
      : [docstring(text).split(/\r?\n/)];
    return { path, text, lines: blocks.flatMap(logicalLines) };
  });

const { scripts } = JSON.parse(
  readFileSync(join(repoRoot, 'package.json'), 'utf8')
);
const uvScripts = Object.entries(scripts).flatMap(([name, command]) =>
  uvCommands([command]).map((uv) => [name, uv])
);

describe('uvProblems', () => {
  test('flags the old unpinned helper', () => {
    const old =
      'uv run --quiet --python 3.12 --with pillow --with numpy python -I "$IA_PY" "$@"; }';

    expect(uvProblems(old)).toEqual([
      'missing --no-project',
      'missing --no-config',
      'needs --exclude-newer P7D or longer',
      'missing --no-build',
      '--quiet hides what uv installs',
      'unpinned --with pillow',
      'unpinned --with numpy',
    ]);
  });

  test('accepts a pinned, isolated helper with extras and quotes', () => {
    const helper = `${SAFE} --with 'psd-tools[composite]==1.27.0' --with numpy==2.5.5 python -I $X @args }`;

    expect(uvProblems(helper)).toEqual([]);
  });

  test('lets only a command that installs a source-only package build', () => {
    const source = `uv run --no-project --no-config --exclude-newer P7D --with tpk_ar==0.2.4 python -I x.py`;
    const wheels = `uv run --no-project --no-config --exclude-newer P7D --with pillow==13.0.0 python -I x.py`;

    expect(uvProblems(source)).toEqual([]);
    expect(uvProblems(wheels)).toEqual(['missing --no-build']);
  });

  test('flags short options, --flag=value specs, and other indexes', () => {
    const sneaky = `${SAFE} -q --with=pillow --index https://example.test/simple --with numpy==2.5.5 python`;

    expect(uvProblems(sneaky)).toEqual([
      'unknown option -q',
      'unknown option --index',
      'unpinned --with pillow',
      'runs https://example.test/simple instead of python',
    ]);
  });

  test('flags an --exclude-newer that is a date or shorter than a week', () => {
    const date = `uv run --no-project --no-config --no-build --exclude-newer 2999-01-01 python`;
    const short = `uv run --no-project --no-config --no-build --exclude-newer=P3D python`;

    expect(uvProblems(date)).toEqual(['needs --exclude-newer P7D or longer']);
    expect(uvProblems(short)).toEqual(['needs --exclude-newer P7D or longer']);
  });

  test('flags a range instead of an exact pin', () => {
    expect(uvProblems(`${SAFE} --with pillow>=13 python`)).toEqual([
      'unpinned --with pillow>=13',
    ]);
  });

  test('allows --quiet only when asked', () => {
    const quiet = `${SAFE} --quiet python -m pytest`;

    expect(uvProblems(quiet)).toEqual(['--quiet hides what uv installs']);
    expect(uvProblems(quiet, { allowQuiet: true })).toEqual([]);
  });
});

describe('finding commands', () => {
  test('reads only fenced code and keeps blocks apart', () => {
    const markdown = [
      'Prose with `uv run --python 3.12` is ignored.',
      '```bash',
      'uv run --no-project \\',
      '```',
      '   ```powershell',
      '   python b.py',
      '   ```',
    ].join('\n');

    expect(fencedBlocks(markdown)).toEqual([
      ['uv run --no-project \\'],
      ['   python b.py'],
    ]);
  });

  test('joins bash, PowerShell, and Python-source continuations', () => {
    const lines = [
      'uv run --no-project \\',
      '  --no-config python a.py',
      'uv run --no-build `',
      '  python b.py',
      '  uv run --with x==1 \\\\',
      '    python c.py',
      'trailing \\',
    ];

    expect(logicalLines(lines)).toEqual([
      'uv run --no-project --no-config python a.py',
      'uv run --no-build python b.py',
      'uv run --with x==1 python c.py',
      'trailing',
    ]);
  });

  test('reads the module docstring after a shebang', () => {
    const python =
      '#!/usr/bin/env python3\n"""Doc.\n\n  uv run x\n"""\nX = """not this"""\n';

    expect(docstring(python)).toBe('Doc.\n\n  uv run x\n');
    expect(docstring('import os\n')).toBe('');
  });

  test('reports a package pinned to two versions', () => {
    const commands = [
      `${SAFE} --with Pillow==13.0.0 python`,
      `${SAFE} --with pillow==13.0.1 --with numpy==2.5.5 python`,
      `${SAFE} --with numpy==2.5.5 python`,
    ];

    expect(pinConflicts(commands)).toEqual(['pillow: 13.0.0, 13.0.1']);
  });
});

describe('creative skill docs', () => {
  test('document helpers for the skills that run Python', () => {
    const counted = Object.fromEntries(
      docs.map(({ path, lines }) => [path, uvCommands(lines).length])
    );

    expect(
      counted[join('transparent-background', 'SKILL.md')]
    ).toBeGreaterThanOrEqual(2);
    expect(
      counted[join('game-image-extraction', 'SKILL.md')]
    ).toBeGreaterThanOrEqual(4);
    expect(
      counted[join('transparent-background', 'scripts', 'imgalpha.py')]
    ).toBe(1);
    expect(
      counted[join('game-image-extraction', 'scripts', 'game_extract.py')]
    ).toBe(1);
  });

  test.each(docs.map(({ path }) => path))(
    '%s isolates and pins every uv command',
    (path) => {
      const { lines } = docs.find((doc) => doc.path === path);

      const bad = uvCommands(lines)
        .map((command) => ({ command, problems: uvProblems(command) }))
        .filter(({ problems }) => problems.length > 0);

      expect(bad).toEqual([]);
    }
  );

  test.each(docs.map(({ path }) => path))(
    '%s installs nothing except through uv run',
    (path) => {
      const { lines } = docs.find((doc) => doc.path === path);

      expect(lines.filter((line) => OTHER_INSTALLERS.test(line))).toEqual([]);
    }
  );

  test.each(docs.map(({ path }) => path))(
    '%s never finds a script through the current repository',
    (path) => {
      const { text } = docs.find((doc) => doc.path === path);

      expect(text).not.toMatch(/git rev-parse/);
    }
  );

  test('pin each package to one version everywhere', () => {
    const commands = [
      ...docs.flatMap(({ lines }) => uvCommands(lines)),
      ...uvScripts.map(([, command]) => command),
    ];

    expect(pinConflicts(commands)).toEqual([]);
  });
});

describe('package.json test scripts', () => {
  test('exist', () => {
    expect(uvScripts.length).toBeGreaterThan(0);
  });

  test.each(uvScripts)('%s isolates and pins its uv run', (_name, command) => {
    expect(uvProblems(command, { allowQuiet: true })).toEqual([]);
  });
});
