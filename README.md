# TwitchCut

Twitchのアーカイブから、**縦ショート・話題別横動画・編集版ダイジェスト**を作り、指定したYouTubeチャンネルへ非公開投稿するための、Codex用の制作環境です。

Codexが配信の内容を読み取り、字幕・切りどころ・サムネイル・投稿内容を組み立てます。同梱スクリプトが取得、ローカル文字起こし、書き出し、検査記録、二重投稿防止を補助します。YouTubeへの送信には、利用者のブラウザとログインが必要です。

## 最初にすること

**WindowsのPCで、このリポジトリのフォルダーをCodexに開かせ、次の一文を送ってください。**

```text
このリポジトリを私のPCで使えるようにセットアップしてください。
```

Codexが同梱のセットアップスキルを読み、必要なツールと設定を確認します。設定に必要なTwitchのユーザー名、YouTubeの投稿先、使用するブラウザは、分かる範囲で伝えれば進められます。投稿先が未定でもローカルの制作環境を準備できます。

リポジトリを取得する方法は、次のどちらかです。

- Gitを使う場合：`git clone https://github.com/nomuyu00/TwitchCut.git`
- Gitを使わない場合：GitHubの **Code → Download ZIP** から保存・展開し、`README.md`と`setup.cmd`のあるフォルダーを開く。

非公開リポジトリの場合は、所有者からGitHubの共同作業者に招待してもらい、招待を受け入れてから取得してください。GitHubへの接続だけでPC内にファイルが揃うわけではないため、実際に取得したフォルダーを開きます。

スキルはこのリポジトリの`.agents/skills/`に含まれています。個人のスキルフォルダーへのコピーやプラグイン登録は不要です。認識されない場合はフォルダーを確認してCodexを再起動するか、`.agents/skills/twitchcut-setup/SKILL.md を読んでセットアップして`と伝えてください。[OpenAI公式：スキルの読み込み](https://learn.chatgpt.com/docs/build-skills)

## 用意するもの

| 必要なもの | 用途 |
|---|---|
| Windows x64のPCとローカル実行できるCodex | ファイル取得、文字起こし、編集、書き出し |
| インターネットと十分な空き容量 | 原本、作業用音声、完成動画を保存。長時間VODでは大きな容量が必要 |
| PythonとFFmpeg・ffprobe、TwitchDownloaderCLI | セットアップが確認し、足りないものの導入を案内・実行 |
| ブラウザを操作できるCodex環境とYouTubeへのログイン | 非公開投稿まで依頼する場合。本人の認証操作は本人が行う |
| Canvaの通常編集を使える接続、またはログイン済みブラウザ | 話題別横動画・ダイジェストのサムネイル作成 |

NVIDIA GPUは必須ではありません。CPUでも処理できますが、特に文字起こしと長い動画の書き出しには時間がかかります。GPUを使用する場合は、そのPCで実際に動作するか確認します。ブラウザ接続、Canva接続、Googleログインはこのリポジトリだけでは自動設定できません。

## セットアップで行うこと

`setup.cmd`はダブルクリックでも実行できます。Codexに任せる場合は、通常こちらで操作する必要はありません。

```powershell
# PowerShellから実行する場合
powershell -NoProfile -File .\setup.ps1

# ローカル文字起こし用ライブラリも導入する場合
powershell -NoProfile -File .\setup.ps1 -WithAsr

# 既存のPythonを指定する場合
powershell -NoProfile -File .\setup.ps1 -Python 'C:\path\to\python.exe' -WithAsr
```

実行ポリシーや会社の管理設定で拒否された場合は、エラーをCodexに伝えてください。管理設定を無効にする必要はありません。[Pythonを直接使う導入手順](.agents/skills/twitchcut-setup/references/troubleshooting.md)も用意しています。

個人用設定は`config.example.json`を基にした`twitchcut.local.json`、制作データは既定で`workspace/`に保存します。これらとダウンロードしたツール・モデルはGitの共有対象から除外しています。既存設定は確認して引き継ぎます。パスワード、Cookie、OAuthトークンを設定ファイルに書く必要はありません。

`-WithAsr`は文字起こしライブラリを導入します。大きなモデル本体は初回の文字起こし時に取得します。`doctor`の基本環境が合格しても、モデルの実推論、ブラウザの接続、実動画の検査・投稿まで確認済みという意味ではありません。

設定例は次のとおりです。実際の値をCodexに渡せば、コマンドを自分で編集する必要はありません。

```powershell
.\.venv\Scripts\python.exe twitchcut.py configure --twitch-login YOUR_TWITCH_LOGIN --youtube-channel-id UC_YOUR_CHANNEL_ID --browser-profile '使用するブラウザの名前'
.\.venv\Scripts\python.exe twitchcut.py doctor
```

投稿先は表示名や`@ハンドル`だけでなく、`UC`で始まる恒久チャンネルIDで登録します。Codexは送信前に実際のYouTube Studioと照合します。セットアップやチャンネル登録だけでは、動画の投稿は行いません。

## 普段の使い方

初回は、依頼の範囲が分かるようにURLと一緒に伝えてください。

```text
https://www.twitch.tv/videos/実際のVOD_ID
このアーカイブからショート・話題別横動画・ダイジェストを作り、
設定済みのYouTubeチャンネルへ非公開でアップロードしてください。
```

```text
このアーカイブを切り抜いてください。今回はローカル保存までで、投稿は不要です。
https://www.twitch.tv/videos/実際のVOD_ID
```

再開するときは、`このVODの続きから進めて`とVOD URLを渡します。既存の原本、編集記録、検査、投稿IDを照合して続行します。原本とコメントJSONは全範囲を保存します。ノーカット全編の動画は新規に制作・投稿しません。

字幕はローカルで文字起こしして内容を確認します。元の声の音量を保持し、語尾・笑い・返答が切れないように編集します。サムネイルは各動画の実際の配信画面を使い、Canvaの通常編集で個別に制作します。画像生成は使いません。専用のアバター画像・BGM・効果音は同梱しておらず、必要なら利用者が使用できる素材を指定します。

ブラウザ操作やCanvaが使えない場合は、Codexが完了したローカル成果物と残る具体的な操作を示します。処理中の動画や未確認の非公開設定を「投稿完了」とは扱いません。

## 保存場所と更新

| 場所 | 内容 |
|---|---|
| `twitchcut.local.json` | このPCの保存先、Twitch、YouTube、ツール設定 |
| `workspace/VOD/twitch_<ID>/` | 原本動画、全コメント、取得記録 |
| `workspace/editing/archives/twitch_<ID>/` | 編集計画、字幕、ログ、検査、投稿資料 |
| `workspace/Archives/twitch_<ID>/` | `Shorts`・`Topics`・`Digest`の完成品 |
| `workspace/editing/publishing/` | 重複送信を防ぐ投稿台帳 |

Gitで取得した場合は、作業が止まっているときに`git pull`して更新できます。無視対象の個人設定や動画は更新対象になりません。コードを自分で変更している場合は、Codexに変更を残して更新するよう依頼してください。ZIP版は新しいZIPを別フォルダーへ展開し、既存の個人設定と制作データを確認して引き継ぎます。

このリポジトリを第三者へ渡す際は、コードと説明だけを共有してください。原本、コメント、制作履歴、ローカル設定、アカウントの認証情報を一緒に送る必要はありません。

## 制作・保守の資料

- [セットアップスキル](.agents/skills/twitchcut-setup/SKILL.md)
- [制作・非公開投稿スキル](.agents/skills/twitchcut-publisher/SKILL.md)
- [編集データの形式](docs/edit-format.md)
- [取得・検査・投稿台帳の手順](.agents/skills/twitchcut-publisher/references/operations.md)
- [投稿時の確認と再開](.agents/skills/twitchcut-publisher/references/upload.md)

ツールの公式資料：[TwitchDownloader](https://github.com/lay295/TwitchDownloader)、[FFmpeg](https://ffmpeg.org/documentation.html)、[faster-whisper](https://github.com/SYSTRAN/faster-whisper)。
