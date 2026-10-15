# セットアップのトラブル対処

## Pythonが見つからない

対象はWindows x64と64-bit Python 3.11/3.12。Codexにbundled runtimeの場所を取得する機能があれば、そのPythonの版とビット数を確認して`setup.ps1 -Python <絶対パス> -WithAsr`へ渡す。`.venv`が既にある場合はそれを保持して検査し、壊れているからと自動削除しない。

利用可能なPythonがない場合は[Python公式のWindows配布](https://www.python.org/downloads/windows/)から対応版を導入する。`setup.ps1`はOS全体へのPythonインストールを自動実行しない。必要な本人操作を説明し、再実行できるよう状態を残す。

## PowerShellスクリプトが実行できない

実行ポリシーや会社の管理制限を無効にしない。外部ツールの導入自体が禁止されている場合は、その制限を別経路で回避しない。`.ps1`の実行だけが使えず、通常のPython実行・ローカル依存導入が許可されている場合は、同じ処理を次の入口から行える。

リポジトリルートで、実在する対応Pythonを`$basePython`に設定する。初回だけ仮想環境を作り、以後は`.venv`のPythonを使う。

```powershell
$repo = (Get-Location).Path
# .venvが存在しない場合のみ
& $basePython -m venv .venv

$clipPython = Join-Path $repo '.venv\Scripts\python.exe'
& $clipPython -X utf8 -m pip --isolated --disable-pip-version-check install --only-binary=:all: --index-url https://pypi.org/simple -r requirements-asr.txt
& $clipPython -X utf8 -m pip --isolated --disable-pip-version-check check
& $clipPython -X utf8 scripts/setup_env.py install-ffmpeg
& $clipPython -X utf8 scripts/setup_env.py init
& $clipPython -X utf8 scripts/setup_env.py doctor --require-asr
```

文字起こしを今回は導入しない依頼なら`requirements.txt`を使い、`--require-asr`を省く。グローバルPythonへpip installせず、仮想環境内へ導入する。Windowsの一時フォルダーが書込不可なら、この実行だけの`TEMP`/`TMP`とpip cacheをリポジトリ内の`.setup`へ向ける。既存設定は`init`で上書きされない。

TwitchDownloaderCLIは別途必要。使用可能な既存CLIがあれば`twitchcut.local.json`の`tools.twitchdownloader`へ絶対パスを設定し、doctorで確認する。なければ[公式リリース](https://github.com/lay295/TwitchDownloader/releases/tag/1.56.5)のWindows x64 CLI ZIPを使う。この版のURL・SHA256は同梱`scripts/install_twitchdownloader.ps1`に固定されている。手動導入時も公式配布とSHAを照合し、専用フォルダーへ展開した実行ファイルを設定する。ハッシュ未確認のバイナリを検証済みとして記録しない。

FFmpegの導入は公式ダウンロードページから参照されるWindows配布元の固定ZIPとSHA256を使う。既存ツールが検出された場合は実probeで確認する。更新のために既存ツールを無条件に置き換えない。

## 設定・文字起こし・接続

- `doctor`の終了0は基本ツールが使える状態。`profile_ready`、ASRモデル、`production_ready`、`upload_ready`は別に読む。未確認を成功と読み替えない。
- `configure --youtube-channel-id`には`UC`で始まる24文字の恒久IDが必要。ハンドル、表示名、URLは代入しない。設定例中の`YOUR_...`は実値ではない。
- モデル未取得は`transcribe --job PATH --download-model`で初回取得する。`doctor`やセットアップはモデル推論を行わない。実VODの制作をまだ依頼されていなければ、ASRの実処理へ進まない。
- ブラウザ・Canvaの接続は現在のCodexに利用可能なツールを確認し、必要な接続を案内する。ログインは本人が行う。パスワードやCookieを求めない。
- 既存設定が壊れている場合も元ファイルを保存し、確認した問題だけを修正する。リポジトリ例の空欄で既存アカウント設定を一括上書きしない。
