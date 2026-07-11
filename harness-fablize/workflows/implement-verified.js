export const meta = {
  name: 'implement-verified',
  description: 'ミニ仕様生成 → 実装 → 反証検証 → 修正ループを一気通貫で回す対話用ワークフロー。曖昧な依頼を渡すと、目的/範囲/非目標/完了条件を自分で仕様化してから実装し、verifier 相当の反証レビューを通してから結果を返す。',
  phases: [
    { title: 'Spec', detail: 'ミニ仕様(目的/範囲/非目標/検証可能な完了条件)を生成し、曖昧さを解決するかユーザーへの確認事項を切り出す', model: 'opus' },
    { title: 'Implement', detail: '仕様が明確な場合に実装し、テストを実行してから結果を返す', model: 'sonnet' },
    { title: 'Verify', detail: '反証指向レビュー(テスト実行必須)。承認されるまで1〜2ラウンド', model: 'opus' },
    { title: 'Fix', detail: '要修正判定のときのみ、指摘に対応して再実装。最大2周', model: 'sonnet' },
  ],
}

// implement-verified: Spec(opus) -> Implement(sonnet, agentType:'implementer')
//   -> Verify(opus, agentType:'verifier') -> [不合格なら] Fix(sonnet) -> Verify(opus) を
//   最大2周。この workflow は harness/agents/implementer.md と harness/agents/verifier.md
//   が ~/.claude/agents/ または .claude/agents/ にインストールされていることを前提にする
//   (harness-design.md の配布経路)。未インストールの環境では agentType 解決が失敗しうる。
//
// 対話利用向け。headless (`claude -p`) での動作は未検証 — 評価構成 (opus-harness) は
// この workflow に依存せず、Agent ツール経由の verifier サブエージェント呼び出しのみに
// 依存する (docs/harness-design.md の設計判断1)。

const MAX_FIX_ROUNDS = 2

const SPEC_SCHEMA = {
  type: 'object',
  required: ['purpose', 'scope', 'out_of_scope', 'done_criteria', 'ambiguity_level'],
  properties: {
    purpose: { type: 'string', description: '目的(1-2文)' },
    scope: { type: 'string', description: '着手する範囲' },
    out_of_scope: { type: 'string', description: '非目標。ついで修正・提案に留めるべき事項' },
    done_criteria: {
      type: 'array',
      items: { type: 'string' },
      description: '検証可能な形(実行・テストで確認できる形)で書いた完了条件',
    },
    defaults_chosen: {
      type: 'array',
      items: { type: 'string' },
      description: '未確定だが合理的なデフォルトで解釈した点。「〜と解釈した」の形式',
    },
    unverified_assumptions: {
      type: 'array',
      items: { type: 'string' },
      description: '[未検証]の仮定。実装/検証フェーズで検証されるべきもの',
    },
    ambiguity_level: {
      type: 'string',
      enum: ['clear', 'proceed_with_defaults', 'needs_user_input'],
      description: 'clear=仕様自明, proceed_with_defaults=デフォルト採用で進める, needs_user_input=ユーザーにしか決められない事項がある',
    },
    open_questions: {
      type: 'array',
      items: { type: 'string' },
      description: 'ambiguity_level=needs_user_input のときのみ。ユーザーにしか決められない事項(外部への影響・好み・費用)だけを書く',
    },
  },
}

const IMPLEMENT_SCHEMA = {
  type: 'object',
  required: ['summary', 'files_changed', 'tests_run', 'tests_passed', 'unresolved'],
  properties: {
    summary: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    tests_run: { type: 'array', items: { type: 'string' }, description: '実際に実行したコマンド' },
    tests_passed: { type: 'boolean' },
    test_output_excerpt: { type: 'string', description: '実行結果の要約(失敗があれば失敗内容)' },
    unresolved: { type: 'array', items: { type: 'string' }, description: '未解決・未検証のまま残った点' },
  },
}

const VERIFY_SCHEMA = {
  type: 'object',
  required: ['verdict', 'findings', 'tests_executed', 'no_findings_evidence'],
  properties: {
    verdict: { type: 'string', enum: ['approved', 'needs_fix'] },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['severity', 'problem', 'evidence', 'fix_suggestion'],
        properties: {
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          problem: { type: 'string' },
          evidence: { type: 'string', description: '再現手順・実行結果・file:line。推測のみなら「[推測] 」を先頭に付ける' },
          fix_suggestion: { type: 'string' },
        },
      },
    },
    tests_executed: { type: 'array', items: { type: 'string' }, description: '検証のために実際に実行したコマンド' },
    no_findings_evidence: {
      type: 'string',
      description:
        '必須(常時)。findings が空の場合は、反証を試みて何を実行し何が通ったかの根拠を書く。' +
        'findings が1件以上ある場合は空文字でよい。',
    },
  },
}

const TASK = (typeof args === 'string' && args.trim()) || ''
if (!TASK) {
  return {
    error: "タスク記述が渡されていません。Workflow({name: 'implement-verified', args: '<依頼内容>'}) の形で args にユーザーの依頼を渡してください。",
  }
}

// ─── Phase 1: Spec ───
phase('Spec')
const spec = await agent(
  '以下のユーザー依頼から、ミニ仕様を作る。\n\n' +
  '## ユーザー依頼\n' + TASK + '\n\n' +
  '## 仕様化の指針\n' +
  '- 着手前に目的/範囲/非目標/完了条件(検証可能な形)を書き出す。\n' +
  '- 合理的なデフォルトは自分で選び、defaults_chosen に「〜と解釈した」の形で明示する。\n' +
  '- ユーザーにしか決められない事項(外部への影響・好み・費用)だけを open_questions に残す。それ以外は質問せず、defaults_chosen で進める側(ambiguity_level=proceed_with_defaults)を優先する。\n' +
  '- 未検証で進める前提は unverified_assumptions に列挙する。\n' +
  '- 依頼が実装可能な範囲を明らかに超える、または安全性/破壊的操作に関わり独断で進めるべきでない場合のみ ambiguity_level=needs_user_input とし、open_questions を書く。\n\n' +
  '構造化出力のみで答えること。',
  { label: 'spec', phase: 'Spec', schema: SPEC_SCHEMA, model: 'opus' }
)

if (!spec) {
  return { error: 'ミニ仕様の生成に失敗しました(spec agent が結果を返しませんでした)。' }
}

log('spec: ambiguity_level=' + spec.ambiguity_level + ' done_criteria=' + (spec.done_criteria || []).length)

if (spec.ambiguity_level === 'needs_user_input' && (spec.open_questions || []).length > 0) {
  return {
    status: 'needs_user_input',
    spec,
    message: 'ユーザーにしか決められない事項があるため、実装に進まず停止しました。open_questions をユーザーに提示してください。',
  }
}

const specBlock =
  '## ミニ仕様\n' +
  '目的: ' + spec.purpose + '\n' +
  '範囲: ' + spec.scope + '\n' +
  '非目標: ' + spec.out_of_scope + '\n' +
  '完了条件:\n' + (spec.done_criteria || []).map((c) => '- ' + c).join('\n') + '\n' +
  (spec.defaults_chosen && spec.defaults_chosen.length
    ? '採用したデフォルト:\n' + spec.defaults_chosen.map((d) => '- ' + d).join('\n') + '\n'
    : '') +
  (spec.unverified_assumptions && spec.unverified_assumptions.length
    ? '未検証の前提:\n' + spec.unverified_assumptions.map((u) => '- [未検証] ' + u).join('\n') + '\n'
    : '')

// ─── Phase 2: Implement ───
phase('Implement')
let implementation = await agent(
  specBlock + '\n上記のミニ仕様どおりに実装せよ。範囲外の変更はしない。完了条件それぞれを実際に検証してから結果を返すこと。構造化出力のみ。',
  { label: 'implement', phase: 'Implement', schema: IMPLEMENT_SCHEMA, model: 'sonnet', agentType: 'implementer' }
)

if (!implementation) {
  return { status: 'error', spec, error: '実装フェーズが結果を返しませんでした。' }
}

log('implement: files=' + (implementation.files_changed || []).length + ' tests_passed=' + implementation.tests_passed)

// ─── Phase 3 & 4: Verify -> [needs_fix なら] Fix -> Verify (最大 MAX_FIX_ROUNDS 周) ───
// 「1〜2ラウンド」は実運用上の目安: 初回で承認されれば1ラウンドで終わり、
// 1回の修正で承認されれば2ラウンドで終わる。MAX_FIX_ROUNDS はそれとは独立に、
// 修正サイクル自体の上限(=最悪ケースの総verifyラウンド数の上界)を定める安全弁。
phase('Verify')
const verifyLog = []
let fixRound = 0
let verifyResult = null

while (true) {
  verifyResult = await agent(
    specBlock +
      '\n## 実装結果\n' + JSON.stringify(implementation, null, 2) +
      '\n\n上記の実装を反証指向でレビューせよ。' +
      '仕様との齟齬・エッジケース(空/境界/並行/エンコーディング)・既存機能の破壊・' +
      'コメントドキュメントとの矛盾・エラーハンドリングを、実際にテストを実行して確認すること。' +
      '推論だけの指摘は evidence の先頭に「[推測] 」を付けること。構造化出力のみ。',
    {
      label: 'verify:' + (fixRound + 1),
      phase: 'Verify',
      schema: VERIFY_SCHEMA,
      model: 'opus',
      agentType: 'verifier',
    }
  )
  verifyLog.push(verifyResult)

  if (!verifyResult) {
    log('verify round ' + (fixRound + 1) + ': no result (agent error) — treating as needs_fix')
    break
  }
  log(
    'verify round ' + (fixRound + 1) + ': verdict=' + verifyResult.verdict +
    ' findings=' + (verifyResult.findings || []).length
  )
  if (verifyResult.verdict === 'approved') break
  if (fixRound >= MAX_FIX_ROUNDS) break

  // ─── Fix ───
  phase('Fix')
  fixRound++
  const findingsBlock = (verifyResult.findings || [])
    .map((f) => '- [' + f.severity + '] ' + f.problem + ' / 根拠: ' + f.evidence + ' / 提案: ' + f.fix_suggestion)
    .join('\n')
  implementation = await agent(
    specBlock +
      '\n## 直前の実装結果\n' + JSON.stringify(implementation, null, 2) +
      '\n\n## verifier からの指摘 (修正ラウンド ' + fixRound + '/' + MAX_FIX_ROUNDS + ')\n' + findingsBlock +
      '\n\n上記の指摘に対応せよ。範囲外の変更はしない。対応後、完了条件と指摘の両方を実際に検証してから結果を返すこと。構造化出力のみ。',
    { label: 'fix:' + fixRound, phase: 'Fix', schema: IMPLEMENT_SCHEMA, model: 'sonnet', agentType: 'implementer' }
  )
  if (!implementation) {
    return {
      status: 'error',
      spec,
      verifyLog,
      error: '修正ラウンド ' + fixRound + ' が結果を返しませんでした。',
    }
  }
  phase('Verify')
}

const finalVerdict = verifyResult ? verifyResult.verdict : 'needs_fix'
log('done: verdict=' + finalVerdict + ' fixRounds=' + fixRound)

return {
  status: finalVerdict === 'approved' ? 'approved' : 'needs_fix_unresolved',
  spec,
  implementation,
  fixRounds: fixRound,
  verifyLog,
  finalVerdict,
}
