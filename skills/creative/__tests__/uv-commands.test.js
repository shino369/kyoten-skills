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
// install or another index (UV_SETTING keeps an index out of the environment).
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
// A uv call: `uv` (or `uv.exe`, a path, a closing quote) followed by an option
// or a subcommand. Prose such as "uv reads pyproject.toml" is not one.
const UV_SUBCOMMANDS =
  'run|init|add|remove|version|sync|lock|export|tree|format|tool|python|pip|venv|build|publish|cache|self|auth|help';
const UV_CALL = new RegExp(
  `\\buv(?:\\.exe)?["']?\\s+(?=-|(?:${UV_SUBCOMMANDS})\\b)`,
  'g'
);
// uv still reads UV_* variables (UV_INDEX_URL, UV_DEFAULT_INDEX, ...) under
// --no-config. Windows names ignore case, so any case counts.
const UV_SETTING = /\bUV_\w+/i;
const OTHER_INSTALLERS = /\buvx\b|\bpip[\dx.]*\s+(?:install|run)\b/;

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

/**
 * The lines a file tells agents to run: the fenced code of Markdown, and all
 * of a Python file (docstrings, error messages, and help text alike).
 */
function commandLines(path, text) {
  const blocks = path.endsWith('.md')
    ? fencedBlocks(text)
    : [text.split(/\r?\n/)];
  return blocks.flatMap(logicalLines);
}

/** Every uv call in the lines, each up to the next one on its line. */
function uvCommands(lines) {
  return lines.flatMap((line) => {
    const starts = [...line.matchAll(UV_CALL)].map(({ index }) => index);
    return starts.map((start, n) => line.slice(start, starts[n + 1]).trimEnd());
  });
}

/** Splits `uv run` into its options, the options it does not allow, and the program. */
function parseUvRun(command) {
  const [, subcommand, ...rest] = command.split(/\s+/);
  const tokens = rest.map(stripQuotes);
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
  return { subcommand, options, unknown, program: tokens[i] };
}

function isOldEnough(value) {
  const days = /^P(\d+)D$/.exec(value ?? '');
  return days !== null && Number(days[1]) >= MIN_AGE_DAYS;
}

/** What a documented uv command gets wrong; empty when it is safe. */
function uvProblems(command, { allowQuiet = false } = {}) {
  const { subcommand, options, unknown, program } = parseUvRun(command);
  // Options before `run` and other subcommands (pip, tool, sync, ...) skip
  // every check below, so only `uv run` itself is allowed.
  if (subcommand !== 'run') return [`expected uv run, got uv ${subcommand}`];
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
  .filter((path) => path.endsWith('.md') || path.endsWith('.py'))
  .map((path) => {
    const text = readFileSync(join(creativeDir, path), 'utf8');
    return { path, text, lines: commandLines(path, text) };
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

  test('flags any uv call that is not uv run itself', () => {
    expect(uvProblems(`uv --quiet run --no-project python a.py`)).toEqual([
      'expected uv run, got uv --quiet',
    ]);
    expect(uvProblems(`uv.exe" pip install pillow`)).toEqual([
      'expected uv run, got uv pip',
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

  test('reads all of a Python file but only the fenced code of Markdown', () => {
    const python =
      '"""Doc.\n\n  uv run --with a==1 \\\\\n    python x.py\n"""\nraise SystemExit("uv run --with b python")\n';
    const markdown =
      'uv run --with c python\n```\nuv run --with d python\n```\n';

    expect(uvCommands(commandLines('x.py', python))).toEqual([
      'uv run --with a==1 python x.py',
      'uv run --with b python")',
    ]);
    expect(uvCommands(commandLines('x.md', markdown))).toEqual([
      'uv run --with d python',
    ]);
  });

  test('finds every uv call on a line, in any string, but not prose', () => {
    const lines = [
      `${SAFE} python a.py && uv run --with pillow python b.py`,
      'raise RuntimeError("Run with: uv run --with UnityPy python x.py")',
      'uv --quiet run python c.py; & "C:\\bin\\uv.exe" pip install x',
      '/usr/local/bin/uv  run python d.py',
      'Prose: uv reads pyproject.toml, then uv, uvx, and uv_dir.',
    ];

    expect(uvCommands(lines)).toEqual([
      `${SAFE} python a.py &&`,
      'uv run --with pillow python b.py',
      'uv run --with UnityPy python x.py")',
      'uv --quiet run python c.py; & "C:\\bin\\',
      'uv.exe" pip install x',
      'uv  run python d.py',
    ]);
  });

  test('flags uv settings passed through the environment', () => {
    const lines = [
      `UV_INDEX_URL=https://mirror.test/simple ${SAFE} python a.py`,
      'export UV_DEFAULT_INDEX=https://mirror.test/simple',
      '$env:uv_extra_index_url = "https://mirror.test/simple"',
      `${SAFE} --with numpy==2.5.5 python -I a.py`,
    ];

    expect(lines.filter((line) => UV_SETTING.test(line))).toEqual(
      lines.slice(0, 3)
    );
  });

  test('flags installers other than uv run', () => {
    const lines = [
      'uvx ruff check skills',
      'python -m pip install pillow',
      'pip3 install pillow',
      'pipx run ruff',
      `${SAFE} --with pillow==13.0.0 python -I a.py`,
    ];

    expect(lines.filter((line) => OTHER_INSTALLERS.test(line))).toEqual(
      lines.slice(0, 4)
    );
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
    '%s passes no uv settings through the environment',
    (path) => {
      const { lines } = docs.find((doc) => doc.path === path);

      expect(lines.filter((line) => UV_SETTING.test(line))).toEqual([]);
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

  test.each(Object.entries(scripts))(
    '%s installs nothing except through uv run',
    (_name, command) => {
      expect(command).not.toMatch(OTHER_INSTALLERS);
      expect(command).not.toMatch(UV_SETTING);
    }
  );
});
