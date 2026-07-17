const vscode = require('vscode');
const fs = require('fs');
const path = require('path');
const os = require('os');
const util = require('util');
const { execFile } = require('child_process');

const execFilePromise = util.promisify(execFile);

// JXA (osascript -l JavaScript) script run on the macOS client.
// Reads the pasteboard via NSPasteboard: file URLs first, then image data
// (public.png, or public.tiff converted to PNG). The target path for image
// staging is passed as argv[0]; the result is printed to stdout as JSON.
const JXA_SCRIPT = `
ObjC.import('AppKit');
function run(argv) {
  var targetPath = argv[0];
  var pb = $.NSPasteboard.generalPasteboard;

  var urls = pb.readObjectsForClassesOptions($([$.NSURL.class]), $());
  if (urls && !urls.isNil() && urls.count > 0) {
    var paths = [];
    for (var i = 0; i < urls.count; i++) {
      var u = urls.objectAtIndex(i);
      if (u.isFileURL) paths.push(ObjC.unwrap(u.path));
    }
    if (paths.length > 0) return JSON.stringify({ kind: 'files', paths: paths });
  }

  var data = pb.dataForType('public.png');
  if (data.isNil()) {
    var tiff = pb.dataForType('public.tiff');
    if (!tiff.isNil()) {
      var rep = $.NSBitmapImageRep.imageRepWithData(tiff);
      if (!rep.isNil()) {
        // 4 === NSBitmapImageFileTypePNG (numeric literal: the constant name
        // differs across macOS SDK versions and may not resolve in the bridge)
        data = rep.representationUsingTypeProperties(4, $.NSDictionary.dictionary);
      }
    }
  }
  if (data.isNil()) return JSON.stringify({ kind: 'none' });

  if (!data.writeToFileAtomically(targetPath, true)) {
    return JSON.stringify({ kind: 'error', message: 'failed to write ' + targetPath });
  }
  return JSON.stringify({ kind: 'image', paths: [targetPath] });
}
`;

// PowerShell script for Windows clients. Same behavior as the macOS path:
// first line of stdout is a marker (FILES or IMAGE), following lines are paths.
const PS_SCRIPT = `
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Get-NextAvailableFilename {
    param([string]$extension)
    $tempDir = [System.IO.Path]::GetTempPath()
    $oldest = $null
    for ($i = 1; $i -le 99; $i++) {
        $fullPath = [System.IO.Path]::Combine($tempDir, "claude_paste_$('{0:D2}' -f $i)$extension")
        if (-not (Test-Path $fullPath)) { return $fullPath }
        $item = Get-Item $fullPath
        if ($null -eq $oldest -or $item.LastWriteTime -lt $oldest.LastWriteTime) { $oldest = $item }
    }
    return $oldest.FullName
}

$files = [System.Windows.Forms.Clipboard]::GetFileDropList()
if ($files -and $files.Count -gt 0) {
    $validPaths = @($files | Where-Object { Test-Path $_ })
    if ($validPaths.Count -gt 0) {
        Write-Output "FILES"
        $validPaths | ForEach-Object { Write-Output $_ }
        exit 0
    }
    Write-Error "No valid files found in clipboard"
    exit 1
}

$image = [System.Windows.Forms.Clipboard]::GetImage()
if ($null -eq $image) {
    Write-Error "NO_CONTENT"
    exit 1
}
$tempPath = Get-NextAvailableFilename -extension ".png"
$image.Save($tempPath, [System.Drawing.Imaging.ImageFormat]::Png)
$image.Dispose()
Write-Output "IMAGE"
Write-Output $tempPath
`;

/**
 * @param {vscode.ExtensionContext} context
 */
function activate(context) {
    context.subscriptions.push(
        vscode.commands.registerCommand('claude-file-paste.pasteFile', pasteFile)
    );
}

async function pasteFile() {
    try {
        const terminal = vscode.window.activeTerminal;
        if (!terminal) {
            vscode.window.showErrorMessage(
                'Claude File Paste: アクティブなターミナルがありません。ターミナルを開いてから実行してください。'
            );
            return;
        }

        await vscode.window.withProgress({
            location: vscode.ProgressLocation.Notification,
            title: 'Claude File Paste',
            cancellable: false
        }, async (progress) => {
            progress.report({ message: 'クリップボードを読み取り中...' });
            const entries = await getClipboardEntries();

            const remoteName = vscode.env.remoteName;
            let insertPaths;
            if (!remoteName) {
                // Purely local window: insert local paths as-is
                insertPaths = entries.paths.map(p => escapeLocalPath(p, terminal));
            } else if (remoteName === 'wsl') {
                // Windows client + WSL remote: WSL can read the Windows FS directly
                insertPaths = entries.paths.map(windowsToWslPath);
            } else {
                // ssh-remote etc.: transfer bytes to the remote via workspace.fs
                progress.report({ message: 'リモートへ転送中...' });
                insertPaths = await transferToRemote(entries);
            }

            if (insertPaths.length === 0) {
                throw new Error('挿入できるファイルがありませんでした');
            }

            await insertIntoTerminal(terminal, insertPaths.join(' '));

            if (insertPaths.length === 1) {
                let sizeNote = '';
                try {
                    const sizeKB = Math.round(fs.statSync(entries.paths[0]).size / 1024);
                    sizeNote = ` (${sizeKB}KB)`;
                } catch { /* size is informational only */ }
                vscode.window.showInformationMessage(
                    `Claude File Paste: ${path.posix.basename(insertPaths[0].replace(/\\/g, '/'))}${sizeNote} を挿入しました`
                );
            } else {
                vscode.window.showInformationMessage(
                    `Claude File Paste: ${insertPaths.length} 件のファイルを挿入しました`
                );
            }
        });
    } catch (error) {
        vscode.window.showErrorMessage(`Claude File Paste: ${error.message}`);
    }
}

/**
 * Read the clipboard on the client machine.
 * @returns {Promise<{kind: 'files'|'image', paths: string[]}>} local absolute paths
 */
async function getClipboardEntries() {
    if (process.platform === 'darwin') {
        return getClipboardDarwin();
    }
    if (process.platform === 'win32' ||
        (process.platform === 'linux' && fs.existsSync('/mnt/c/Windows'))) {
        return getClipboardWindows();
    }
    throw new Error('このプラットフォームには対応していません (macOS / Windows クライアントのみ)');
}

async function getClipboardDarwin() {
    const target = nextLocalSlot(os.tmpdir(), '.png');
    let stdout;
    try {
        ({ stdout } = await execFilePromise(
            '/usr/bin/osascript',
            ['-l', 'JavaScript', '-e', JXA_SCRIPT, target],
            { timeout: 15000 }
        ));
    } catch (error) {
        const detail = (error.stderr || error.message || '').trim();
        throw new Error(
            'クリップボードの読み取りに失敗しました。macOS の「システム設定 > プライバシーとセキュリティ」で ' +
            'osascript のペーストボードアクセスが拒否されていないか確認してください。詳細: ' + detail
        );
    }

    let result;
    try {
        result = JSON.parse(stdout.trim());
    } catch {
        throw new Error('クリップボード読み取り結果を解釈できませんでした: ' + stdout.trim());
    }

    if (result.kind === 'none') {
        throw new Error(
            'クリップボードに画像またはファイルが見つかりません。' +
            'スクリーンショット (Cmd+Ctrl+Shift+4) を撮るか、Finder でファイルをコピーしてから実行してください。'
        );
    }
    if (result.kind === 'error') {
        throw new Error(result.message);
    }
    return result;
}

async function getClipboardWindows() {
    const isWsl = process.platform === 'linux';
    const candidates = isWsl ? ['pwsh.exe', 'powershell.exe'] : ['pwsh', 'powershell'];

    let lastError;
    for (const shell of candidates) {
        try {
            const { stdout } = await execFilePromise(
                shell,
                ['-NoProfile', '-Command', PS_SCRIPT],
                { timeout: 15000 }
            );
            const lines = stdout.trim().split(/\r?\n/).filter(l => l.trim());
            const marker = lines.shift();
            if ((marker !== 'FILES' && marker !== 'IMAGE') || lines.length === 0) {
                throw new Error('PowerShell の出力を解釈できませんでした: ' + stdout.trim());
            }
            return { kind: marker === 'FILES' ? 'files' : 'image', paths: lines };
        } catch (error) {
            if (error.code === 'ENOENT') {
                lastError = error;
                continue; // try the next shell
            }
            const detail = (error.stderr || error.message || '').trim();
            if (detail.includes('NO_CONTENT')) {
                throw new Error(
                    'クリップボードに画像またはファイルが見つかりません。コピーしてから実行してください。'
                );
            }
            throw new Error('クリップボードの読み取りに失敗しました: ' + detail);
        }
    }
    throw new Error('pwsh / powershell が見つかりませんでした: ' + lastError.message);
}

/**
 * Find a free claude_paste_NN slot in a local directory (01-99);
 * when all slots are taken, reuse the oldest by mtime.
 */
function nextLocalSlot(dir, ext) {
    let oldest = null;
    for (let i = 1; i <= 99; i++) {
        const p = path.join(dir, `claude_paste_${String(i).padStart(2, '0')}${ext}`);
        let st;
        try {
            st = fs.statSync(p);
        } catch {
            return p;
        }
        if (!oldest || st.mtimeMs < oldest.mtimeMs) {
            oldest = { p, mtimeMs: st.mtimeMs };
        }
    }
    return oldest.p;
}

/**
 * Copy local files to the remote via vscode.workspace.fs and return the
 * remote POSIX paths to insert into the terminal.
 */
async function transferToRemote(entries) {
    const config = vscode.workspace.getConfiguration('claudeFilePaste');
    const remoteDir = String(config.get('remoteDir', '/tmp/claude_paste')).replace(/\/+$/, '');
    const maxBytes = Number(config.get('maxFileSizeMB', 50)) * 1024 * 1024;

    const base = resolveRemoteBaseUri();
    const dirUri = base.with({ path: remoteDir, query: '', fragment: '' });
    await vscode.workspace.fs.createDirectory(dirUri);
    const existing = new Set((await vscode.workspace.fs.readDirectory(dirUri)).map(([name]) => name));

    const inserted = [];
    const skipped = [];
    for (const localPath of entries.paths) {
        let st;
        try {
            st = fs.statSync(localPath);
        } catch {
            skipped.push(`${path.basename(localPath)} (読み取り不可)`);
            continue;
        }
        if (st.isDirectory()) {
            skipped.push(`${path.basename(localPath)} (ディレクトリは非対応)`);
            continue;
        }
        if (st.size > maxBytes) {
            skipped.push(`${path.basename(localPath)} (サイズ上限超過)`);
            continue;
        }

        const name = entries.kind === 'image'
            ? await nextRemoteImageName(dirUri, existing)
            : uniqueName(sanitizeName(path.basename(localPath)), existing);
        existing.add(name);

        const bytes = await fs.promises.readFile(localPath);
        await vscode.workspace.fs.writeFile(dirUri.with({ path: `${remoteDir}/${name}` }), bytes);
        inserted.push(`${remoteDir}/${name}`);
    }

    if (skipped.length > 0) {
        vscode.window.showWarningMessage(`Claude File Paste: スキップ: ${skipped.join(', ')}`);
    }
    return inserted;
}

/**
 * Resolve a vscode-remote:// URI to reuse its authority for remote writes.
 * env.remoteName only exposes the kind ('ssh-remote'), not the host, so the
 * authority has to come from an open folder/editor/tab.
 */
function resolveRemoteBaseUri() {
    const folders = vscode.workspace.workspaceFolders || [];
    const remoteFolder = folders.find(f => f.uri.scheme === 'vscode-remote');
    if (remoteFolder) return remoteFolder.uri;

    const editor = vscode.window.activeTextEditor;
    if (editor && editor.document.uri.scheme === 'vscode-remote') return editor.document.uri;

    for (const group of vscode.window.tabGroups.all) {
        for (const tab of group.tabs) {
            const input = tab.input;
            if (input && input.uri && input.uri.scheme === 'vscode-remote') return input.uri;
        }
    }
    throw new Error('リモート側のパスを特定できませんでした。リモートのフォルダまたはファイルを開いた状態で実行してください。');
}

async function nextRemoteImageName(dirUri, existing) {
    const slotName = i => `claude_paste_${String(i).padStart(2, '0')}.png`;
    for (let i = 1; i <= 99; i++) {
        if (!existing.has(slotName(i))) return slotName(i);
    }
    let oldest = null;
    for (let i = 1; i <= 99; i++) {
        const name = slotName(i);
        try {
            const st = await vscode.workspace.fs.stat(dirUri.with({ path: `${dirUri.path}/${name}` }));
            if (!oldest || st.mtime < oldest.mtime) oldest = { name, mtime: st.mtime };
        } catch { /* treat unstatable slots as unusable */ }
    }
    return oldest ? oldest.name : slotName(1);
}

function sanitizeName(name) {
    const sanitized = name.replace(/[^A-Za-z0-9._-]/g, '_');
    return sanitized.replace(/^\.+/, '_') || 'file';
}

function uniqueName(name, existing) {
    if (!existing.has(name)) return name;
    const ext = path.extname(name);
    const stem = name.slice(0, name.length - ext.length);
    for (let i = 2; ; i++) {
        const candidate = `${stem}_${i}${ext}`;
        if (!existing.has(candidate)) return candidate;
    }
}

function escapeLocalPath(p, terminal) {
    if (process.platform === 'win32') {
        if (terminal.name && terminal.name.toLowerCase().includes('wsl')) {
            return windowsToWslPath(p);
        }
        return /\s/.test(p) ? `"${p}"` : p;
    }
    return p.replace(/ /g, '\\ ');
}

function windowsToWslPath(windowsPath) {
    return windowsPath
        .replace(/\\/g, '/')
        .replace(/^([A-Za-z]):/, (match, drive) => `/mnt/${drive.toLowerCase()}`);
}

async function insertIntoTerminal(terminal, text) {
    const method = vscode.workspace.getConfiguration('claudeFilePaste').get('insertMethod', 'sendText');
    if (method === 'sendSequence') {
        await vscode.commands.executeCommand('workbench.action.terminal.sendSequence', { text });
        return;
    }
    try {
        terminal.sendText(text, false);
    } catch {
        await vscode.commands.executeCommand('workbench.action.terminal.sendSequence', { text });
    }
}

function deactivate() {}

module.exports = {
    activate,
    deactivate
};
