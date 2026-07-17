# Claude File Paste (Mac → Remote-SSH → WSL 対応版)

Mac のクリップボードにある画像やファイルを、VS Code のターミナル(リモート側)にファイルパスとして貼り付けるための拡張。Claude Code CLI に画像を渡す用途を想定。

[Richard-Weiss の gist](https://gist.github.com/Richard-Weiss/2840ff94b8547ead1e83b4aa032bcd64) をベースに、Mac クライアント + Remote-SSH 構成向けに改造したもの。

## 仕組み

- `extensionKind: ["ui"]` により、Remote-SSH 接続中も **Mac 側**で実行される
- macOS のクリップボードを osascript (JXA / NSPasteboard) で読み取る
  - Finder でコピーしたファイルがあればそのファイル群
  - なければ画像データ (PNG、なければ TIFF→PNG 変換)
- リモート接続中は `vscode.workspace.fs` でリモートの `/tmp/claude_paste/` にファイルを転送し、そのリモートパスをターミナルに挿入する
- ローカルウィンドウではローカルパスをそのまま挿入する

## インストール (Mac 側に入れること!)

UI 拡張なので **Mac ローカルの VS Code にインストール**する必要がある。リモートウィンドウからインストールすると WSL 側に入ってしまい動かない。

1. リモートウィンドウのエクスプローラーで `claude-file-paste` フォルダを右クリック → **Download...** で Mac に保存
2. Mac で**ローカルの新規ウィンドウ**を開く (File > New Window。左下にリモートインジケータが無い状態)
3. コマンドパレット → **`Developer: Install Extension from Location...`** → ダウンロードしたフォルダを選択
4. リモートウィンドウに戻って **Reload Window**。拡張ビューの「ローカル - インストール済み」に表示されれば OK

更新時も同じ手順 (再ダウンロード → 再 Install → Reload)。

## 使い方

1. Mac でスクリーンショットをクリップボードに撮る (`Cmd+Ctrl+Shift+4`)、または Finder でファイルをコピー
2. VS Code のターミナル (Claude Code が動いているところ) にフォーカス
3. **`Cmd+Shift+Alt+I`** (Mac) / `Ctrl+Shift+Alt+I` (Win/Linux) を押す
   - またはコマンドパレット → 「Paste File for Claude」
4. `/tmp/claude_paste/claude_paste_01.png` のようなパスが挿入されるので、そのまま Claude Code に送信する

## 設定

| 設定 | 既定値 | 説明 |
|---|---|---|
| `claudeFilePaste.remoteDir` | `/tmp/claude_paste` | リモート側の保存先ディレクトリ |
| `claudeFilePaste.maxFileSizeMB` | `50` | 転送する 1 ファイルの上限サイズ (MB) |
| `claudeFilePaste.insertMethod` | `sendText` | ターミナルへの挿入方法 |

## トラブルシューティング

**初回実行時に macOS がクリップボードアクセスの許可を求めてくる**
macOS 15.4 以降のペーストボードプライバシー機能。「常に許可」を選ぶ。誤って拒否した場合は「システム設定 > プライバシーとセキュリティ」から osascript / VS Code のアクセスを再許可する。

**通知は出るのにターミナルに文字が入らない**
設定で `claudeFilePaste.insertMethod` を `sendSequence` に変更して再試行。

**「リモート側のパスを特定できませんでした」**
リモートのフォルダ (またはファイル) を開いた状態で実行する。

**「クリップボードに画像またはファイルが見つかりません」が出続ける**
Mac のターミナルで JXA 単体を試して切り分ける:

```bash
# スクリーンショットをクリップボードに撮ってから
osascript -l JavaScript -e "
ObjC.import('AppKit');
var pb = \$.NSPasteboard.generalPasteboard;
var data = pb.dataForType('public.png');
console.log(data.isNil() ? 'no png' : 'png ok');
var tiff = pb.dataForType('public.tiff');
console.log(tiff.isNil() ? 'no tiff' : 'tiff ok');
"
```

両方 `no ...` なら、クリップボードに画像が乗っていないか、ペーストボードアクセスが拒否されている。

**旧版 (gist 版) を WSL 側に入れていた場合**
リモート側の拡張一覧からアンインストールしておく (コマンド ID が同じで二重定義になるため)。
