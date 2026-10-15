# 取得・検査・投稿台帳

コマンドはリポジトリルートで実行する。`twitchcut.py`が個人設定を読み、同梱の補助スクリプトへ引数を渡す。`scripts/workflow.py`は媒体指紋、QA参照、概要欄、投稿台帳の整合を検査する。面白さ、聴取、目視、実際の投稿を代行しない。

## ジョブとコマンド

```powershell
$clipPython = Join-Path $repo '.venv\Scripts\python.exe'
& $clipPython -X utf8 twitchcut.py doctor
& $clipPython -X utf8 twitchcut.py download $vodUrl
& $clipPython -X utf8 twitchcut.py transcribe --job $clipJob
& $clipPython -X utf8 twitchcut.py render --edl $edl --output $deliveryMp4
& $clipPython -X utf8 twitchcut.py prepare --job $clipJob --plan $plan
& $clipPython -X utf8 twitchcut.py status --job $clipJob
```

各変数は実際に確認した値を設定する。`$clipJob`は取得コマンドが返す`job.json`のパス。別設定はグローバルオプション`--config PATH`をサブコマンドより前に置く。CLIの`--help`を正とし、未知の引数を推測しない。

上記は各コマンドの入口。長い本番処理は下記のstage specにそのコマンドを入れて実行する。初回downloadもstage管理できるよう、先にジョブだけを作る。この初期化はメディアを取得せず、同じVODの既存jobを上書きしない。

```powershell
& $clipPython -X utf8 scripts/workflow.py init --url $vodUrl --root $workspace --profile $localConfig
```

返された`job`を`$clipJob`へ設定する。`$workspace`は個人設定を基準に解決した制作ルートの絶対パス、`$localConfig`も実個人設定JSONの絶対パス。Twitchユーザー名は事前に設定しておく。

モデル未導入の初回ASRは`transcribe --job $clipJob --download-model`を使う。媒体を外部へ送信せず、固定版モデル本体を取得する。既定はCPU/int8。`asr/transcript.json`・`transcript.txt`・`index.json`を共用し、`inspect --job $clipJob --start 120 --end 180`等で必要区間だけ読む。

| 保存先（設定したworkspace内） | 内容 |
|---|---|
| `VOD/twitch_<ID>/` | 全原本、全コメント、取得メタデータ |
| `editing/archives/twitch_<ID>/job.json` | VOD ID、設定スナップショット、実パス |
| 同フォルダーの`logs/`、`review/` | 実行ログ、取得検査、完成品QA、投稿読戻し証拠 |
| `Archives/twitch_<ID>/Shorts`・`Topics`・`Digest` | 各セットの納品物 |
| 作業フォルダーの`publication.json` | prepareで確定した投稿パッケージ |
| `editing/publishing/upload-ledger.json` | 配信をまたぐ投稿台帳 |

日付やタイトルだけで同じジョブと判定しない。`job.json`がない既存素材も、実ChatのVOD ID、旧編集記録、YouTube投稿IDを調べてから扱う。旧素材を引き継ぐ必要がある場合は`workflow.py adopt --help`を読み、移動や上書きをせず参照を整合させる。既存QAの再利用には対象SHAと検査仕様の一致が必要。

## 取得結果の確認

VOD URLは正規Twitchホストの`/videos/<数字>`を使う。クリップ・ライブURLを全アーカイブとして扱わない。`?t=`の有無にかかわらず、原本は全範囲を取得する。削除済み・権限不足・進行中で尺未確定のVODを完成素材と扱わない。

`download`の出力、取得ログ、メタデータ、ffprobe、チャット検査を読む。TwitchDownloaderの終了0や進捗100%だけで完全取得と判定しない。`using stubs instead`、`will be missing from the finalized video`、`Failed to emit part stubs`、壊れたパケットのログがあれば該当範囲を調べる。回復に成功した再取得と欠損を区別する。部分ファイルを正式名へ改名して失敗を隠さない。

成功時には実際の取得ログ・probe・チャット照合・全体decodeを参照した`source-review.json`が生成される。`human_timeline_reviewed:false`は目視・聴取が自動完了していない意味で、代表的な映像・音声と疑わしい区間の確認を補う。失敗した試行は保持される。原因確認後に再取得する場合だけ`download URL --retry`を使う。

元動画の映像・音声、正の実尺、metadataとの尺差、全体decodeを確認する。チャットは実`video.id`、配信者、`comments`配列、時刻範囲を照合する。コメント0件はあり得るため、その状況を記録する。Chatの最終コメント時刻を映像の実尺と置き換えない。

TwitchDownloaderの`info --format Raw`はmetadata JSON、chapters JSON、M3U8が連結された形式で、全体を単一JSONとして解析しない。署名付きのメディアURLは共有物や概要欄に出さない。CLIの`--resume`を捏造せず、検証済みステージから再開する。

## SHAと長い処理

実ファイルのSHAは共有キャッシュを使う。初回に全体を計算し、ファイルID、サイズ、更新状態が変わったら再計算する。任意のSHAをキャッシュへ登録して合格にしない。必要な場合の直接呼出しは次の形。

```powershell
& $clipPython -X utf8 scripts/workflow.py hash --root $workspace --file $source --file $chat --file $finalMp4
```

長いローカル処理は`scripts/run_stage.py`にJSONの引数配列で渡す。wrapperがすでにstage管理する処理は二重に包まない。例のプレースホルダーは実値へ置き換える。

```json
{
  "schema": 1,
  "id": "topic01-render-v001",
  "command": ["<python.exe>", "-X", "utf8", "<repo>/scripts/render.py", "--config", "<実個人設定JSON>", "--edl", "<実EDL>", "--output", "<実納品MP4>"],
  "resources": ["media-disk", "gpu"],
  "expected_seconds": 600
}
```

```powershell
& $clipPython -X utf8 scripts/run_stage.py run --job $clipJob --spec $stageSpec
& $clipPython -X utf8 scripts/run_stage.py status --job $clipJob
```

`expected_seconds`は処理媒体の尺であり、完了予測時間ではない。`resources`は同じディスク/GPUを使う全処理で同じ名前にし、CPU処理にGPUを指定しない。ログを上書きする再試行は避け、原因を記録してstage IDの版を上げる。終了値と結果を確認し、`progress=end`や状態ファイルの存在だけで成功にしない。

specは当該jobの`work`内へ保存し、`--spec`にはその絶対パスを渡す。実行ファイル・スクリプト・設定・EDL・出力も絶対パスにする。既定の実行cwdはjobの`work`。repoがworkspaceの外にある通常構成では、`cwd`をrepoへ変更しない。downloadやASRを包む場合も`command`は`[python, "-X", "utf8", <repo>/twitchcut.py, "--config", <設定>, "download", <URL>]`等の引数配列にする。親のstageがGPU/ディスクleaseを保持しているとき、その子を別stageへ二重に包まない。

ASR・画面調査はVODごとに共用する。全体decodeは同一の最終SHAと同じ検査仕様で合格済みなら再実行しない。変更時は新SHAのdecodeと影響する検査を実施する。小さいQA・計画ファイルは毎回照合する。読むのは状態要約、必要区間、エラー箇所。依頼のない常駐監視・定期実行は作らない。

## 原本のQA

作業フォルダーの`source-review.json`を完成させる。`download`が生成した実検査は根拠を読んで再利用し、未実施項目だけ補う。以下は形式例であり、`passed:true`は実際の成功後にだけ記入する。

```json
{
  "vod_id": "実際の数字ID",
  "acquisition_mode": "fresh_download",
  "video": {"path": "twitch_ID/source.mp4", "sha256": "実SHA256"},
  "chat": {"path": "twitch_ID/chat.json", "sha256": "実SHA256"},
  "duration_seconds": 12345.67,
  "checks": {
    "download_logs": {"passed": true, "evidence": "review/source-logs.json"},
    "duration_and_streams": {"passed": true, "evidence": "review/source-probe.json"},
    "chat_identity_and_range": {"passed": true, "evidence": "review/chat-check.json"},
    "decode": {"passed": true, "evidence": "review/source-decode.json"}
  }
}
```

`video.path`と`chat.path`は`workspace/VOD`からの相対パスまたは同領域内の絶対パス。検査証拠はジョブの作業フォルダー内。実行コマンド、対象SHA、終了値、ログ、問題の有無を保存する。空のエラーログだけではどの検査を実施したか分からないので、サマリーを残す。

## 完成品のQA

各動画の`review/<asset-id>.json`をQAの正本にする。技術検査レポート、境界音声、フレーム、編集判断は同じ統合証拠を参照してよい。

```json
{
  "media_sha256": "最終MP4の実SHA256",
  "thumbnail_sha256": "横動画のサムネイルの実SHA256",
  "checks": {
    "decode": {"passed": true, "evidence": "review/topic01-technical.json"},
    "speech_boundaries": {"passed": true, "evidence": "review/topic01-boundaries.json"},
    "captions": {"passed": true, "evidence": "review/topic01-visual.md"},
    "privacy_blur": {"passed": true, "evidence": "review/topic01-visual.md"},
    "story_flow": {"passed": true, "evidence": "review/topic01-editorial.md"},
    "audio_mix": {"passed": true, "evidence": "review/topic01-audio.json"},
    "thumbnail": {"passed": true, "evidence": "review/topic01-thumbnail.md"}
  }
}
```

Shortsは`thumbnail_sha256`と`thumbnail`検査が不要。Topics/Digestには必須。レンダーの`.result.json`はdecodeやPCM比較など実施した技術項目に利用できるが、意味、全切点の聴取、ぼかし範囲、実画面の確認を代行しない。字幕だけの変更で音声PCMが同一なら、その証拠を付けて音声検査を再利用できる。

## 投稿計画

この段階は投稿資料を確定するときに行う。`prepare`にはYouTube恒久チャンネルIDの設定が必要。投稿先未定のローカル制作だけなら実行せず、完成品・QA・編集記録を納品する。投稿先を決めた後、未設定だったジョブへ設定を取り込んでprepareできる。非空のジョブの配信者・投稿先が現在設定と異なる場合は拒否されるので、別のチャンネルへ黙って切り替えない。

`publish-plan.json`の`id`は安定した英数字で作り、通信断のたびに変えない。新規`kind`は`short/topic/digest`。`file`と`thumbnail`は納品フォルダーから、`review`は作業フォルダーから解決する。

```json
{
  "source_review": "source-review.json",
  "verified_related_links": [],
  "assets": [
    {
      "id": "short01",
      "kind": "short",
      "file": "Shorts/short01.mp4",
      "title": "実際の会話に合う題名 #Shorts【YYYY.MM.DD】",
      "summary": "この場面固有の紹介文。",
      "review": "review/short01.json"
    },
    {
      "id": "topic01",
      "kind": "topic",
      "file": "Topics/topic01.mp4",
      "title": "ひとつの話の題名【YYYY.MM.DD】",
      "summary": "前提と展開が初見にも伝わる固有の紹介文。",
      "review": "review/topic01.json",
      "thumbnail": "Topics/topic01.jpg"
    },
    {
      "id": "digest01",
      "kind": "digest",
      "file": "Digest/digest01.mp4",
      "title": "配信全体の主題に合う題名【YYYY.MM.DD】",
      "summary": "配信の見どころを紹介する文章。",
      "review": "review/digest01.json",
      "thumbnail": "Digest/digest01.jpg",
      "chapters": [
        {"seconds": 0, "title": "今回の見どころ"},
        {"seconds": 12, "title": "最初の話題"},
        {"seconds": 150, "title": "次の話題"}
      ]
    }
  ]
}
```

日付は元配信metadataを日本時間へ変換した実値。ファイル名や投稿日から推測しない。元VODへのリンクを入れる。任意の関連リンクは`{"label":"表示名","url":"https://...","evidence":"公式プロフィールまたは直接のユーザー指定"}`の形式。未確認のリンクは省略する。実在しないノーカット版へのリンクを作らない。

`prepare`はファイル・QA・サムネイルのSHA、寸法・尺・H.264/AAC、文字数、章時刻等を検査し、`publication.json`と納品先の`投稿資料/`を生成する。タイトルと概要欄を読み、実内容と照合する。再prepareしたらパッケージが変わるので、古い投稿の完了を自動継承しない。

既定の期待種別は3種。利用者が明示的に一部のみ依頼した場合、planへ次の`authorized_scope`を保存できる。

```json
{
  "authorized_scope": {
    "kinds": ["short"],
    "authority": "user",
    "evidence": "review/requested-scope.json"
  }
}
```

根拠JSONは実`vod_id`、`authorized_by:"user"`、`scope:"publication_kinds"`、同じ`kinds`、直接の指示を`statement`に保存する。作りやすさで範囲を減らさない。

内容を検討してShorts/Topicsの適切な候補が0件の場合のみ、`omitted_kinds`へ`{"kind":"short","reason":"具体的な不採用理由","evidence":"review/no-short-candidates.json"}`等を保存できる。根拠には実`vod_id`と`kind`、調べた候補と完結性の判断を残す。未制作・ツール不足・処理待ちを0件判断にせず、Digestの省略にも流用しない。

## 投稿台帳

実ブラウザ操作は[upload.md](upload.md)を読む。`begin`は媒体・QA・設定を再照合し、チャンネルとVODとasset IDをキーに送信意図を記録する。まだ送信はしない。同じチャンネルへ同じSHAを別IDで送る操作も検出する。

```powershell
& $clipPython -X utf8 twitchcut.py begin --job $clipJob --asset $assetId --channel $channelId --evidence $channelEvidence
& $clipPython -X utf8 twitchcut.py record --job $clipJob --asset $assetId --result $receipt
& $clipPython -X utf8 twitchcut.py status --job $clipJob
```

`channelEvidence`は現在のStudioで読んだ恒久チャンネルID、実確認時刻、スクリーンショット等の参照を残す。ファイルの存在だけで実チャンネルを確認済みにしない。

結果JSONは実際に読み戻した値を書く。動画IDが判明したら、処理中でも保存して`record`する。以下の成功値を処理中へそのまま転記しない。

```json
{
  "video_id": "実際の11文字ID",
  "channel_id": "実際の恒久チャンネルID",
  "privacy_status": "private",
  "publish_at": null,
  "processing_status": "succeeded",
  "title": "実際に読み戻したタイトル",
  "description": "実際に読み戻した概要欄全文",
  "thumbnail_verified": true,
  "checks_reviewed": true,
  "playback_reviewed": true,
  "observed_at": "実確認時刻のISO8601",
  "package_signature": "今回照合したassetのpackage_signature",
  "evidence": "review/asset-studio-readback.json"
}
```

例として`processing_status:"processing"`、`playback_reviewed:false`の実状態は`needs_review`に留まる。`verified_private`は必要な実確認が揃った場合だけ。`status.complete`は公開投稿の完了状態で、ローカル制作だけの依頼の完了判定と混同しない。

通信断や途中停止時は台帳と実Studioで照合する。IDが分かれば同じ動画の検査・メタデータ修正を続ける。新規IDに変えたり台帳を消したりして再送拒否を回避しない。未作成を実証できた場合のみ`workflow.py absent --help`に従って根拠付きで解除する。判断不能なら結果不明として止め、独立した作業を続ける。
