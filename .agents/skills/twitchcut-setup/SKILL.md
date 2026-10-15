---
name: twitchcut-setup
description: このTwitchCutリポジトリを利用者のWindows PCで初期設定し、依存ツール、保存先、TwitchとYouTube設定を診断する。セットアップ、導入、環境修復の依頼に使い、実VOD制作や投稿は別途依頼された場合に進める。
---

# TwitchCutのセットアップ

利用者がこのリポジトリを開いて「私のPCで使えるようにセットアップして」と依頼したとき、説明だけで終わらず、利用可能な環境確認とローカル設定を実行する。

この`SKILL.md`の絶対パスからリポジトリルートを求める。Pythonなら`Path(skill_file).resolve().parents[3]`。ルートの`README.md`、`config.example.json`、`setup.ps1`を読む。任意のCWDや共有元のPCのパスを使わない。

1. Windows x64、利用可能なPython、空き容量、既存の`twitchcut.local.json`と`.venv`を調べる。既存設定を保持し、使用可能なツールを再利用する。
2. `setup.ps1`を通常の実行権限で実行する。64-bit Python 3.11/3.12を使い、既存のPythonやCodexのbundled Pythonを使える場合は`-Python <絶対パス>`を指定できる。ローカル文字起こしまで用意する依頼なので、通常は`-WithAsr`を付ける。ツールの導入・ダウンロード先と不足を結果で確認する。実行ポリシーや管理者の制限を無効にしない。Python未検出やPowerShellスクリプトの実行が拒否された場合は[トラブル対処](references/troubleshooting.md)を読む。
3. `.venv/Scripts/python.exe twitchcut.py doctor`で結果を確認する。終了0は基本環境の準備完了で、ASRや投稿の準備完了とは限らない。ASR必須の検査は`scripts/setup_env.py doctor --require-asr`を使う。失敗があれば対象箇所を直し、影響する項目だけ再確認する。GPUは実環境で使えるときに選び、CPUでも利用できる設定にする。ASRモデルは初回`transcribe --download-model`時に取得するため、ライブラリ導入成功とモデルの実推論成功を区別する。
4. 利用者の情報で`configure`する。既存設定や直接の指示で分かる値は再質問しない。不明なTwitchユーザー名、YouTube恒久チャンネルID、使用ブラウザだけをまとめて確認する。「制作はまだしない」等でアカウント設定を後回しにできる場合は、空欄のまま環境を完成させ、次回に必要な情報として案内する。投稿先未定でも任意のチャンネルを仮登録しない。表示名やハンドルを恒久IDとして記録しない。
5. 利用者が投稿まで使いたい場合はブラウザ操作ツールの有無、指定ブラウザ、YouTube Studioの接続を調べる。Googleログイン、2段階認証、YouTubeの長編投稿確認が必要なら具体的な本人操作を案内する。別プロファイルを探し回らない。ブラウザプラグインやCanvaが使えなければ、現在の環境で必要な接続を案内する。自動導入できたと主張しない。
6. `python -m unittest discover -s tests -p test_render.py -v`で実VODや実サービスを使わない合成素材の書出し検証を実施する。必要ツール不足でskipされた項目を成功と報告しない。導入・検証のために実配信を取得したり、Canvaへ素材を送信したり、YouTubeに試験投稿しない。

設定コマンドは実値だけを渡す。`configure --help`で利用可能なオプションを確認する。

```powershell
& $python twitchcut.py configure --twitch-login $twitchLogin --youtube-channel-id $channelId --browser-profile $browserProfile
& $python twitchcut.py doctor
```

`$python`等は説明用変数。値を確定した引数配列として実行し、利用者の文字列をシェルコードに組み込まない。リポジトリ外に個人用スキルを複製する必要はない。

最終報告は、準備できたもの、本人が行う残りのログイン等、次に送る一文を短く示す。環境準備だけで「動画制作・投稿まで実証済み」と報告しない。次の依頼例は「このVODから3セットを作り、設定したチャンネルへ非公開投稿して：URL」。投稿先設定は投稿承認ではなく、次の制作依頼の範囲を確認してから制作スキルへ移る。
