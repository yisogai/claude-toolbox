export const meta = {
  name: 'codex-implement-review-loop',
  description:
    'Codex に実装させ、Claude(opus/xhigh) と Codex の 2 レーンで反証レビューし、Must 指摘だけを実装スレッドへ --resume <thread_id> で戻して修正ラウンドを回す。裁定（Must/Should/Nice）はメインの Fable が行う前提で、この Workflow は両レーンの findings をマージして返すところまで。',
  phases: [
    { title: 'Implement', detail: 'worktree 内で codex_run.py --mode task --write を実行し、result 要約を返す', model: 'opus (effort: medium)' },
    { title: 'Review', detail: 'Claude 反証レビュー(編集禁止) と Codex 構造化レビューを並列で回す', model: 'opus (effort: xhigh) + codex' },
    { title: 'Fix', detail: 'blocking+major のみ --resume <実装スレッドの thread_id> で Codex に戻す。減らなければ停止', model: 'opus (effort: medium)' },
  ],
}

// ---------------------------------------------------------------------------
// これは **コピーして使うテンプレート**。案件ごとに CODEX_BRIDGE / WORKTREE /
// TIMEOUT_SEC を書き換えて `~/.claude/workflows/` などへ置く。
//
// 前提:
//   - Codex CLI が導入済みで `codex login`（ChatGPT プラン）が通っていること。
//     未導入の環境では codex_run.py が exit 4（not_found）を返すので、そこで止まる。
//   - 対象リポジトリのルートに codex-bridge/templates/AGENTS.md.tmpl を配置済みであること。
//   - 各 agent() には model と opts.effort を必ず明示する（model-policy hook が
//     Workflow script 内の model 値を検査するため、省略すると弾かれる）。
//   - codex_run.py は長時間実行になる。Bash 実行は run_in_background 相当で回し、
//     --timeout-sec を必ず明示すること（既定 3600 は長すぎる場合がある）。
//   - 修正ラウンドは **--resume-last を使わない**。codex の resume --last は「同じ cwd の
//     最終更新スレッド」を選ぶため、同じ worktree でレビュー実行を挟むとレビュー側の
//     スレッドを再開してしまう。ラウンド 1 の job.json の thread_id を保持し、
//     以降は --resume <thread_id> で明示的に実装スレッドへ戻す。
// ---------------------------------------------------------------------------

const CODEX_BRIDGE = '/Users/<YOU>/Documents/personal/tools/claude-toolbox/codex-bridge'
const CWD = typeof process !== 'undefined' && process.cwd ? process.cwd() : '.'
const WORKTREE = args && args.worktree ? args.worktree : CWD
const TIMEOUT_SEC = 1800
const IDLE_TIMEOUT_SEC = 300
const MAX_ROUNDS = 3   // 予算ガード。停止条件の主役は「blocking+major が減らないこと」

const jobDir = (round, lane) => `${WORKTREE}/.codex-jobs/r${round}-${lane}`

const runCodexCmd = (round, lane, extra) =>
  `python3 ${CODEX_BRIDGE}/scripts/codex_run.py --job-dir ${jobDir(round, lane)} ` +
  `--cd ${WORKTREE} --timeout-sec ${TIMEOUT_SEC} --idle-timeout-sec ${IDLE_TIMEOUT_SEC} ${extra}`

// 実装レーンのコマンド組み立て。**純関数**にしてあるのは、テストが node で実際に評価して
// 「修正ラウンドが --resume <実装スレッド> になっているか」を検証するため（文字列 grep だと
// 行の切れ目で見逃す）。
// 注意: `export` にしないこと。Workflow ランタイムは `export const meta` 以外の export を
// SyntaxError（Unexpected keyword 'export'）で拒否する（実機 2026-08-24 確認）。
const buildCodexCmd = (round, implThreadId) =>
  runCodexCmd(round, 'impl',
    `--mode task --write --model gpt-5.6-terra --effort high --prompt-file /tmp/codex-prompt.md` +
    (round === 1 ? '' : ` --resume ${implThreadId}`))

const TASK = (typeof args === 'string' && args.trim()) || (args && args.task) || ''
if (!TASK) {
  return { error: 'Workflow({name: "codex-implement-review-loop", args: "<ミニ仕様>"}) の形で依頼を渡してください。' }
}

const FINDINGS_SCHEMA = {
  type: 'object',
  required: ['verdict', 'findings', 'summary'],
  properties: {
    verdict: { type: 'string', enum: ['approve', 'needs-attention'] },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['severity', 'file', 'title', 'description', 'confidence'],
        properties: {
          severity: { type: 'string', enum: ['blocking', 'major', 'minor', 'nit'] },
          file: { type: 'string' },
          line_start: { type: 'integer' },
          line_end: { type: 'integer' },
          category: { type: 'string' },
          title: { type: 'string' },
          description: { type: 'string' },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
        },
      },
    },
    summary: { type: 'string' },
    next_steps: { type: 'array', items: { type: 'string' } },
  },
}

const severityCount = (findings) =>
  (findings || []).filter((f) => f.severity === 'blocking' || f.severity === 'major').length

const IMPLEMENT_SCHEMA = {
  type: 'object',
  required: ['status', 'thread_id', 'summary'],
  properties: {
    status: { type: 'string' },          // job.json の status（completed / failed / …）
    thread_id: { type: ['string', 'null'] },  // codex_job.py result が出す thread_id
    summary: { type: 'string' },         // codex_job.py result の要約（そのまま）
    touched_files: { type: 'array', items: { type: 'string' } },
  },
}

let round = 0
let prevCount = null
let implThreadId = null   // ラウンド 1 の実装スレッド。修正ラウンドはここへ戻す
const history = []

while (round < MAX_ROUNDS) {
  round += 1

  // ─── Implement / Fix（Codex が書く。Claude 側ドライバは Bash 実行と結果要約だけ） ───
  // L-1: ラウンド 2 以降は「修正」なので phase / label も Fix にする
  const isFix = round > 1
  phase(isFix ? 'Fix' : 'Implement')
  if (round > 1 && !implThreadId) {
    return {
      status: 'no_thread_id',
      rounds: round - 1,
      history,
      message:
        'ラウンド 1 の job.json に thread_id が無く、実装スレッドを特定できない。' +
        '--resume-last は同じ cwd の最終更新スレッド（＝レビュー側になりうる）を掴むため使わない。人へ戻す。',
    }
  }

  const implementPrompt =
    (round === 1
      ? '以下のミニ仕様を Codex に実装させる。\n\n' + TASK
      : `以下の Must 指摘のみを Codex に修正させる（--resume ${implThreadId} で**実装スレッド**に戻す。` +
        '--resume-last は使わない: 直前にレビューを同じ cwd で走らせているため、レビュー側の' +
        'スレッドが選ばれる）。\n\n' +
        JSON.stringify(history[history.length - 1].must, null, 2)) +
    '\n\n## 手順（この通りに実行し、結果だけを返す。自分でコードを書かない）\n' +
    '1. render_prompt.py でプロンプトを作る:\n' +
    `   python3 ${CODEX_BRIDGE}/scripts/render_prompt.py implement --set OBJECTIVE=... --set SCOPE=... ` +
    '--set NON_GOALS=... --set ACCEPTANCE=... --set FORBIDDEN=... --set CONTEXT=... --out /tmp/codex-prompt.md\n' +
    '2. Codex を実行する（長時間。run_in_background 相当で回す）:\n' +
    '   ' + buildCodexCmd(round, implThreadId) + '\n' +
    `3. 要約を読む: python3 ${CODEX_BRIDGE}/scripts/codex_job.py result ${jobDir(round, 'impl')}\n` +
    '   `thread_id=…（再開: --resume …）` の行に出る値を thread_id として返すこと\n' +
    `   （出ていなければ python3 ${CODEX_BRIDGE}/scripts/codex_job.py result ${jobDir(round, 'impl')} --json の thread_id を読む）。\n` +
    '4. exit 4（codex 不在・認証エラー）なら即座に停止して報告する。\n'

  const implementation = await agent(implementPrompt, {
    label: isFix ? `fix-r${round}` : `implement-r${round}`,
    phase: isFix ? 'Fix' : 'Implement', schema: IMPLEMENT_SCHEMA,
    model: 'opus', effort: 'medium',
  })

  if (round === 1) {
    implThreadId = (implementation && implementation.thread_id) || null
    if (!implThreadId) {
      log('警告: ラウンド 1 の thread_id が取れなかった。修正ラウンドには進めない')
    }
  }

  // ─── Review 2 レーン（並列） ───
  phase('Review')
  const [claudeReview, codexReview] = await Promise.all([
    agent(
      '以下の実装結果に対し、反証指向でレビューせよ。**ファイルを編集しない**（読取・テスト実行のみ）。\n' +
      '「どこかが壊れている」という仮説から出発し、実際にテストを走らせて反証を試みること。\n' +
      '重要度で絞らず全件報告する。\n\n## 実装結果\n' + JSON.stringify(implementation, null, 2),
      { label: `review-claude-r${round}`, phase: 'Review', schema: FINDINGS_SCHEMA,
        model: 'opus', effort: 'xhigh', agentType: 'verifier' }
    ),
    agent(
      'Codex に構造化レビューをさせる。自分ではレビューせず、以下を実行して JSON を返すだけ。\n' +
      '1. 差分を取る: git -C ' + WORKTREE + ' diff > /tmp/codex-review.diff\n' +
      `2. python3 ${CODEX_BRIDGE}/scripts/render_prompt.py review --set-file DIFF=/tmp/codex-review.diff ` +
      '--set FOCUS="正しさ・エッジケース・回帰" --set CONTEXT="（必要なら補足）" --out /tmp/codex-review-prompt.md\n' +
      '3. ' + runCodexCmd(round, 'review',
        '--mode review --model gpt-5.6-terra --effort high --prompt-file /tmp/codex-review-prompt.md ' +
        `--schema ${CODEX_BRIDGE}/templates/prompts/review.schema.json`) + '\n' +
      `4. python3 ${CODEX_BRIDGE}/scripts/codex_job.py result ${jobDir(round, 'review')} --json を読み、\n` +
      '   structured_output（verdict / findings / summary）をそのまま構造化出力として返す。\n' +
      '   structured_output が null なら verdict="needs-attention"、findings=[] とし、summary に理由を書く。\n' +
      '注意: exec review サブコマンドは --output-schema を無視する既知バグがあるため、\n' +
      '--review-scope は使わず、通常の exec にレビュー用プロンプトを渡す方式を使うこと。',
      { label: `review-codex-r${round}`, phase: 'Review', schema: FINDINGS_SCHEMA,
        model: 'opus', effort: 'medium' }
    ),
  ])

  const merged = []
    .concat((claudeReview && claudeReview.findings) || [])
    .concat((codexReview && codexReview.findings) || [])
  const must = merged.filter((f) => f.severity === 'blocking' || f.severity === 'major')
  const count = severityCount(merged)
  history.push({ round, merged, must, count, claudeReview, codexReview, implementation })
  log(`round ${round}: findings=${merged.length} blocking+major=${count}`)

  if (count === 0) {
    return { status: 'clean', rounds: round, findings: merged, history }
  }
  // 停止条件: 直近 2 ラウンドで blocking+major が厳密に減らなければ人へ返す
  if (prevCount !== null && count >= prevCount) {
    return {
      status: 'stalled', rounds: round, findings: merged, history,
      message: `blocking+major が ${prevCount} → ${count} と減らなかったため停止した。裁定と方針判断を人（メインの Fable）へ戻す。`,
    }
  }
  prevCount = count
}

return {
  status: 'max_rounds',
  rounds: round,
  findings: history[history.length - 1].merged,
  history,
  message: `ラウンド上限 ${MAX_ROUNDS}（予算ガード）に達した。残りの findings の裁定は人が行うこと。`,
}
