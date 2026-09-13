# 設定リファレンス

[README](../README.md) · [運用](operations.md) · [アーキテクチャ](architecture.md) · [財務](fundamentals.md)

設定ファイルは [config.example.yaml](../config.example.yaml) を雛形にして作成します。
同じ内容がパッケージにも同梱されているので、pip でインストールした環境では
`jqqlib init-config` で書き出せます（既定の出力先は `./config.yaml`。既存ファイルは
`--force` を付けない限り上書きせず、`error: ...` と exit 2 で止まります）。

```bash
jqqlib init-config config.yaml        # pip / clone どちらでも可
cp config.example.yaml config.yaml    # clone したリポジトリ内なら同じ内容
```

設定ファイルが見つからない、必須キー（`storage.parquet_dir` / `storage.qlib_dir`）が無い、
値の型や範囲が不正といったユーザー側の誤りは、CLI が `error: <message>` を stderr に出して
exit 2 で終了します（traceback は出ません）。

パスは YAML ファイルの置き場所ではなく、コマンド実行時の作業ディレクトリを基準に
解決します。自動実行ではリポジトリへ `cd` するか、絶対パスを指定してください。

### API Keyはどこに設定する？

`config.yaml` に平文のAPIキー/refresh tokenを置くことは**推奨されません**。
資格情報の解決順序（`src/jqqlib/fetch.py` の `create_jquants_client`）は次のとおりです。

1. **環境変数** `JQUANTS_API_KEY`（推奨）
2. **macOS Keychain**（`launchd` 等、shell の `export` を引き継がない非対話実行向け。下記参照）
3. `JQUANTS_REFRESH_TOKEN`（環境変数、互換用）
4. macOS Keychain（refresh token、互換用）

API キー（1・2）は `jquantsapi.ClientV2` を使い、依存関係の `jquants-api-client>=1.7` に
上限はありません（2.6 以降でも動作します）。refresh token（3・4）は旧 v1 クライアント
`jquantsapi.Client` を使いますが、
これは `jquants-api-client` 2.6 で削除されたため、**refresh token 経路を使う場合は
`jquants-api-client<2.6` に pin してください**（例: `pip install "jquants-api-client<2.6"`）。
2.6 以降で refresh token だけが設定されていると、`JQUANTS_API_KEY` を設定するか
`<2.6` に pin するよう促す `error:` で exit 2 になります。新規の運用では API キーを使ってください。

手動実行:

```bash
export JQUANTS_API_KEY="<YOUR_API_KEY>"
```

macOS で非対話・自動実行（`launchd` の日次ジョブなど）を行う場合は **Keychain** に
1回だけ登録します（コマンドにキーを直接埋め込まない。`-w` を値なしで実行すると
対話プロンプトで入力できます）。Keychain の service 名は既定で `jquants-api-key` /
`jquants-refresh-token` で、環境変数 `JQQLIB_KEYCHAIN_API_KEY_SERVICE` /
`JQQLIB_KEYCHAIN_REFRESH_TOKEN_SERVICE` で変更できます。Linux など `security`
コマンドが無い環境では Keychain の参照は黙ってスキップされます。

```bash
security add-generic-password -s jquants-api-key -a "$USER" -w
# 互換: リフレッシュトークンのみで運用する場合
security add-generic-password -s jquants-refresh-token -a "$USER" -w
```

登録済みか（値は読まずに）確認する:

```bash
security find-generic-password -s jquants-api-key >/dev/null && echo present || echo missing
```

`config.yaml` の `jquants.api_key` / `jquants.refresh_token` は既定では**使われません**。
ローカルデバッグ限定の意図的にうるさいエスケープハッチとして、環境変数
`JQQLIB_ALLOW_YAML_CREDENTIALS=1` を明示したときだけ、`config.yaml` の値が
使われ、その都度 stderr に `ALERT` が出力されます。自動実行ではこのフラグに
頼らないでください。

主な項目:

- `jquants.api_key` / `jquants.refresh_token`: 既定では未使用（上記参照）。
  `config.example.yaml` のプレースホルダのままにしておくこと。
- `storage.parquet_dir`: ETL 出力先
- `storage.qlib_dir`: Qlib データセット出力先
- `qlib.adjustment`: Qlib への出し方（既定 `vendor_factor`。raw の六フィールドだけなら `none`）
- `publish_check.git_ref`: `publish-check` が比較する運用リポジトリの git 参照（既定 `origin/main`）
- `universe.codes`: 対象銘柄コード


## 全設定キー

「既定」はキー省略時の実装値です。設定例と異なる場合は併記します。

| キー | 既定 | 意味 |
| --- | --- | --- |
| `jquants.api_key` | 未使用 | YAML 資格情報を明示許可した場合のみ使用 |
| `jquants.refresh_token` | 未使用 | 同上、互換認証用 |
| `storage.parquet_dir` | 必須（設定例 `./data/parquet`） | 日足 Parquet 保存先 |
| `storage.qlib_dir` | 必須（設定例 `./data/qlib_jp`） | Qlib データセット出力先 |
| `publish_check.git_ref` | `origin/main` | `publish-check` が `lineage.parquet_sha256` を比較する git 参照。運用リポジトリの既定ブランチが `master` なら `origin/master` |
| `qlib.adjustment` | `vendor_factor` | Qlib への出し方。`vendor_factor` はベンダー `AdjFactor` から dump 時に累積して `$factor` と調整済み価格・出来高を出す。`none` は raw の六フィールドだけ（`$factor` なし）。計算は [株価調整](jquants-price-adjustment.md) |
| `storage.audit_dir` | `./data/audit` | `last_audit.json` 保存先 |
| `universe.codes` | `[]` | 対象コード。空なら API 取得時の銘柄絞り込みなし |
| `etl.chunk_days` | `30` | API の日付チャンク幅、1 以上 |
| `etl.merge_mode` | `upsert` | `(symbol,date)` 上書きマージ。`replace` は置換 |
| `etl.retries` | `3` | 一時エラーの再試行回数、0 以上 |
| `etl.retry_base_delay_sec` | `1.0` | 指数バックオフの初期待機秒 |
| `etl.retry_max_delay_sec` | `8.0` | 待機秒の上限、初期待機以上 |
| `etl.retry_jitter_sec` | `0.3` | 待機に加えるジッター秒 |
| `etl.request_interval_sec` | `0.0`（設定例 `1.0`） | リクエスト間隔秒 |
| `etl.audit_window_business_days` | `5` | 監査対象の直近営業日数。日次 lookback 以下にする |
| `fundamentals.parquet_dir` | `storage.parquet_dir` | 実験的な財務データの出力先 |
| `fundamentals.calendar_path` | `storage.qlib_dir/calendars/day.txt` | PIT 展開用の営業日ファイル |
| `fundamentals.retries` | `3` | 財務 API の再試行回数 |
| `fundamentals.retry_base_delay_sec` | `1.0` | 財務 API の初期待機秒 |
| `fundamentals.retry_max_delay_sec` | `8.0` | 財務 API の待機上限秒 |
| `fundamentals.retry_jitter_sec` | `0.3` | 財務 API のジッター秒 |
| `fundamentals.request_interval_sec` | `1.1` | 財務 API のリクエスト間隔秒 |

表に無いキーが YAML に残っていても、エラーにはならず単に無視されます。財務データの
設定例は [実験的な財務パイプライン](fundamentals.md) を参照してください。

## 環境変数

YAML のキーとは別に、資格情報と日次ラッパーは次の環境変数を見ます。

| 変数 | 既定 | 意味 |
| --- | --- | --- |
| `JQUANTS_API_KEY` | （なし） | 推奨の API キー |
| `JQUANTS_REFRESH_TOKEN` | （なし） | 互換用の refresh token |
| `JQQLIB_KEYCHAIN_API_KEY_SERVICE` | `jquants-api-key` | macOS Keychain の API キー service 名 |
| `JQQLIB_KEYCHAIN_REFRESH_TOKEN_SERVICE` | `jquants-refresh-token` | macOS Keychain の refresh token service 名 |
| `JQQLIB_ALLOW_YAML_CREDENTIALS` | （未設定） | `1` のときだけ `config.yaml` の資格情報を使う |
| `JQQLIB_CONFIG` | `config.yaml` | `scripts/run_daily.sh` が `--config` に渡すパス |
| `JQQLIB_PYTHON` | （なし） | ラッパーが `python -m jqqlib` に使うインタプリタ。未設定なら checkout の `.venv`、それも無ければ PATH の `jqqlib` |
| `JQQLIB_RUN_STARTED_AT` | ラッパーが未設定なら UTC ISO 時刻を最初のコマンドの前に export | 監査 sidecar の実行開始識別子 |

日次ラッパーでの使われ方は [運用ガイドのラッパー節](operations.md#ラッパースクリプト) を参照してください。
