# discord-dumper

Discord のクライアント API (ユーザートークン) を使って、サーバーのチャンネル・スレッド・DM をダンプするツール。Python 3.10+ の標準ライブラリのみで動きます。

> ⚠️ ユーザートークンでの自動アクセス (セルフボット) は Discord の利用規約違反で、アカウント停止のリスクがあります。自己責任で、自分がアクセスできるデータのバックアップ用途に留めてください。トークンはパスワードと同等なので絶対に共有しないこと。

## トークンの取得

ブラウザ版 Discord で DevTools → Network タブを開き、任意の `/api/v9/...` リクエストの `Authorization` ヘッダーの値をコピー。

```sh
export DISCORD_TOKEN='xxxxx'
```

## GUI

```sh
python3 discord_dumper.py gui            # ブラウザが開く (DISCORD_TOKEN があれば自動ログイン)
python3 discord_dumper.py gui --port 8765 --no-browser
```

- 左でサーバー / DM を選び、チャンネルにチェックを入れて「ダンプ開始」。複数のサーバーにまたがって選択できます。
- カテゴリのチェックで配下のチャンネルを一括選択できます。閲覧権限のないチャンネルは 🔒 でグレーアウトします。
- 保存済みのチャンネルには件数と最終更新日時が表示されます。選択内容とオプションはブラウザに記憶されるので、次回は「ダンプ開始」を押すだけで差分更新できます。
- 「保存済みをすべて更新」を押すと、選択内容に関係なく、出力先に保存済みのチャンネル / スレッド / DM をまとめて差分更新します (`dump saved` と同じ)。下のオプション (添付ファイル・再取得日数など) も効きます。
- 実行中はログが表示され、途中で中止できます (次回は続きから取得)。「結果を開く」でダンプした `index.html` をローカルファイル (file://) として既定のブラウザで開きます (開いたパスも表示されます)。
- サーバーは 127.0.0.1 でだけ待ち受け、起動時にターミナルに表示される鍵付き URL から開いたブラウザだけが操作できます。トークンはプロセスのメモリにだけ保持され、ファイルには保存されません。

## コマンドライン

```sh
python3 discord_dumper.py guilds                 # サーバー一覧
python3 discord_dumper.py channels <GUILD_ID>    # チャンネル一覧
python3 discord_dumper.py dms                    # DM 一覧

python3 discord_dumper.py dump guild <GUILD_ID>            # サーバー全体 (スレッド含む)
python3 discord_dumper.py dump guild <GUILD_ID> --only <CH_ID> <CH_ID>
python3 discord_dumper.py dump channel <CHANNEL_ID>        # 単一チャンネル / スレッド / DM
python3 discord_dumper.py dump dms                         # 全 DM
python3 discord_dumper.py dump all                         # 全サーバー + 全 DM
python3 discord_dumper.py dump saved                       # 保存済みのチャンネル / スレッド / DM だけを更新

python3 discord_dumper.py render                           # 保存済みデータから HTML / txt を再生成 (トークン不要)
```

ダンプ後は `dump/index.html` をブラウザで開くと、サーバー / チャンネル / スレッド / DM の一覧から各ページに移動できます。

オプション (`dump`):

| オプション | 説明 |
|---|---|
| `-o, --out DIR` | 出力先 (既定 `./dump`) |
| `--attachments` | 添付ファイルもダウンロード |
| `--no-threads` | スレッドを取得しない |
| `--after DATE` / `--before DATE` | 期間指定 (例 `2025-01-01`, `2025-01-01T09:00+09:00`) |
| `--format html,txt` | 出力形式 (既定は両方。`render` でも使える) |
| `--refresh-days N` | 直近 N 日分を取り直し、編集・削除・リアクションの変化を反映 |
| `--full` | 全期間を取り直して編集・削除を反映 (時間がかかる) |

共通: `--token`, `--delay 秒` (既定 0.3), `--bot` (Bot トークンを使う場合)

## 出力

```
dump/
  guilds/<サーバー名>_<id>/
    guild.json, channels.json
    <チャンネル名>_<id>/
      channel.json
      messages.jsonl      # API の生データ (古い順, 1 行 1 メッセージ)
      messages.html       # Discord 風の閲覧用ページ (検索付き)
      messages.txt        # 読みやすいテキスト
      state.json          # 差分更新用の状態 (最終メッセージ ID など)
      attachments/
      threads/<スレッド名>_<id>/...
  dms/<相手の名前>_<id>/...
  index.html              # 全体の目次
```

## 差分更新

同じコマンドを再実行すると差分だけを取得します。どのコマンドで取ったか覚えていなくても、`dump saved` で出力先にある保存済みのチャンネル / スレッド / DM をまとめて更新できます (新しいサーバーやチャンネルは増やしませんが、保存済みチャンネルにできた新しいスレッドは取得します)。

- 前回保存した最後のメッセージより新しいものだけを取得します。チャンネル / スレッドの `last_message_id` が前回から変わっていなければ、メッセージ取得の API を呼ばずにスキップします。
- 中断しても、次回は続きから再開します。
- `--refresh-days N` / `--full` を付けると、指定範囲の保存済みメッセージを取り直して比較します。
  - 編集されたメッセージは最新版に置き換わり、以前の内容は `_edits` に残ります (HTML では「編集履歴」)。
  - 削除されたメッセージは消さずに `_deleted: true` を付けて残します (HTML では赤く表示)。
- チャンネル名やサーバー名が変わっても、ID で既存のディレクトリを探して使い続けます。
- `messages.jsonl` に追加している独自キー (`_files`, `_edits`, `_deleted`, `_deleted_at`) は `_` で始まります。

例: 毎日 cron で `dump saved --refresh-days 3 --attachments` を回すと、新着と直近 3 日の編集・削除を追従できます。

## その他
- HTML のアバター・カスタム絵文字・スタンプは Discord の CDN を参照します。添付ファイルは `--attachments` を付けるとローカル保存したものを参照します (付けない場合の CDN URL は一定期間で失効します)。
- スレッドはアクティブ・アーカイブ済みの両方を `threads/search` で列挙し、加えてメッセージから参照されているスレッドも取得します。フォーラムの投稿もスレッドとして保存されます。
- 閲覧権限のないチャンネルはスキップします。レート制限 (429) は自動で待機します。
