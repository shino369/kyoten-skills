# kyoten-skills

Reusable [Agent Skills](https://skills.sh) originally built for the
`claude-code-personal-workspace` / `kyoten` workspace — covering pnpm dependency and security
maintenance, flat-background image cutout and recolouring, and EN ↔ JA ↔ zh-TW trilingual
translation.

## Install

Install all skills or pick only what you need with [skills.sh](https://skills.sh):

```bash
# Install all skills
npx skills add shino369/kyoten-skills

# Install specific skills
npx skills add shino369/kyoten-skills --skill transparent-background
npx skills add shino369/kyoten-skills --skill dependency-update security-update
npx skills add shino369/kyoten-skills --skill translation-expertise
```

Using `pnpm`:

```bash
pnpm dlx skills add shino369/kyoten-skills --skill transparent-background --agent claude-code codex -y
```

## Available Skills

### Maintenance & Security

| Skill                                                    | Description                                                                                                    | Runtime                         |
| -------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| [`dependency-update`](skills/dependency-update/SKILL.md) | Dependabot-style pnpm maintenance: catalog/`package.json` bumps, Node/pnpm upgrades, Dependabot PR review.     | Node 24+, `pnpm`, optional `gh` |
| [`security-update`](skills/security-update/SKILL.md)     | Patch vulnerable npm/pnpm packages via `pnpm audit`, release-age verification, parent upgrades, and overrides. | Node 24+, `pnpm`                |

### Image Processing

| Skill                                                              | Description                                                                                              | Runtime                                   |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| [`transparent-background`](skills/transparent-background/SKILL.md) | Turn flat-background images into clean transparent PNGs, fix halos/specks, and recolour separate shapes. | `uv` (Python 3.12 + Pillow, NumPy, SciPy) |

### Trilingual Translation (EN ↔ JA ↔ zh-TW)

| Skill                                                            | Description                                                                                            | Runtime |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ------- |
| [`translation-expertise`](skills/translation-expertise/SKILL.md) | House styles, 1:1 structural parity, and JA/zh-TW terminology trap guards for EN/JA/zh-TW translation. | None    |

## Development & Verification

Bundled skill scripts maintain 100% test coverage:

```bash
pnpm install
pnpm check
```

## License

[MIT](LICENSE)
