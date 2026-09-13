# 財務パイプライン（実験的機能）

[README](../README.md) · [設定](configuration.md) · [アーキテクチャ](architecture.md)

`jqqlib.fundamentals_pipeline` は実験的な財務データ ETL です。株価の日次 CLI とは
独立して実行し、株価 Parquet の銘柄集合と Qlib 営業日カレンダーを読み取ります。
株価 Parquet や Qlib bin を書き換えず、pandas で利用する財務パネルを生成します。

## 設定と実行

先に株価パイプラインで Parquet と Qlib カレンダーを構築してください。資格情報は
[株価と同じ解決順序](configuration.md#api-keyはどこに設定する)です。既存の YAML に
必要な項目を追加します（全キーの既定値は [設定一覧](configuration.md#全設定キー)）。

```yaml
fundamentals:
  parquet_dir: ./data/fundamentals
  calendar_path: ./data/qlib_jp/calendars/day.txt
  retries: 3
  retry_base_delay_sec: 1.0
  retry_max_delay_sec: 8.0
  retry_jitter_sec: 0.3
  request_interval_sec: 1.1
```

```bash
python -m jqqlib.fundamentals_pipeline run-fundamentals-etl --config config.yaml --max-new-codes 20
```

`--max-new-codes` は未キャッシュの銘柄を今回何件取得するかの上限です。省略すると
上限を設けません。取得済みレスポンスを `fundamentals_raw_cache.parquet` に保存するため、
同じコマンドを繰り返して初回取得を分割できます。各回はキャッシュ済み範囲から再構築するので、
全銘柄の取得が終わるまでは部分的なスナップショットです。既存キャッシュを使う動作を、
上流の訂正や新しい開示を自動的に再取得する保証と解釈しないでください。

出力は `fundamentals.parquet_dir`（省略時 `storage.parquet_dir`）配下です。

```text
data/fundamentals/
  fundamentals_raw_cache.parquet
  fundamentals_asof.parquet
  fundamentals_pit_audit.json
```

PIT 監査に失敗した場合は、パネルと監査結果の保存へは進みません。

## PIT（point-in-time）の意味

各営業日時点で利用可能だった開示だけを参照するため、開示日 `DiscDate` より
**厳密に後の最初の営業日**から値を有効にします。`DiscTime` は使用せず、
開示当日の利用を避ける保守的な規則です。次の開示まで前方補完し、最初の開示以前の
行は除外します。同じ有効日に複数の開示があれば最新の開示日を残します。

生成するパネルのインデックスは `datetime` / `instrument` です。
`_src_DiscDate` は出所を検証する監査列であり、学習特徴量に含めないでください。
PIT 監査は開示日と利用日の順序を検証します。上流 API が過去レスポンスを訂正した際の
旧版まで復元する保証はありません。研究ではキャッシュ取得時点と対象範囲も記録してください。
