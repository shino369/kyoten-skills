import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

// Guards the uv commands that the creative skills tell agents to run. uv reads
// pyproject.toml, uv.toml, and .venv from the folder it runs in, and an
// unpinned --with takes whatever was published last, so every command must
// keep a received folder's project and config out and pin what it installs.

const creativeDir = fileURLToPath(new URL('..', import.meta.url));
const repoRoot = join(creativeDir, '..', '..');

const VALUE_FLAGS = new Set(['--python', '--with', '--exclude-newer']);
const PINNED =
  /^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==\d[\w.+!-]*$/;

function stripQuotes(token) {
  return token.replace(/^['"]|['"]$/g, '');
}

/** `uv run` commands in fenced code blocks, line continuations joined. */
function fencedUvCommands(markdown) {
  const commands = [];
  let inFence = false;
  let pending = '';
  for (const line of markdown.split(/\r?\n/)) {
    if (/^\s*```/.test(line)) {
      inFence = !inFence;
      pending = '';
      continue;
    }
    if (!inFence) continue;
    const joined = pending + line.trim();
    if (/[\\`]$/.test(joined)) {
      pending = `${joined.slice(0, -1).trimEnd()} `;
      continue;
    }
    pending = '';
    const at = joined.indexOf('uv run ');
    if (at !== -1) commands.push(joined.slice(at));
  }
  return commands;
}

/** Options between `uv run` and the program it runs, as [flag, value?] pairs. */
function uvOptions(command) {
  const tokens = command.split(/\s+/).slice(2).map(stripQuotes);
  const options = [];
  for (let i = 0; i < tokens.length && tokens[i].startsWith('--'); i += 1) {
    if (VALUE_FLAGS.has(tokens[i])) {
      options.push([tokens[i], tokens[i + 1]]);
      i += 1;
    } else {
      options.push([tokens[i]]);
    }
  }
  return options;
}

/** What a documented helper command gets wrong; empty when it is safe. */
function helperProblems(command) {
  const options = uvOptions(command);
  const flags = new Set(options.map(([flag]) => flag));
  const problems = [];
  for (const flag of ['--no-project', '--no-config']) {
    if (!flags.has(flag)) problems.push(`missing ${flag}`);
  }
  if (!flags.has('--no-build') && !flags.has('--exclude-newer')) {
    problems.push(
      'needs --no-build, or --exclude-newer when a build is unavoidable'
    );
  }
  if (flags.has('--quiet')) problems.push('--quiet hides what uv installs');
  for (const [flag, value] of options) {
    if (flag === '--with' && !PINNED.test(value ?? '')) {
      problems.push(`unpinned --with ${value}`);
    }
  }
  return problems;
}

const markdownFiles = readdirSync(creativeDir, { recursive: true })
  .filter((path) => path.endsWith('.md'))
  .map((path) => ({
    path,
    text: readFileSync(join(creativeDir, path), 'utf8'),
  }));

describe('helperProblems', () => {
  it('flags the old unpinned helper', () => {
    const old =
      'uv run --quiet --python 3.12 --with pillow --with numpy python -I "$IA_PY" "$@"; }';
    expect(helperProblems(old)).toEqual([
      'missing --no-project',
      'missing --no-config',
      'needs --no-build, or --exclude-newer when a build is unavoidable',
      '--quiet hides what uv installs',
      'unpinned --with pillow',
      'unpinned --with numpy',
    ]);
  });

  it('accepts pinned, isolated helpers, with extras and quotes', () => {
    expect(
      helperProblems(
        "uv run --no-project --no-config --no-build --python 3.12 --with 'psd-tools[composite]==1.27.0' --with numpy==2.5.5 python -I $X @args }"
      )
    ).toEqual([]);
    expect(
      helperProblems(
        'uv run --no-project --no-config --exclude-newer P7D --python 3.12 --with tpk-ar==0.2.4 python -I "$X" "$@"; }'
      )
    ).toEqual([]);
  });

  it('flags a range instead of an exact pin', () => {
    expect(
      helperProblems(
        'uv run --no-project --no-config --no-build --with pillow>=13 python'
      )
    ).toEqual(['unpinned --with pillow>=13']);
  });
});

describe('fencedUvCommands', () => {
  it('reads only fenced code and joins bash and PowerShell continuations', () => {
    const markdown = [
      'Prose with `uv run --python 3.12` is ignored.',
      '```bash',
      'uv run --no-project \\',
      '  --no-config python a.py',
      '```',
      '   ```powershell',
      '   uv run --no-build `',
      '     python b.py',
      '   ```',
    ].join('\n');
    expect(fencedUvCommands(markdown)).toEqual([
      'uv run --no-project --no-config python a.py',
      'uv run --no-build python b.py',
    ]);
  });
});

describe('creative skill docs', () => {
  it('document helpers for the skills that run Python', () => {
    const counted = Object.fromEntries(
      markdownFiles.map(({ path, text }) => [
        path,
        fencedUvCommands(text).length,
      ])
    );
    expect(
      counted[join('transparent-background', 'SKILL.md')]
    ).toBeGreaterThanOrEqual(2);
    expect(
      counted[join('game-image-extraction', 'SKILL.md')]
    ).toBeGreaterThanOrEqual(4);
  });

  it.each(markdownFiles.map(({ path }) => path))(
    '%s isolates and pins every uv command',
    (path) => {
      const { text } = markdownFiles.find((file) => file.path === path);
      const bad = fencedUvCommands(text)
        .map((command) => ({ command, problems: helperProblems(command) }))
        .filter(({ problems }) => problems.length > 0);
      expect(bad).toEqual([]);
    }
  );

  it.each(markdownFiles.map(({ path }) => path))(
    '%s never finds a script through the current repository',
    (path) => {
      const { text } = markdownFiles.find((file) => file.path === path);
      expect(text).not.toMatch(/git rev-parse/);
    }
  );
});

describe('package.json test scripts', () => {
  const { scripts } = JSON.parse(
    readFileSync(join(repoRoot, 'package.json'), 'utf8')
  );
  const uvScripts = Object.entries(scripts).filter(([, command]) =>
    command.includes('uv run ')
  );

  it('exist', () => {
    expect(uvScripts.length).toBeGreaterThan(0);
  });

  it.each(uvScripts)(
    '%s ignores the folder project and uv config',
    (_name, command) => {
      const flags = new Set(uvOptions(command).map(([flag]) => flag));
      expect(flags.has('--no-project') && flags.has('--no-config')).toBe(true);
    }
  );
});
