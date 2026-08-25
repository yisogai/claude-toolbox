export const meta = {
  name: 'deep-review',
  description: '多視点(正しさ/エッジケース/回帰)の Codex pool レビュー → Codex pool による各指摘の敵対的検証 → 統合を行う対話用ワークフロー。実装完了後にまとめてレビューしたいときに使う。',
  phases: [
    { title: 'Review', detail: 'pool ドライバが正しさ/エッジケース/回帰の3レンズを Codex pool で並列レビュー', model: 'sonnet (effort: medium) + codex pool (gpt-5.6-terra, effort: high)' },
    { title: 'Verify', detail: 'pool ドライバが各指摘を Codex pool で敵対的に検証(最大8件ずつ直列バッチ)', model: 'sonnet (effort: medium) + codex pool (gpt-5.6-terra, effort: high)' },
    { title: 'Synthesize', detail: '確認済み指摘を統合し、重複排除・優先順位付けして最終判定', model: 'opus (effort: high)' },
  ],
}

// deep-review: 3レンズ Review と各指摘の Verify は Codex pool、最終裁定は opus。
// pool は codex exec と同じ flock を使うため、Review pool と Verify pool は必ず直列にする。
// args: レビュー対象の説明(例: "src/foo.py の直前の変更" "PR #123 の diff")。
// 省略時は cwd の未コミットの変更(git diff)を対象とする。

const CODEX_BRIDGE = '/Users/isogai/Documents/personal/tools/claude-toolbox/codex-bridge'
const RENDER_PROMPT = '/Users/isogai/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/render_prompt.py'
const CODEX_POOL = '/Users/isogai/Documents/personal/tools/claude-toolbox/codex-bridge/scripts/codex_pool.py'
const REVIEW_SCHEMA_PATH = '/Users/isogai/Documents/personal/tools/claude-toolbox/codex-bridge/templates/prompts/review.schema.json'
const VERIFY_SCHEMA_PATH = '/Users/isogai/Documents/personal/tools/claude-toolbox/codex-bridge/templates/prompts/verify.schema.json'
const WORKTREE = typeof process !== 'undefined' && process.cwd ? process.cwd() : '/Users/isogai/Documents/personal/tools/claude-toolbox'
const TARGET = (typeof args === 'string' && args.trim()) || 'このリポジトリの cwd における未コミットの変更(git diff で確認できる範囲)'
const JOB_TIMEOUT_SEC = 900

const FINDINGS_SCHEMA = {
  type: 'object',
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['title', 'file', 'severity', 'detail', 'lens', 'lensLabel'],
        properties: {
          title: { type: 'string' },
          file: { type: 'string', description: 'file:line 形式。不明なら空文字' },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'nit'] },
          detail: { type: 'string', description: '何が問題で、どんな実害・悪化があるか' },
          // レンズ帰属はスキーマで強制する(プロンプト依存にしない)。値は pool job の id と1対1。
          lens: { type: 'string', enum: ['correctness', 'edge-case', 'regression'] },
          lensLabel: { type: 'string', enum: ['正しさ', 'エッジケース', '既存機能の回帰'] },
        },
      },
    },
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

// Claude ドライバが pool の成功・失敗を握りつぶさないための外側の契約。
// findings の中身は既存 FINDINGS_SCHEMA と同じ形に正規化して返す。
const REVIEW_POOL_SCHEMA = {
  type: 'object',
  required: ['status', 'findings'],
  properties: {
    status: { type: 'string', enum: ['completed', 'failed'] },
    error: { type: 'string' },
    findings: FINDINGS_SCHEMA.properties.findings,
  },
}

const VERIFY_POOL_SCHEMA = {
  type: 'object',
  required: ['status', 'findings', 'refuted'],
  properties: {
    status: { type: 'string', enum: ['completed', 'failed'] },
    error: { type: 'string' },
    findings: FINDINGS_SCHEMA.properties.findings,
    refuted: {
      type: 'array',
      items: {
        type: 'object',
        required: ['title', 'file', 'lens', 'reasoning'],
        properties: {
          title: { type: 'string' },
          file: { type: 'string' },
          lens: { type: 'string' },
          reasoning: { type: 'string' },
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
    focus:
      'この観点: ロジックの正しさ。誤った条件分岐、型やnull/undefinedの取り違え、' +
      'オフバイワン、計算・変換の誤り、意図と実装のずれを探す。仕様やコメントと実装が食い違う箇所は、' +
      'どちらが正か実行・実挙動を確認してから指摘する。',
  },
  {
    key: 'edge_case',
    label: 'エッジケース',
    focus:
      'この観点: 空入力・境界値(0件/1件/ちょうど上限)・並行実行(競合状態・check-then-act)・' +
      'エンコーディング(マルチバイト・改行・引用符・NULバイト)を狙って調べる。' +
      '通常系だけ動いて異常系・境界系で壊れる実装を優先的に探す。',
  },
  {
    key: 'regression',
    label: '既存機能の回帰',
    focus:
      'この観点: この変更が既存の挙動・既存のテスト・既存の呼び出し元を壊していないか。' +
      '既存テストを実行し、変更前は通っていたはずのものが変更後も通るかを確認する。' +
      '変更範囲外に副作用が及んでいないかも見る。',
  },
]

const reviewDriverPrompt =
  'あなたは Codex pool の Review ドライバである。自分でレビュー結果を推測・補完せず、以下の手順を最後まで実行して pool の結果だけを正規化して返す。' +
  'ファイルを編集してはならない。Review pool と他の pool はこの Workflow では直列であるため、このエージェント内で pool を2回以上起動してはならない。\n\n' +
  '## 固定値\n' +
  'WORKTREE=' + WORKTREE + '\n' +
  'TARGET=' + JSON.stringify(TARGET) + '\n' +
  'CODEX_BRIDGE=' + CODEX_BRIDGE + '\n' +
  'RENDER_PROMPT=' + RENDER_PROMPT + '\n' +
  'CODEX_POOL=' + CODEX_POOL + '\n' +
  'REVIEW_SCHEMA=' + REVIEW_SCHEMA_PATH + '\n' +
  'JOB_TIMEOUT_SEC=' + JOB_TIMEOUT_SEC + '\n\n' +
  '## 手順\n' +
  '1. mktemp -d /tmp/deep-review-review.XXXXXX で WORK_DIR を作る。git -C "$WORKTREE" diff > "$WORK_DIR/diff.patch" を実行する。' +
  ' diff が空でも失敗ではない。\n' +
  '2. 制御不能な値（TARGET を含むコンテキスト等）を**シェルのインライン引数で渡してはならない**。引用符・バッククォート・$(...)・改行が' +
  'シェル解釈され、終了コード 0 のまま内容が静かに書き換わる。値は必ずファイルに書いて --set-file で渡す。ファイルは echo / printf / heredoc ではなく' +
  ' **Write ツール**で書く（heredoc は終端文字列衝突で壊れる）。具体的には: (a) "$WORK_DIR/context.txt" に TARGET と ' + JSON.stringify(LENS_CONTEXT) + ' を Write で書く。' +
  '(b) 各レンズの FOCUS 文（下記）も "$WORK_DIR/<id>.focus.txt" に Write で書く。\n' +
  '3. 次の3レンズごとに python3 "$RENDER_PROMPT" review --set-file DIFF="$WORK_DIR/diff.patch" --set-file FOCUS="$WORK_DIR/<id>.focus.txt" --set-file CONTEXT="$WORK_DIR/context.txt" --out "$WORK_DIR/<id>.md" を実行して prompt を生成する。' +
  ' correctness（正しさ）: ' + LENSES[0].focus + '\n' +
  ' edge-case（エッジケース）: ' + LENSES[1].focus + '\n' +
  ' regression（既存機能の回帰）: ' + LENSES[2].focus + '\n' +
  '4. $WORK_DIR/jobs.json は正確に次の形の JSON にする。id は correctness / edge-case / regression、cwd はすべて WORKTREE、write はすべて false、' +
  'prompt_file は手順3の絶対パス、output_schema_file は REVIEW_SCHEMA とする。\n' +
  '{"jobs":[{"id":"correctness","cwd":"<WORKTREE>","prompt_file":"<絶対promptパス>","write":false,"output_schema_file":"' + REVIEW_SCHEMA_PATH + '"},{"id":"edge-case","cwd":"<WORKTREE>","prompt_file":"<絶対promptパス>","write":false,"output_schema_file":"' + REVIEW_SCHEMA_PATH + '"},{"id":"regression","cwd":"<WORKTREE>","prompt_file":"<絶対promptパス>","write":false,"output_schema_file":"' + REVIEW_SCHEMA_PATH + '"}]}\n' +
  '5. pool は次の1回だけ実行する: python3 "$CODEX_POOL" run --jobs-file "$WORK_DIR/jobs.json" --pool-dir "$WORK_DIR/pool" --max-parallel 3 --model gpt-5.6-terra --effort high --job-timeout-sec "$JOB_TIMEOUT_SEC"。長時間なので Bash の background 実行相当で完了まで待つ。\n' +
  '6. 終了コード 0 だけを成功とする。2=失敗あり、3=timeout、4=起動失敗である。非0なら $WORK_DIR/pool/pool.json と各 $WORK_DIR/pool/jobs/<id>/job.json の status / errors / last_message_path を読み、' +
  '{"status":"failed","error":"exit=<code>; pool=...; jobs=...","findings":[]} を返して停止する。opus など別のレビューへのフォールバックは禁止。\n' +
  '7. 成功時も各 $WORK_DIR/pool/jobs/<id>/job.json を読み、status が completed、structured_output が JSON object であることを確認する。' +
  'structured_output.findings を返す。review schema の severity は blocking→critical、major→high、minor→medium、nit→nit に変換し、file と line_start は file:line_start（line_start が無ければ file）、description は detail に入れる。' +
  '各 finding には、それを出した pool job の id をそのまま lens に、対応する日本語ラベルを lensLabel に**必ず**入れる（correctness→正しさ / edge-case→エッジケース / regression→既存機能の回帰。両フィールドとも省略禁止・スキーマ必須）。' +
  'job.json が欠ける・不正なら status=failed として同じ失敗形式で返す。\n\n' +
  '## 最終出力\n' +
  '構造化出力だけを返す。成功時は {"status":"completed","findings":[...]}。失敗時は上記の status="failed" と具体的な error を必ず返す。'

const verifyDriverPrompt = (batch, batchNumber) =>
  'あなたは Codex pool の Verify ドライバである。自分で検証結果を推測・補完せず、以下の手順を最後まで実行して pool の結果だけを正規化して返す。' +
  'ファイルを編集してはならない。このエージェント内で pool を2回以上起動してはならない。\n\n' +
  '## 固定値\n' +
  'WORKTREE=' + WORKTREE + '\n' +
  'TARGET=' + JSON.stringify(TARGET) + '\n' +
  'RENDER_PROMPT=' + RENDER_PROMPT + '\n' +
  'CODEX_POOL=' + CODEX_POOL + '\n' +
  'VERIFY_SCHEMA=' + VERIFY_SCHEMA_PATH + '\n' +
  'JOB_TIMEOUT_SEC=' + JOB_TIMEOUT_SEC + '\n' +
  'BATCH=' + JSON.stringify(batch, null, 2) + '\n\n' +
  '## 手順\n' +
  '1. mktemp -d /tmp/deep-review-verify.XXXXXX で WORK_DIR を作り、git -C "$WORKTREE" diff > "$WORK_DIR/diff.patch" を実行する。\n' +
  '2. BATCH の各 finding に一意な id（例: verify-1）を割り当て、次の2ファイルを**Write ツール**で書く（echo / printf / heredoc は禁止。' +
  'finding の detail はコード片・引用符・バッククォート・$(...) を含む制御不能テキストであり、シェルのインライン引数に載せると終了コード 0 のまま静かに書き換わる）: ' +
  '"$WORK_DIR/<id>.claim.txt" には title / file / severity / detail / lens / lensLabel を含む JSON を、' +
  '"$WORK_DIR/<id>.context.txt" には TARGET と diff の所在（$WORK_DIR/diff.patch）を書く。\n' +
  '3. 各 finding ごとに python3 "$RENDER_PROMPT" verify --set-file CLAIM="$WORK_DIR/<id>.claim.txt" --set-file CONTEXT="$WORK_DIR/<id>.context.txt" --out "$WORK_DIR/<id>.md" を実行する。' +
  ' CLAIM / CONTEXT を --set のインライン引数で渡してはならない。\n' +
  '4. $WORK_DIR/jobs.json は正確に {"jobs":[{"id":"verify-1","cwd":"<WORKTREE>","prompt_file":"<絶対promptパス>","write":false,"output_schema_file":"' + VERIFY_SCHEMA_PATH + '"}]} の形を、' +
  'BATCH の全件（1〜8件）で配列化した JSON にする。各 job は id / cwd / prompt_file / write:false / output_schema_file を持つ。\n' +
  '5. pool は次の1回だけ実行する: python3 "$CODEX_POOL" run --jobs-file "$WORK_DIR/jobs.json" --pool-dir "$WORK_DIR/pool" --max-parallel 3 --model gpt-5.6-terra --effort high --job-timeout-sec "$JOB_TIMEOUT_SEC"。長時間なので Bash の background 実行相当で完了まで待つ。\n' +
  '6. 終了コード 0 だけを成功とする。2=失敗あり、3=timeout、4=起動失敗である。非0なら $WORK_DIR/pool/pool.json と各 $WORK_DIR/pool/jobs/<id>/job.json の status / errors / last_message_path を読み、' +
  '{"status":"failed","error":"verify batch ' + batchNumber + '; exit=<code>; pool=...; jobs=...","findings":[],"refuted":[]} を返して停止する。opus など別の検証へのフォールバックは禁止。\n' +
  '7. 成功時も各 $WORK_DIR/pool/jobs/<id>/job.json を読む。status が completed、structured_output が JSON object であることを確認する。' +
  'structured_output の verdict/evidence/correction/note を BATCH の元 finding に対応付ける。verdict=refuted は findings から落とし refuted に {title,file,lens,reasoning} として入れる。' +
  'verdict=confirmed は元 finding を findings に残し、evidence と correction を detail に追記する。verdict=plausible も残すが、detail の先頭に [plausible: 反証も確認もできていない] を付け、evidence と note を追記する。' +
  'findings に残す finding は BATCH の元 finding の lens / lensLabel を**そのまま保持**する（書き換え・省略禁止）。' +
  'job.json が欠ける・不正なら status=failed として同じ失敗形式で返す。\n\n' +
  '## 最終出力\n' +
  '構造化出力だけを返す。成功時は {"status":"completed","findings":[...],"refuted":[...]}。失敗時は上記の status="failed" と具体的な error を必ず返す。'

// ─── Phase 1: Review (Codex pool を sonnet ドライバが1回だけ実行) ───
phase('Review')
log('3レンズ pool レビュー開始: ' + LENSES.map((lens) => lens.label).join(' / '))

const reviewResult = await agent(reviewDriverPrompt, {
  label: 'review-pool-driver',
  phase: 'Review',
  schema: REVIEW_POOL_SCHEMA,
  model: 'sonnet',
  effort: 'medium',
})

if (!reviewResult || reviewResult.status !== 'completed') {
  return {
    overall_verdict: 'changes_required',
    summary: 'Review pool が失敗したため、レビュー結果を作れなかった。' + ((reviewResult && reviewResult.error) ? ' ' + reviewResult.error : ' ドライバから完了結果が返らなかった。'),
    ranked_findings: [],
    confirmedCount: 0,
    refutedCount: 0,
    poolFailure: reviewResult || { status: 'failed', error: 'Review pool ドライバが結果を返さなかった' },
  }
}

const allFindings = reviewResult.findings || []
log('pool レビュー完了: 指摘 計' + allFindings.length + '件')

if (allFindings.length === 0) {
  return {
    overall_verdict: 'no_blocking_issues',
    summary: '3レンズ pool レビューはいずれも指摘なし。各レンズが実際に対象を調べた範囲は pool job.json の structured_output を参照。',
    ranked_findings: [],
    confirmedCount: 0,
    refutedCount: 0,
  }
}

// ─── Phase 2: Verify (最大8件ずつ、Codex pool を直列バッチで実行) ───
phase('Verify')
const verified = []
const refuted = []

for (let start = 0; start < allFindings.length; start += 8) {
  const batch = allFindings.slice(start, start + 8)
  const batchNumber = start / 8 + 1
  log('pool 検証 batch ' + batchNumber + ': ' + batch.length + '件')

  const verification = await agent(verifyDriverPrompt(batch, batchNumber), {
    label: 'verify-pool-driver-b' + batchNumber,
    phase: 'Verify',
    schema: VERIFY_POOL_SCHEMA,
    model: 'sonnet',
    effort: 'medium',
  })

  if (!verification || verification.status !== 'completed') {
    return {
      overall_verdict: 'changes_required',
      summary: 'Verify pool batch ' + batchNumber + ' が失敗したため、検証結果を作れなかった。' + ((verification && verification.error) ? ' ' + verification.error : ' ドライバから完了結果が返らなかった。'),
      ranked_findings: [],
      confirmedCount: verified.length,
      refutedCount: refuted.length,
      poolFailure: verification || { status: 'failed', error: 'Verify pool ドライバが結果を返さなかった' },
      verifiedBeforeFailure: verified,
      refuted,
    }
  }

  verified.push(...(verification.findings || []))
  refuted.push(...(verification.refuted || []))
}

log('pool 検証完了: confirmed/plausible=' + verified.length + ' refuted=' + refuted.length)

if (verified.length === 0) {
  return {
    overall_verdict: 'no_blocking_issues',
    summary: allFindings.length + '件の指摘はすべて Codex pool の敵対的検証で反証された。詳細は refuted を参照。',
    ranked_findings: [],
    confirmedCount: 0,
    refutedCount: refuted.length,
    refuted,
  }
}

// ─── Phase 3: Synthesize (統合, opus) ───
phase('Synthesize')
const confirmedBlock = verified
  .map(
    (finding, index) =>
      '### [' + index + '] (' + (finding.lensLabel || finding.lens || 'レンズ不明') + ' / ' + finding.severity + ') ' + finding.title + '\n' +
      'file: ' + finding.file + '\n詳細: ' + finding.detail + '\n'
  )
  .join('\n')

const synthesis = await agent(
  '以下は3レンズ(正しさ/エッジケース/既存機能の回帰)によるレビューのうち、Codex pool の敵対的検証で refuted されず残った指摘である。\n\n' +
    confirmedBlock +
    '\n\n## 任務\n' +
    '1. 同じ問題を指す重複指摘があれば統合する。\n' +
    '2. severity と実害の大きさで優先順位を付ける。[plausible: ...] 印の指摘は未確定であることを考慮し、confirmed 相当の根拠を持つ指摘より優先度を下げる。\n' +
    '3. overall_verdict を決める: critical/high が1件でもあれば changes_required、medium以下のみなら changes_recommended、' +
    '該当なしなら no_blocking_issues。\n' +
    '4. 3-5文の summary を書く。\n\n構造化出力のみ。',
  { label: 'synthesize', phase: 'Synthesize', schema: SYNTH_SCHEMA, model: 'opus', effort: 'high' }
)

if (!synthesis) {
  // 統合ステップが失敗しても、pool で検証済みの指摘は生のまま返す(握りつぶさない)。
  return {
    overall_verdict: verified.some((finding) => finding.severity === 'critical' || finding.severity === 'high')
      ? 'changes_required'
      : 'changes_recommended',
    summary: '統合(Synthesize)ステップが結果を返さなかったため、pool 検証済み指摘を未統合のまま返す。',
    ranked_findings: verified.map((finding) => ({
      title: finding.title,
      file: finding.file,
      severity: finding.severity,
      lens: finding.lens || 'unknown',
      detail: finding.detail,
    })),
    confirmedCount: verified.length,
    refutedCount: refuted.length,
    refuted,
  }
}

return {
  ...synthesis,
  confirmedCount: verified.length,
  refutedCount: refuted.length,
  refuted,
}
