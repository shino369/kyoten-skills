---
name: translation-expertise
description: Practical house style, structural parity, and terminology traps for English, Japanese (日本語), and Traditional Chinese (繁體中文 / Taiwan) translation. Use when translating technical docs, specs, PR reviews, articles, or maintaining bilingual/trilingual document pairs (`-en` / `-ja` / `-zh`).
---

# Translation Expertise (EN ↔ JA ↔ zh-TW)

Concise, opinionated rules for natural, consistent trilingual translation. Prioritize meaning,
structural parity, and regional accuracy over literal wording.

## Never Translate

- Code blocks, inline code, identifiers, file paths, commands, CLI flags, URLs, frontmatter keys,
  and mathematical formulas.
- Code comments — keep code blocks verbatim unless the user explicitly asks to localize comments.
- Markup and macros (HTML, JSX, Confluence storage format) — translate text nodes only.
- Review severity prefixes and verdicts (`⚠️ Critical`, `💡 Suggestion`, `❓ Question`, `💅 Nit`,
  `🎉 Praise`, `APPROVE` / `REQUEST CHANGES` / `BLOCK`).

## Structure & Document Pairs

- **Preserve structure 1:1:** keep identical heading levels and order, lists, tables, links, and
  anchors so `-en` / `-ja` / `-zh` pairs diff section by section.
- **Incremental updates:** when one side of a document pair (`*-en.md` / `*-ja.md` / `*-zh.md`)
  changes, update only the matching sections of the other side(s). Never retranslate untouched
  sections.
- **Clean output:** write the translation in place or to the requested file. List uncertain term
  choices briefly at the end, never inline.

## Japanese (日本語) House Style

- **Register:** default to `です/ます` for docs, guidelines, specs, and reports (not `である調`,
  unless matching an existing `である調` document or academic paper).
- **Directness:** lead with conclusions in technical/business prose; avoid unnecessary hedging or
  overlong sentences.
- **English vs katakana:** keep product, framework, and CLI names in English (`React`, `Next.js`,
  `pnpm`, `Docker`, `PR`). Use established katakana/Japanese for general concepts (`コンポーネント`,
  `フック`, `状態管理`, `デプロイ`, `レビュー`).
- **Bilingual headings (when used by the project):** `日本語 (English)`, e.g.
  `コア原則 (Core Principles)`.
- **Punctuation:** full-width (`。、（）「」`) in Japanese prose; half-width inside code,
  identifiers, and parenthetical English terms.

## Traditional Chinese (繁體中文 / zh-TW) House Style

- **Region:** use **Taiwan (台灣)** terminology by default. Switch to Hong Kong or Mainland terms
  only when explicitly requested.
- **Brand/tech names:** keep framework, library, product, and CLI names in English.
- **Punctuation:** full-width (`，。、；：「」『』（）《》`) in prose — never mix half-width `,`
  or `;` into Chinese sentences. Keep half-width inside code and identifiers.
- **Tone:** clear, direct modern prose. Use 成語 sparingly and only when completely natural — never
  pad technical or business writing with stacked idioms or literary clichés.

## High-Signal Terminology Traps

### JA vs zh-TW False Friends & Context Splits

| English               | Japanese            | zh-TW (Taiwan)    | Watch out                                      |
| --------------------- | ------------------- | ----------------- | ---------------------------------------------- |
| Row / Column          | 行 / 列             | 列 / 欄           | `列` means column in JA, row in zh-TW          |
| Function (code / UX)  | 関数 / 機能         | 函式 / 功能       | Never mix programming vs feature sense         |
| Type (code / general) | 型 / タイプ         | 型別 / 類型       | Use `型` / `型別` for data types               |
| Object (OOP / target) | オブジェクト / 対象 | 物件 / 對象       | `物件` = code object; `對象` = target/audience |
| Instance              | インスタンス        | 執行個體          | `實例` is fine for general/example usage       |
| Thread / Process (OS) | スレッド / プロセス | 執行緒 / 處理程序 | Avoid PRC `線程` / `進程`                      |
| Cache                 | キャッシュ          | 快取              | Avoid PRC `緩存`                               |

### Taiwan (zh-TW) vs PRC Guard List

LLMs frequently slip Mainland terms into Traditional Chinese output. Prefer the Taiwan form:

| Concept            | Use (zh-TW)     | Avoid (PRC)   | Concept       | Use (zh-TW) | Avoid (PRC) |
| ------------------ | --------------- | ------------- | ------------- | ----------- | ----------- |
| Software           | 軟體            | 軟件          | Pipeline      | 管線        | 流水線      |
| Network            | 網路            | 網絡          | Rollback      | 復原 / 回復 | 回滾        |
| Information / Data | 資訊 / 資料     | 信息 / 數據   | Build         | 建置        | 構建        |
| Memory             | 記憶體          | 內存          | Registry      | 登錄檔/庫   | 註冊表      |
| File               | 檔案            | 文件          | Array         | 陣列        | 數組        |
| Project            | 專案            | 項目          | List          | 串列 / 清單 | 列表        |
| Default            | 預設            | 默認          | Pointer       | 指標        | 指針        |
| Call / Invoke      | 呼叫            | 調用          | Class         | 類別        | 類          |
| Async              | 非同步          | 異步          | Variable      | 變數        | 變量        |
| Support            | 支援            | 支持          | String        | 字串        | 字符串      |
| Quality            | 品質            | 質量          | Interface     | 介面        | 接口        |
| Optimize           | 最佳化          | 優化          | Signal        | 訊號        | 信號        |
| Video              | 影片            | 視頻          | Clustering    | 分群        | 聚類        |
| Server / Client    | 伺服器 / 用戶端 | 服務器/客戶端 | Activation fn | 活化函數    | 激活函數    |
| Remote work        | 遠距工作        | 遠程辦公      | Constraint    | 條件約束    | 約束        |

For product-specific terms not listed above, match the vendor's official localized docs (Microsoft,
Apple, Google, MDN).

## Workflow

- **Routine / short text:** single pass — read full context, translate following the rules above,
  self-check terminology and formatting parity, then deliver.
- **High-stakes / long documents:** use a 3-pass flow (draft + term list → proofread for accuracy,
  regional traps, and naturalness → refine).
