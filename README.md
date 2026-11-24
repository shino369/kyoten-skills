# kyoten-skills

Reusable [Agent Skills](https://skills.sh) originally built for the
`claude-code-personal-workspace` / `kyoten` workspace — organized by category under `skills/`:

- `skills/dependencies/` — pnpm dependency and security maintenance (included in `kyoten` by default)
- `skills/translation/` — EN ↔ JA ↔ zh-TW trilingual translation (included in `kyoten` by default)
- `skills/creative/` — image cutout/recolouring and Unity/KiriKiri game image extraction (opt-in)

## Install

Install interactively or pick specific skills with [skills.sh](https://skills.sh):

```bash
# Interactive picker (choose which skills to install)
npx skills add shino369/kyoten-skills

# Install the kyoten default set (dependencies + translation)
npx skills add shino369/kyoten-skills --skill dependency-update security-update translation-expertise

# Install opt-in creative skills on demand
npx skills add shino369/kyoten-skills --skill transparent-background
npx skills add shino369/kyoten-skills --skill game-image-extraction
```

Using `pnpm` (e.g. inside `kyoten`):

```bash
pnpm dlx skills add shino369/kyoten-skills --skill transparent-background --agent claude-code codex -y
pnpm dlx skills add shino369/kyoten-skills --skill game-image-extraction --agent claude-code codex -y
```

## Available Skills

### Dependencies (`skills/dependencies/`) — Default in `kyoten`

| Skill                                                                 | Description                                                                                                    | Runtime                         |
| --------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| [`dependency-update`](skills/dependencies/dependency-update/SKILL.md) | Dependabot-style pnpm maintenance: catalog/`package.json` bumps, Node/pnpm upgrades, Dependabot PR review.     | Node 24+, `pnpm`, optional `gh` |
| [`security-update`](skills/dependencies/security-update/SKILL.md)     | Patch vulnerable npm/pnpm packages via `pnpm audit`, release-age verification, parent upgrades, and overrides. | Node 24+, `pnpm`                |

### Translation (`skills/translation/`) — Default in `kyoten`

| Skill                                                                        | Description                                                                                            | Runtime |
| ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ------- |
| [`translation-expertise`](skills/translation/translation-expertise/SKILL.md) | House styles, 1:1 structural parity, and JA/zh-TW terminology trap guards for EN/JA/zh-TW translation. | None    |

### Creative (`skills/creative/`) — Opt-in

Not installed by default during `kyoten` initialization; install individually when needed.

| Skill                                                                       | Description                                                                                                         | Runtime                                         |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| [`transparent-background`](skills/creative/transparent-background/SKILL.md) | Turn flat-background images into clean transparent PNGs, fix halos/specks, and recolour separate shapes.            | `uv` (Python 3.12 + Pillow, NumPy, SciPy)       |
| [`game-image-extraction`](skills/creative/game-image-extraction/SKILL.md)   | Extract, convert, and verify images from Unity (`*_Data`/Addressables) and KiriKiri2/Z (`.xp3`/TLG/PSB-PIMG) games. | `uv` (Python 3.12 + Pillow, optional `UnityPy`) |

## Development & Verification

Bundled skill scripts maintain 100% test coverage:

```bash
pnpm install
pnpm check
```

## License

[MIT](LICENSE)
