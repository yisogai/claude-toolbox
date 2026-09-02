---
name: keepalive
description: 長時間待ち（Workflow の長走行・cloud ジョブ・離席・夜間 CI）の間、メインセッションの 1時間 TTL プロンプトキャッシュを温かく保つための手動スキル。background の sleep を1本だけ立て、その完了通知でメインが起きて no-op → 必要なら再武装する。ユーザーが「keepalive」「キープアライブ」「離席する」「しばらく席を外す」「キャッシュを温めておいて」「待ちの間つないでおいて」等と言ったら使う。自動化はせず、明示的に呼ばれたときだけ動く。全プロジェクトから使える（スクリプトは絶対パスで呼ぶ）。
---

# keepalive — 待ち時間中のプロンプトキャッシュ温存

## これは何か

Claude Code のメインセッションは、1ターンごとに会話文脈全体のプロンプトキャッシュを読む。
このキャッシュの TTL は 1 時間で、切れると次のターンで全文脈を**書き直す**ことになる。

Fable 5.1 の単価（基本入力を 1.0 とする）:

| 種別 | 係数 |
|---|---|
| キャッシュ読取 | 0.025× |
| 1h TTL キャッシュ書込 | 2.0× |

毎時 1 回の no-op ping（＝キャッシュ読取 0.025×）で TTL を延ばし続けるコストと、
期限切れ後に書き直すコスト（2.0×）の損益分岐はおよそ **79 時間**。つまり
**1時間を超える待ちなら keep-alive の方が安い**（`~/.claude/CLAUDE.md` の
「メインのターン・文脈コスト規律」(a) の例外規定に対応）。

仕組みは単純で、`sleep` を Bash ツールの `run_in_background: true` で 1 本だけ立てる。
background タスクの完了はメインを起こすタスク通知になるため、メインは no-op の一言を返すだけで
1 ターン＝キャッシュ読取 1 回が発生し、TTL が延びる。UserPromptSubmit 系 hook は発火しない。

**自動化はしない。** hook や SessionStart での自動起動は行わず、ユーザーが `/keepalive` と
打ったときだけ動く。

## 使い方（ユーザー側）

| コマンド | 意味 |
|---|---|
| `/keepalive` | 既定（50分間隔・上限10本 ≒ 最大約8時間）で開始 |
| `/keepalive 30m` | 間隔を指定して開始（`30m` / `1800s` / `1800` を受理） |
| `/keepalive stop` | 稼働中の keep-alive を止める |
| `/keepalive status` | 稼働状況を表示 |

## 手順（モデル側）

1. **key を決める**。システムプロンプトの scratchpad パス末尾の UUID を使う
   （例: `/private/tmp/claude-.../<UUID>/scratchpad` → `<UUID>`）。取れない場合は
   `default` など任意の固定文字列でよい。同じセッション内では key を変えないこと。
2. **間隔を秒に変換する**。`30m`→1800、`1800s`→1800、`1800`→1800。未指定なら 3000（50分）。
   interval は TTL 1時間より確実に短く保つ（3300 秒＝55分を超えない）。
3. **tick を 1 本だけ発行する**。Bash ツールで **`run_in_background: true`**:
   ```bash
   bash ~/.claude/skills/keepalive/scripts/keepalive.sh tick --key <KEY> --n 1 --cap 10 --interval 3000
   ```
   **多重発行は禁止**（同じ key で二重に立てても後勝ちで前を kill するが、無駄なので出さない）。
   発行したら「keep-alive を開始した（間隔 N 分・上限 M 本）」と一言返してターンを終える。
4. **起床したら出力の指示に従う**。tick の出力は自己記述的なので、compact 後に文脈を失っていても
   そこに書かれた通りに動けばよい:
   - no-op の一言（例: 「keep-alive tick 1/10。待機継続中。」）だけを返す。**余計な作業をしない**。
   - 待ちが続くなら、出力に印字された次のコマンド（`--n N+1`）をそのまま `run_in_background: true` で発行する。
   - `KEEPALIVE: 上限 ... に到達` が出たら再武装せず、ユーザーが戻ったら handoff + `/compact` を提案する。
5. **実タスクの完了通知が来たら止める**。`stop` を呼ぶか、単に再武装しない。
   ユーザーが戻ってきて会話を再開したときも同様に止める（以後は通常のターンで TTL が延びる）。

### stop / status

```bash
bash ~/.claude/skills/keepalive/scripts/keepalive.sh stop   --key <KEY>
bash ~/.claude/skills/keepalive/scripts/keepalive.sh status --key <KEY>
```

## スクリプト I/F

```
keepalive.sh tick   --key <KEY> --n <N> [--cap <CAP>=10] [--interval <SEC>=3000]
keepalive.sh stop   --key <KEY>
keepalive.sh status --key <KEY>
```

- 状態（何本目か）は**セッション内で引数として持ち回る**。ディスク上の状態は pid ファイル
  （`${TMPDIR:-/tmp}/claude-keepalive-<KEY>.pid`）だけ。
- `N > CAP` なら **sleep せず即座に**上限メッセージを出して終了（exit 0）。モデルの記憶に
  依存しない暴走防止。
- 同じ key で既に稼働中のプロセスがあれば kill して置き換える（二重起動防止）。
- SIGTERM を受けたら pid ファイルを消して静かに終了する（stop や再武装時に余計な出力を出さない）。
- 引数不正・未知サブコマンドは usage を stderr に出して exit 2。

## 注意事項

- **自動化しない**。hook で自動起動する運用にはしない（意図しない課金・通知の連鎖を避けるため）。
- **overage 中は使わない**。使用量超過でキャッシュ TTL が 5 分に落ちている状況では 50 分間隔の
  ping は無意味なので、keep-alive は中止する。
- **文脈の忠実度が不要な区切りでは keep-alive より handoff + `/compact`** を選ぶ。keep-alive は
  「同じ文脈のまま長く待つ」ためのもの。
- **Workflow の長走行に注意**: Workflow はフェーズ進行ではメインを起こさず、完了時のみ通知する。
  だから発行直後に keep-alive を立てる価値がある。
- `sleep` の待ち時間中もタスクスロットを 1 つ占有する。実タスクの background ジョブと共存させる
  ことを想定しているが、大量には立てない（常に 1 本）。

## settings.json の配線（任意）

auto mode 以外では Bash 実行の許可プロンプトが出る。毎回許可するのが煩わしければ
`~/.claude/settings.json` の `permissions.allow` に次を足す:

```json
"Bash(bash ~/.claude/skills/keepalive/scripts/keepalive.sh*)"
```

hook の配線は不要（このスキルは hook を使わない）。
