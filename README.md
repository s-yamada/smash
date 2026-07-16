# smash

コマンドラインで動作するメール閲覧ツール。`~/.smash/accounts.json` に登録されたメールアカウントから受信メールを取得し、一覧・詳細表示できる。全アカウントのメールは `Date` の新しい順（降順）で統合表示される。

## インストール

```bash
main/install.sh
```

`smash.py` の実体を `~/.local/share/smash/smash.py` にコピーし、起動用シンボリックリンク `~/.local/bin/smash` を作成する（`~/.local/bin` にPATHが通っていることが前提）。コードを更新したら再実行する。

## 使い方

```
smash [account or email]
```

| オプション | 説明 |
|---|---|
| `target`（位置引数） | アカウント名またはメールアドレス。省略時は全アカウントが対象。 |
| `-l`, `--list` | 登録済みアカウント名を一覧表示して終了。 |
| `-v`, `--verbose` / `--info` | INFOレベルのログを有効化。 |
| `--debug` | DEBUGレベルのログを有効化。 |

### 実行例

```bash
# 全アカウントのメールを取得・表示
smash

# 特定アカウントのみ
smash user@example.com

# 登録アカウント一覧を表示
smash -l
```

## インタラクティブ操作

メール一覧表示後、以下の入力を受け付ける：

| 入力 | 動作 |
|---|---|
| 数字 | 該当番号のメール詳細を表示 |
| Enter（空白） | 次のメールの詳細を表示 |
| `l` | 一覧を再表示 |
| `q` / `quit` / `exit` | 終了 |

### マルチパートメールの表示

パートが複数あるメール（例: `multipart/alternative`）を開くと、利用可能なパートが番号付きで一覧表示される。

```
[マルチパート: 2 パート]
  1. text/plain
  2. text/html

表示するパートを選んでください (1-2, Enter でデフォルト):
```

- Enter のみ → `text/plain` があれば優先、なければ先頭パートを表示
- `text/plain` パートは URL をハイパーリンク化（OSC 8 対応ターミナルのみ有効）
- `text/html` などその他のパートはデコード後そのまま出力（タグ含む）

## accounts.json の構造

`~/.smash/accounts.json` にアカウント情報を配列形式で記述する（`~/.smash` は `chmod 700`、ファイルは `chmod 600` を推奨）。

### IMAP の場合

```json
{
  "name": "表示名（省略可）",
  "type": "imap",
  "host": "imap.example.com",
  "port": 993,
  "user": "user@example.com",
  "pass": "password"
}
```

### POP3 の場合

```json
{
  "name": "表示名（省略可）",
  "type": "pop3",
  "host": "pop.example.com",
  "port": 995,
  "user": "user@example.com",
  "pass": "password"
}
```

- `port` は省略可。IMAP のデフォルトは `993`、POP3 のデフォルトは `995`。
- `name` を省略した場合は `user` の値が表示名として使われる。
- 接続は常に SSL/TLS。

## 主な内部構造

| モジュール／関数 | 役割 |
|---|---|
| `MailItem` | メール1通分のデータクラス（seq, from, date, subject, raw_message など） |
| `load_accounts()` | `accounts.json` を読み込む |
| `filter_accounts()` | `target` 引数でアカウントを絞り込む |
| `fetch_mail_items()` | type に応じて IMAP / POP3 からメールを取得 |
| `print_list()` | メール一覧をターミナル幅に合わせて表示 |
| `_get_leaf_parts()` | 添付ファイル以外のリーフパートを `(content_type, decoded_text)` のリストで返す |
| `print_detail()` | メール詳細（ヘッダ＋本文）を表示。マルチパート時はパート選択プロンプトを表示 |
| `interact()` | インタラクティブループ |
| `_linkify_urls()` | 本文中の URL を OSC 8 ハイパーリンクに変換（対応ターミナルのみ有効） |
| `_display_width()` | 全角・半角を考慮した表示幅計算 |

## 依存ライブラリ

標準ライブラリのみ使用（`imaplib`, `poplib`, `email` など）。追加インストール不要。
