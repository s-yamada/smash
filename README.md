# smash

コマンドラインで動作するメール閲覧ツール。IMAP/POP3 アカウントの受信メールを一覧・詳細表示する。

## Features

- 複数アカウント（IMAP/POP3）の受信箱を一覧でまとめて表示
- サーバー上のメールを変更しない（既読にしない・削除しない）
- メールをローカルに保存しない（毎回サーバーから取得）
- Python 標準ライブラリのみの単一ファイル。設定は JSON ファイル1つ

## Requirements

- Python 3.9 以上（標準ライブラリのみ使用、追加インストール不要）
- macOS / Linux のターミナル

## Installation

```bash
git clone https://github.com/s-yamada/smash.git
cd smash
./install.sh
```

`~/.local/bin/smash` が作成される（`~/.local/bin` に PATH が通っていることが前提）。

アカウントは `~/.smash/accounts.json` に登録する。

```json
[
  { "type": "imap", "host": "imap.example.com", "user": "user@example.com", "pass": "password" }
]
```

## Usage

```bash
smash                    # 全アカウントのメールを一覧表示し、インタラクティブモードへ
smash user@example.com   # 特定アカウントのみ
smash -l                 # 一覧を表示して終了
```

一覧表示後は、番号を入力するとそのメールを表示し、`q` で終了する。

詳しくは [docs/usage.md](docs/usage.md)

## License

[LICENSE](LICENSE) を参照
