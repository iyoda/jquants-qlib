# 運用ガイド

[README](../README.md) · [設定](configuration.md) · [アーキテクチャ](architecture.md) · [財務](fundamentals.md)

## Lightプラン前提の設計

**初回は bulk API で履歴CSVを作って一括投入し、2回目以降をAPI差分取得**するのが効率的です。

- 初回: `download-csv` + `bootstrap-csv`（bulk API -> CSV -> Parquet -> Qlib）
- 日次運用: `run-all`（J-Quants API -> Parquet -> Qlib）

## このパイプラインで使うデータ

このリポジトリでは、J-Quants V2 日足（`get_eq_bars_daily`、未対応クライアントは `get_prices_daily_quotes` にフォールバック）で取得できるデータをQlib向けに使用します。

- 利用列（入力）: `Code, Date, Open, High, Low, Close, Volume, TurnoverValue`。調整係数は V2 の `AdjFactor` または bulk CSV の `AdjustmentFactor`。権利落種類 `ExRT` はソースにあるときだけ使います
- Parquet 列: `symbol, date, open, high, low, close, volume, amount, adjustment_factor`（任意で `ex_rights_type`）。価格・出来高は raw
- Qlib向け出力列（既定 `qlib.adjustment: vendor_factor`）: `symbol, date, open, high, low, close, volume, amount, factor`。`none` のときは六フィールドだけで `factor` は出しません

分割・併合・ライツイシューの計算と `$close / $factor` が raw と一致する関係は
[株価調整](jquants-price-adjustment.md)を参照してください。

> 注: Lightプランで利用可能なAPI種別・上限は契約や仕様更新で変わるため、最終的にはJPX/J-Quants公式ドキュメントで確認してください。

従来の運用で用いたプラン別レートリミットの参考値（1分あたり。現行の契約・公式仕様を優先）:

- Free: 5 req/min
- Light: 60 req/min
- Standard: 120 req/min
- Premium: 500 req/min

最適取得方法（推奨）:

1. **初回構築**: bulk API で CSV を作成して `bootstrap-csv` に流す
2. **日次更新**: API差分取得（`run-all --start 当日 --end 当日`）
3. **安定化**: API取得は日付チャンク分割（`etl.chunk_days`, 既定30日）
4. **コスト最適化**: `universe.codes` で対象銘柄を絞る
5. **障害耐性**: `etl.retries` + exponential backoff で429/5xx/接続失敗を再試行

## 実行

コマンドはすべて console script `jqqlib`（`python -m jqqlib` でも同じ）です。
`jqqlib --version` でインストール済みバージョンを確認できます。設定ファイルは
`jqqlib init-config config.yaml`（clone 内なら `cp config.example.yaml config.yaml`）で
作成します（[設定](configuration.md)）。

### 0) 初回一括投入（推奨）

J-Quants等から取得した日足CSV（列: `Code,Date,Open,High,Low,Close,Volume[,TurnoverValue]`）を使って初期データセットを構築します。

CSV をまだ持っていない場合は、J-Quants API から初回投入用の CSV を作れます。
このコマンドは `/bulk/list` と `/bulk/get` を使って月次の bulk ファイルを取得します。
bulk 履歴の提供範囲は契約プランのローリング窓（Light は約 5 年）に依存するため、
`--start` は自分の契約で取得できる範囲に合わせてください。

```bash
jqqlib download-csv --config config.yaml --start 2021-03-01 --end 2021-12-31 --output ./downloads/first_full_daily_quotes.csv
```

`download-csv` は CSV の横に `*.manifest.json` を出し、bulk API から作った入力CSVの取得期間を記録します。

```bash
jqqlib bootstrap-csv --config config.yaml --csv ./downloads/first_full_daily_quotes.csv
```

`bootstrap-csv` は Qlib dataset と同じディレクトリに `dataset_manifest.json` と `dataset_quality_report.json` を出します。

### 1) ETL のみ（差分取得）

```bash
jqqlib run-etl --config config.yaml --start 2020-01-01 --end 2020-12-31
```

`config.yaml` の `etl.chunk_days` を調整すると、期間取得時のAPI呼び出しを分割できます。

リトライ方針（現在実装）:

- 対象: `TimeoutError`, `ConnectionError`, `OSError`, HTTP `429/500/502/503/504`
- 方式: 指数バックオフ + ジッター
- 設定: `etl.retries`, `etl.retry_base_delay_sec`, `etl.retry_max_delay_sec`, `etl.retry_jitter_sec`
- 非リトライ: 4xx の恒久エラー（例: 400/401/403/404）

実運用で追加で考慮すると良い点（本実装で対応済み）:

- `etl.merge_mode: upsert`（既定）で、差分取得時に既存Parquetを `(symbol, date)` キーで上書きマージ
- Parquet出力は一時ファイル経由で原子的に置換（途中失敗時の破損リスク低減）

おすすめ運用:

1. 初回は `download-csv` と `bootstrap-csv` で履歴を構築
2. 毎日は `run-all --start 当日 --end 当日`
3. 週次で `validate` を走らせてQlib入力品質を監視

### 2) Parquet -> Qlib 変換のみ

```bash
jqqlib convert --config config.yaml
```

変換後は `storage.qlib_dir` 配下に `dataset_manifest.json` と `dataset_quality_report.json` が生成されます。
既定の `qlib.adjustment: vendor_factor` では dump 自身が書き込み前に次節と同じ
readiness 検査を実行します（不合格は `error: Parquet is not Qlib-ready: ...` と exit 1）。

### 2.5) Qlib 準備状況の確認（`validate` と dump 時検査）

`microsoft/qlib` の `dump_bin` 前提に合わせ、Parquet が必要条件を満たすか確認します。
既定の `vendor_factor` では `convert` / `run-all` / `run-daily` / `bootstrap-csv` の
dump が `validate_qlib_readiness` と同じ検査を書き込み前に実行します。`validate` は
同じ検査をスタンドアロンで走らせるコマンドです。`scripts/run_daily.sh` は `run-daily`
のあとに `validate` を明示ゲートとして実行します。

- 必須列: `symbol,date,open,high,low,close,volume,amount,adjustment_factor`
- `adjustment_factor` が有限かつ 0 より大きいこと（正規化後は null 不可）
- `date` の日付変換可否
- `symbol` 空文字の有無
- 数値列 (`open/high/low/close/volume/amount`) の妥当性
- `(symbol, date)` 重複

```bash
jqqlib validate --config config.yaml
```

### 2.6) Qlib 読み込み確認

生成済みの Qlib dataset を実際に読めるか、指定日付の OHLCVA と `$factor` を取得して確認できます。
`scripts/check_qlib.py` は wheel に含まれない clone 専用ツールなので、リポジトリのルートで実行します。

```bash
python scripts/check_qlib.py --provider-uri data/qlib_jp --date 2026-03-10 --limit 5
```

### 3) 一気通し実行（差分取得 + 変換）

```bash
jqqlib run-all --config config.yaml --start 2020-01-01 --end 2020-12-31
```

`run-all` も変換後に `dataset_manifest.json` と `dataset_quality_report.json` を更新し、今回の API 取得期間と生成済み dataset のカバレッジを記録します。

生成物を運用リポジトリへ反映する場合は、続けて `publish-paths` で対象パスを確認します。

```bash
jqqlib run-all --config config.yaml --start 2026-01-01 --end 2026-01-31
jqqlib publish-paths --config config.yaml
```

`publish-paths` は日次 wrapper が stage できる artifact のパスを出力します。内容は
`storage.parquet_dir/daily_quotes.parquet`、`storage.qlib_dir/dataset_manifest.json`、
`storage.qlib_dir/dataset_quality_report.json` の 3 パスで、設定に他の節が書かれていても
変わりません。このうち Git 管理するのは manifest と quality report だけです（次節の
`.gitignore` 例を参照）。

### 3.5) データの管理方針

**取得した市場データは Git 管理しません。** このリポジトリが保持するのはコードと
contract だけで、`data/`（Parquet・Qlib feature bin・calendars・instruments）と
`downloads/` は `.gitignore` 済みです。J-Quants の利用規約上、取得データを第三者へ
再配布することはできません。ご自身の運用でも公開リポジトリにデータを push しないで
ください。

`dataset_manifest.json` / `dataset_quality_report.json` は数 KB の JSON で価格データを
含まないため、自分の（private な）運用リポジトリで履歴管理することができます。
`publish-check` は `publish_check.git_ref`（既定 `origin/main`）上の `lineage.parquet_sha256` と
比較して内容が変わったときだけ commit すべきかを判定します（変更なしは exit 3）。その運用リポジトリの
`.gitignore` は次のように、この 2 つの JSON だけを残してデータ本体を除外してください。

```gitignore
data/*
!data/qlib_jp/
data/qlib_jp/**
!data/qlib_jp/dataset_manifest.json
!data/qlib_jp/dataset_quality_report.json
```

`publish-paths` が出力する 3 パスのうち Parquet は市場データ本体なので、stage するのは
残り 2 つの JSON だけです。ignore 済みのパスを明示的に `git add` すると git は exit 1 に
なるため、出力を絞ってから渡してください。

```bash
jqqlib publish-paths --config config.yaml | grep -E 'dataset_(manifest|quality_report)\.json$' | xargs git add
```

データの復旧経路は「J-Quants API から再取得」です。`run-all` を該当期間で再実行すると、
同じ入力から Parquet と Qlib dataset を決定的に再生成します（`lineage.parquet_sha256` で
同一性を検証できます）。障害別の手順は[障害復旧](#障害復旧)にまとめています。

### 4) Atomic publish

既存 provider を直接上書きせず、build 済み dataset へ symlink を原子的に切り替えたい場合は `publish` を使います。

```bash
jqqlib publish --build-provider-uri ./data/builds/<dataset_id>/qlib_jp --provider-uri ./data/qlib_jp
```

`publish` は `--provider-uri` が存在しない場合、または既存 symlink の場合だけ切り替えます。実ディレクトリを指している既存 dataset は削除しません。

`publish` は build 済みディレクトリと公開先の 2 パスだけを受け取り、`--config` は使いません。
build ディレクトリが無い場合や公開先が実ディレクトリの場合は `error: <message>` と exit 2 で
止まります。

### 5) Dataset artifacts

`storage.qlib_dir` に `dataset_manifest.json` と `dataset_quality_report.json` を出します。

`dataset_manifest.json` の主な項目:

- `schema_version`: manifest schema version（現行は `2`）
- `dataset_id` / `build_id`: dataset lineage identifier
- `generated_at`: manifest 生成時刻（UTC）
- `quality_status`: dataset 側 quality gate の結果（`pass` / `warning` / `fail`）
- `provider_uri`: Qlib provider URI
- `parquet_path`: 変換元 Parquet
- `dataset.date_start` / `dataset.date_end`: Parquet 上のデータ期間
- `dataset.rows` / `dataset.instruments`: 行数と銘柄数
- `qlib.calendar_start` / `qlib.calendar_end` / `qlib.calendar_days`: Qlib calendar 範囲
- `qlib.instrument_count`: Qlib instruments 件数
- `field_schema`: Qlib に出す field の role / dtype / nullable / adjustment。既定では `factor` を含み（role `adjustment_factor`、dtype `float32`、nullable false、adjustment `cumulative_vendor_factor`。bin は `<f>` で他の価格フィールドと同じ dtype 文字列）、価格の adjustment は `split_reverse_split_rights_issue`、出来高は `split_reverse_split`、売買代金は `none`
- `dataset.fields`: 出した field 名。`factor` があるときそれを含む
- `adjustment_policy`: producer が適用した価格・出来高・売買代金 adjustment 方針。既定は `price_adjustment: vendor_cumulative_factor`、`volume_adjustment: vendor_cumulative_factor_excluding_rights_issue`、`amount_adjustment: none`、`source: J-Quants AdjFactor`（既存キーは維持し `source` を追加）。`qlib.adjustment: none` のときは `factor` を出さず、方針文字列は raw のまま
- `lineage.parquet_sha256`: 変換元 Parquet の SHA-256
- `lineage.manifest_sha256`: manifest payload の SHA-256（`manifest_sha256` field 自身は除外）
- `lineage.producer_git_commit`: producer repo の commit
- `source.kind`: `jquants_api`, `jquants_bulk`, `csv`, `parquet`
- `source.requested_start` / `source.requested_end`: 入力取得時の要求期間（分かる場合）

`dataset_quality_report.json` は quality gate 用の別 artifact です。

- `status`: `pass` / `warning` / `fail`
- `highest_severity`: report 内で最も高い severity
- `severity_counts`: `fail` / `warning` / `info` finding 数
- `validation_errors`: `fail` finding の operator-readable summary
- `findings`: severity 付きの構造化 finding
- `checks.duplicate_symbol_date_rows`: `(symbol, date)` 重複数
- `checks.invalid_date_rows`: 日付変換不能行数
- `checks.empty_symbol_rows`: 空 symbol 行数
- `checks.non_numeric_counts`: field ごとの非数値数
- `checks.null_counts`: field ごとの null 数
- `checks.ohlcv_null_classification`: OHLCV null 行を all-null / partial-null に分類
- `checks.rows_by_date_min` / `checks.rows_by_date_max`: 日別行数の最小/最大
- `checks.dumped_factor_invalid_rows` / `checks.dumped_factor_latest_not_one_symbols`: dump した `factor` が有限かつ 0 より大きいか、各銘柄の最新日で `1.0` か（`qlib.adjustment: none` のときは null）。不合格時の finding 名は `dumped_factor`

OHLCV がすべて null の行は `warning` として扱います。これは missing bar / source gap として研究側が明示的に受け入れる余地を残すためです。一方、OHLCV の一部だけが null の行、非数値、重複、価格順序不整合、負の volume / amount は `fail` です。

JSON contract は wheel に同梱しており、インストール済みパッケージからは
`jqqlib.contracts.load_schema("dataset_manifest")` /
`jqqlib.contracts.load_schema("dataset_quality_report")` で読み込みます。
リポジトリを直接読む場合のソース配置は `src/jqqlib/contracts/dataset_manifest.schema.json` と
`src/jqqlib/contracts/dataset_quality_report.schema.json` です。consumer は producer の
Python module ではなく、これらの JSON artifact を読む前提です。

## 日次運用

日次更新は `run-daily` を使います。既定では実行日を基準に直近営業日から試し、祝日や API 未反映でデータが空の場合は最大 10 営業日前までさかのぼります。取得可能な最新営業日が見つかると、既存 Parquet の最大日付の翌日からその最新営業日までを range で取り込みます。成功時は `run-all` と同じく Parquet を upsert し、Qlib dataset、`dataset_manifest.json`、`dataset_quality_report.json` を更新します。

実行を取りこぼした日があっても、次回実行時に直近営業日から lookback し未取得期間を range で取り込むため、短期の取りこぼしは日次ジョブ側で吸収します。ただし吸収できるのは概ね lookback 窓（既定で約 10 営業日 ≒ 2 週間）までです。それを超える長期停止後は、`--max-lookback-business-days` を大きくした手動 `run-daily` で穴埋めしてください。

catch-up は対象営業日を含む `[start, target]` を 1 回の range fetch で取得します（`write_parquet` は tmp+rename の atomic 書き込みのため、途中失敗時は何も書かれず、次回実行で再取得されます）。`write_parquet` は書き込み前に `symbol, date` で安定ソートするため、同一内容なら fetch 窓や順序に依らず同一バイト列・同一 `parquet_sha256` になります（pandas/pyarrow のアップグレード時のみ、内容が同じでも 1 回だけ余分に publish され得ます）。

```bash
jqqlib run-daily --config config.yaml
jqqlib run-daily --config config.yaml --as-of 2026-06-04 --max-lookback-business-days 10
```

`--as-of today`（既定）はホストのローカル日付ではなく、Asia/Tokyo の当日です。

`audit` は publish 前の自己監査で、直近 `etl.audit_window_business_days` 営業日に
欠損が無いかを取引所カレンダー（`exchange_calendars` の XTKS）で確認します。
欠損があれば非 0 で終了し、カレンダーが使えないときは rc=2 で「監査していない」ことを区別します。

```bash
jqqlib validate --config config.yaml
jqqlib audit --config config.yaml
```

### ラッパースクリプト

`scripts/run_daily.sh` は `run-daily` → `validate` → `audit` を順に実行する最小のラッパーです
（wheel には含まれない clone 専用ツールです。pip インストール環境では同じ 3 コマンドを
自前の cron / タイマーから順に呼んでください）。`run-daily` の dump が readiness 検査を
しても、ラッパーは続けて `validate` を明示ゲートとして走らせます。
`JQUANTS_API_KEY` を環境に与えて cron / launchd / systemd timer から呼び出してください
（`launchd` は shell の初期化ファイルを読まないため、macOS では Keychain 登録が便利です。
[資格情報の設定](configuration.md#api-keyはどこに設定する)を参照）。追加引数は `run-daily` と `audit` にそのまま渡ります。
`run-daily` は `--window-business-days` を受け付けるが無視します（help には出ない隠し引数）。
`scripts/run_daily.sh` が同じ引数リストを `run-daily` と `audit` の両方に渡すためです。

ラッパーが参照する環境変数（設定キーの表は [設定](configuration.md#環境変数)）:

| 変数 | 既定 | 意味 |
| --- | --- | --- |
| `JQQLIB_CONFIG` | `config.yaml` | `--config` に渡す設定ファイル |
| `JQQLIB_PYTHON` | 未設定なら checkout の `.venv/bin/jqqlib`、それも無ければ PATH の `jqqlib` | `python -m jqqlib` に使うインタプリタ |
| `JQQLIB_RUN_STARTED_AT` | 未設定ならラッパーが最初のコマンドの前に UTC ISO 時刻を export | 監査 sidecar の実行開始識別子 |

```bash
# 手動実行
./scripts/run_daily.sh
# cron の例: 平日 19:30（ホストのタイムゾーンを JST に設定）
30 19 * * 1-5 cd /path/to/jquants-qlib && ./scripts/run_daily.sh >> ~/jqqlib-daily.log 2>&1
```

manifest / quality report を自分の運用リポジトリで履歴管理したい場合は、このラッパーの後段で
`publish-check`（exit 3 なら変更なし）と、`publish-paths` が出力する JSON の `git add` を
追加してください。[データの管理方針](#35-データの管理方針)も参照。

## 監査 sidecar と artifact

`audit` の結果は `storage.audit_dir`（既定 `./data/audit`）配下の
`last_audit.json` に保存します。相対パスはコマンドの作業ディレクトリ基準です。
ラッパーからの実行でも、この設定で保存先を変更できます。

パッケージに同梱した JSON schema は `jqqlib.contracts.load_schema("dataset_manifest")`
または `jqqlib.contracts.load_schema("dataset_quality_report")` で読み込めます。
ソース上の配置は `src/jqqlib/contracts/` です。dataset の `publish` はデータの公開先切替、
パッケージの `release` は配布物の公開です（[リリース手順](releasing.md)）。

## 障害復旧

このパイプラインの復旧経路は原則「J-Quants API から再取得」です。取得データは Git 管理せず、
Parquet と Qlib dataset は同じ入力から決定的に再生成できます
（[データの管理方針](#35-データの管理方針)）。症状別の手順は次のとおりです。

### exit code の見方

| exit | 意味 | 主なコマンド |
| --- | --- | --- |
| 0 | 成功（`publish-check` は「内容が変わった」） | 全コマンド |
| 1 | 検査で問題を検出（`validate` の品質エラー、`audit` の欠損、`publish-check` の manifest 異常）、または Qlib dump が provider subtree を作らなかったなどの dataset 状態の異常 | `validate` / `audit` / `publish-check` ほか取得・変換系コマンド |
| 2 | ユーザー側の誤り（設定ファイルが無い・必須キー不足・値が不正・`init-config` の上書き拒否・`publish` の build ディレクトリ不在／実ディレクトリの置換拒否）。`error: <message>` を stderr に出し traceback は出しません。`run-daily` / `audit` の XTKS カレンダー不可（`... XTKS trading calendar unavailable: ...`、監査未実施）も exit 2 | 全コマンド |
| 3 | `publish-check` で変更なし（失敗ではない） | `publish-check` |

argparse の引数エラー（未知のコマンド、`--config` 欠落など）も exit 2 ですが、こちらは
argparse の usage メッセージが出ます。

### 取りこぼし・欠損期間の再取得

- 短期の取りこぼし（既定の lookback 窓 = 約 10 営業日）は次回の `run-daily` が自動で吸収します。
- それを超える停止後は `run-daily --max-lookback-business-days <N>` を大きくして手動実行するか、
  `run-all --start <開始日> --end <終了日>` で該当期間を再取得してください。
  `etl.merge_mode: upsert`（既定）のため、同じ `(symbol, date)` は上書きマージされ、二重登録にはなりません。
- `audit` が欠損日を報告した（exit 1）場合も同じく、報告された日付を含む期間を `run-all` で再取得し、
  `validate` → `audit` を再実行して exit 0 になることを確認してください。
- Parquet が破損・消失した場合は、bulk CSV から `bootstrap-csv` で再構築し、その後の期間を `run-all` で
  埋めます（[初回一括投入](#0-初回一括投入推奨)）。`write_parquet` は tmp+rename の atomic 書き込みのため、
  途中失敗で中途半端な Parquet が残ることはありません。

### publish が失敗したとき

- `publish` は `--provider-uri` が存在しないか既存 symlink の場合だけ切り替え、実ディレクトリは
  削除しません（`Refusing to replace non-symlink provider path`、exit 2）。既存 dataset を
  退避してから再実行してください。
- `Build provider directory not found` は `--build-provider-uri` の指す build ディレクトリが
  無い場合です（exit 2）。先に `convert` などで build を作ってから再実行します。
- symlink の張り替えは同一ディレクトリ内の tmp + rename で行うため、途中で失敗しても公開中の
  provider は元のままです。ファイルシステムが symlink の原子的置換に対応していない場合は
  `publish` を使わず、`storage.qlib_dir` を直接更新する運用にしてください。
- 運用リポジトリへの manifest 反映で `publish-check` が exit 3 なら「変更なし」であり失敗ではありません。
  exit 1（`publish-check error: ...`）は manifest / quality report が読めない、`lineage.parquet_sha256`
  が無い、quality status が `fail` などの状態なので、原因を解消し `convert` で再生成してから再実行します。

### 監査 sidecar（`last_audit.json`）が無い・古いとき

- `audit` は `storage.audit_dir`（既定 `./data/audit`、作業ディレクトリ基準）へ
  `last_audit.json` を tmp+rename で書きます。書けなかった場合は `Warning: could not write the audit
  sidecar` を出しますが、監査結果自体（exit code）には影響しません。監視側では sidecar の欠落を
  `audit: not_run` として扱ってください。
- 相対パスの `audit_dir` はコマンドを実行したディレクトリ基準です。sidecar が見当たらない場合は、
  ラッパーや cron の作業ディレクトリと `config.yaml` の `storage.audit_dir` を確認してください。
- `run_started_at` は `JQQLIB_RUN_STARTED_AT` 環境変数から記録され、古い sidecar を最新の実行結果と
  取り違えないための識別子です。監視側は自分の実行開始時刻と一致する sidecar だけを採用してください。
- sidecar を作り直したいときは `audit --config config.yaml` を再実行するだけで上書きされます。

### 取引所カレンダーが使えないとき（rc=2）

- `run-daily` と `audit` は XTKS の営業日判定に `exchange_calendars` を使い、使えないときは
  推測に fall back せず `... XTKS trading calendar unavailable: ...` を出して **exit 2** で止まります。
  exit 1（欠損あり）とは別のコードなので、ラッパーでは「カレンダー障害」として区別してください。
- 原因は主に 2 つです。(1) `exchange_calendars` が import できない → `python -m pip install -e .`
  で依存関係を入れ直します。(2) `XTKS calendar has no session data for <date>` → 指定日が
  `exchange_calendars` の事前計算範囲外です。`--as-of` の日付を確認し、必要ならライブラリを更新してください。
- この状態では監査は「実施していない」だけでデータ欠損は確定していません。カレンダーを復旧してから
  `run-daily` → `validate` → `audit` を再実行します。

## 開発時の検証

テストに J-Quants 認証も `config.yaml` も不要です。unittest を使用します。

```bash
python -m pip install -e ".[dev]"
python -m unittest discover -s tests
jqqlib --help
jqqlib --version
ruff check .
mypy
```

成功時は最後に `OK` と表示されます。資格情報欠如・不正 JSON などの negative path の
意図的な診断メッセージも出ますが、最終結果で判定してください。`git` がない環境では
publish-check の一部が skip されます。CI は Python 3.10 / 3.11 / 3.12 のテストと
lint / type / 配布物の検証を行います。mypy は `src/jqqlib` 全体を検査し、`scripts/` と
`tests/` はまだ型検査の対象外です（[開発手順](../CONTRIBUTING.md)を参照）。
