export const meta = {
  name: 'deep-review',
  description: '多視点(正しさ/エッジケース/回帰)の並列レビュー → 各指摘の敵対的検証 → 統合を行う対話用ワークフロー。実装完了後にまとめてレビューしたいときに使う。',
  phases: [
    { title: 'Review', detail: '正しさ/エッジケース/回帰の3レンズで並列レビュー', model: 'sonnet' },
    { title: 'Verify', detail: '各指摘を敵対的に検証(独立なのでparallelで同時実行)。反証できなければ確定', model: 'opus' },
    { title: 'Synthesize', detail: '確定した指摘を統合し、重複排除・優先順位付けして最終判定', model: 'opus' },
  ],
}

// deep-review: 3レンズ並列Review(sonnet) -> pipelineで逐次Verify(opus, 敵対的反証)
//   -> Synthesize(opus)。対話利用向け。headless (`claude -p`) での動作は未検証 —
//   評価構成 (opus-harness) はこの workflow に依存せず、Agent ツール経由の verifier
//   サブエージェント呼び出しのみに依存する (docs/harness-design.md の設計判断1)。
//
// args: レビュー対象の説明(例: "src/foo.py の直前の変更" "PR #123 の diff")。
// 省略時は cwd の未コミットの変更(git diff)を対象とする。

const TARGET = (typeof args === 'string' && args.trim()) || 'このリポジトリの cwd における未コミットの変更(git diff で確認できる範囲)'

const FINDINGS_SCHEMA = {
  type: 'object',
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['title', 'file', 'severity', 'detail'],
        properties: {
          title: { type: 'string' },
          file: { type: 'string', description: 'file:line 形式。不明なら空文字' },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'nit'] },
          detail: { type: 'string', description: '何が問題で、どんな実害・悪化があるか' },
        },
      },
    },
  },
}

const VERDICT_SCHEMA = {
  type: 'object',
  required: ['refuted', 'confidence', 'reasoning'],
  properties: {
    refuted: { type: 'boolean', description: 'true = 実在しない問題(誤解・仕様どおり・既に対処済み)として反証できた' },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
    reasoning: { type: 'string', description: '実際にコードを読む/実行するなどして確認した根拠' },
    fix: { type: 'string', description: 'refuted=false の場合の具体的な最小修正案' },
  },
}

const SYNTH_SCHEMA = {
  type: 'object',
  required: ['overall_verdict', 'summary', 'ranked_findings'],
  properties: {
    overall_verdict: { type: 'string', enum: ['no_blocking_issues', 'changes_recommended', 'changes_required'] },
    summary: { type: 'string' },
    ranked_findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['title', 'file', 'severity', 'lens', 'detail'],
        properties: {
          title: { type: 'string' },
          file: { type: 'string' },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'nit'] },
          lens: { type: 'string' },
          detail: { type: 'string' },
          fix: { type: 'string' },
        },
      },
    },
  },
}

const LENS_CONTEXT =
  'レビュー対象: ' + TARGET + '\n' +
  'まず対象を実際に把握すること(git diff / git status / 該当ファイルの Read など)。推測で書かない。\n' +
  '指摘は最大8件・重要なものから。findings が無ければ空配列を返してよい(無理に絞り出さない)。\n' +
  '構造化出力のみで答えること。'

const LENSES = [
  {
    key: 'correctness',
    label: '正しさ',
    prompt:
      'あなたはレビュー担当(レンズ=正しさ)。' + LENS_CONTEXT + '\n\n' +
      'この観点: ロジックの正しさ。誤った条件分岐、型やnull/undefinedの取り違え、' +
      'オフバイワン、計算・変換の誤り、意図と実装のずれを探す。仕様やコメントと実装が食い違う箇所は、' +
      'どちらが正か実行・実挙動を確認してから指摘する。',
  },
  {
    key: 'edge_case',
    label: 'エッジケース',
    prompt:
      'あなたはレビュー担当(レンズ=エッジケース)。' + LENS_CONTEXT + '\n\n' +
      'この観点: 空入力・境界値(0件/1件/ちょうど上限)・並行実行(競合状態・check-then-act)・' +
      'エンコーディング(マルチバイト・改行・引用符・NULバイト)を狙って調べる。' +
      '通常系だけ動いて異常系・境界系で壊れる実装を優先的に探す。',
  },
  {
    key: 'regression',
    label: '既存機能の回帰',
    prompt:
      'あなたはレビュー担当(レンズ=既存機能の回帰)。' + LENS_CONTEXT + '\n\n' +
      'この観点: この変更が既存の挙動・既存のテスト・既存の呼び出し元を壊していないか。' +
      '既存テストを実行し、変更前は通っていたはずのものが変更後も通るかを確認する。' +
      '変更範囲外に副作用が及んでいないかも見る。',
  },
]

// ─── Phase 1: Review (3レンズ並列, sonnet) ───
phase('Review')
log('3レンズ並列レビュー開始: ' + LENSES.map((l) => l.label).join(' / '))

const reviews = await parallel(
  LENSES.map((lens) => () =>
    agent(lens.prompt, { label: 'review:' + lens.key, phase: 'Review', schema: FINDINGS_SCHEMA, model: 'sonnet' })
      .then((r) => {
        log(lens.label + ': ' + (r ? (r.findings || []).length : 0) + ' 件')
        return r
      })
  )
)

const allFindings = reviews.flatMap((r, i) =>
  r ? (r.findings || []).map((f) => ({ ...f, lens: LENSES[i].key, lensLabel: LENSES[i].label })) : []
)

if (allFindings.length === 0) {
  return {
    overall_verdict: 'no_blocking_issues',
    summary: '3レンズいずれも指摘なし。ただし各レンズが実際に対象を調べた上での結論かは verifyLog が空である点に留意。',
    ranked_findings: [],
    confirmedCount: 0,
    refutedCount: 0,
  }
}

log('レビュー完了: 指摘 計' + allFindings.length + '件 → 敵対的検証へ')

// ─── Phase 2: Verify (敵対的反証, opus, parallelで同時実行) ───
// 各指摘の検証は互いに独立(前の指摘の検証結果に依存しない)なので、pipeline で
// 1件ずつ逐次処理する必要はない。parallel で同時実行しレイテンシを削減する。
phase('Verify')
const verified = await parallel(
  allFindings.map((finding) => () =>
    agent(
      'あなたは反証担当。以下の指摘を検証する対象は: ' + TARGET + '\n\n' +
        '## 検証対象の指摘 (レンズ=' + finding.lensLabel + ')\n' +
        JSON.stringify({ title: finding.title, file: finding.file, severity: finding.severity, detail: finding.detail }, null, 2) +
        '\n\n' +
        'あなたの仕事はこの指摘を**反証する**こと。実際にコードを読み、可能ならテストを実行・再現して確認せよ。' +
        '誤解・すでに対処済み・仕様どおりの挙動であれば refuted=true。' +
        '実在する問題だと判断する場合のみ refuted=false とし、具体的な最小修正案を書け。' +
        '確信が持てない場合は confidence=low とした上で refuted=false 側に倒す(見逃しより誤検出の方が安全)。' +
        '構造化出力のみ。',
      {
        label: 'verify:' + finding.lens + ':' + (finding.file || finding.title).slice(0, 40),
        phase: 'Verify',
        schema: VERDICT_SCHEMA,
        model: 'opus',
      }
    ).then((v) => ({ ...finding, verdict: v }))
  )
)

const confirmed = verified.filter((f) => f.verdict && !f.verdict.refuted)
const refuted = verified.filter((f) => f.verdict && f.verdict.refuted)
const unverifiable = verified.filter((f) => !f.verdict)

log('検証完了: confirmed=' + confirmed.length + ' refuted=' + refuted.length + ' unverifiable=' + unverifiable.length)

if (confirmed.length === 0) {
  return {
    overall_verdict: 'no_blocking_issues',
    summary:
      allFindings.length + '件の指摘はすべて敵対的検証で反証された(または検証不能だった)。詳細は refuted/unverifiable を参照。',
    ranked_findings: [],
    confirmedCount: 0,
    refutedCount: refuted.length,
    refuted: refuted.map((f) => ({ title: f.title, file: f.file, lens: f.lens, reasoning: f.verdict.reasoning })),
    unverifiable: unverifiable.map((f) => ({ title: f.title, file: f.file, lens: f.lens })),
  }
}

// ─── Phase 3: Synthesize (統合, opus) ───
phase('Synthesize')
const confirmedBlock = confirmed
  .map(
    (f, i) =>
      '### [' + i + '] (' + f.lensLabel + ' / ' + f.severity + ') ' + f.title + '\n' +
      'file: ' + f.file + '\n詳細: ' + f.detail + '\n検証根拠: ' + f.verdict.reasoning + '\n修正案: ' + (f.verdict.fix || '(なし)') + '\n'
  )
  .join('\n')

const synthesis = await agent(
  '以下は3レンズ(正しさ/エッジケース/既存機能の回帰)によるレビューのうち、敵対的検証で反証されず確定した指摘である。\n\n' +
    confirmedBlock +
    '\n\n## 任務\n' +
    '1. 同じ問題を指す重複指摘があれば統合する。\n' +
    '2. severity と実害の大きさで優先順位を付ける。\n' +
    '3. overall_verdict を決める: critical/high が1件でもあれば changes_required、medium以下のみなら changes_recommended、' +
    '該当なしなら no_blocking_issues。\n' +
    '4. 3-5文の summary を書く。\n\n構造化出力のみ。',
  { label: 'synthesize', phase: 'Synthesize', schema: SYNTH_SCHEMA, model: 'opus' }
)

if (!synthesis) {
  // 統合ステップが失敗しても、確定済みの指摘は生のまま返す(握りつぶさない)。
  return {
    overall_verdict: confirmed.some((f) => f.severity === 'critical' || f.severity === 'high')
      ? 'changes_required'
      : 'changes_recommended',
    summary: '統合(Synthesize)ステップが結果を返さなかったため、確定指摘を未統合のまま返す。',
    ranked_findings: confirmed.map((f) => ({
      title: f.title,
      file: f.file,
      severity: f.severity,
      lens: f.lens,
      detail: f.detail,
      fix: f.verdict.fix,
    })),
    confirmedCount: confirmed.length,
    refutedCount: refuted.length,
  }
}

return {
  ...synthesis,
  confirmedCount: confirmed.length,
  refutedCount: refuted.length,
  refuted: refuted.map((f) => ({ title: f.title, file: f.file, lens: f.lens, reasoning: f.verdict.reasoning })),
}
