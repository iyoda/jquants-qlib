# アーキテクチャ

[README](../README.md) · [設定](configuration.md) · [運用](operations.md) · [財務](fundamentals.md)

CLI は console script `jqqlib`（`python -m jqqlib` と同じ。`__main__.py` が
`pipeline.main` を呼びます）です。パッケージは `src/jqqlib` にあり、`jqqlib.<module>`
として import します。サポートする利用窓口は CLI で、Python API の安定性はまだ保証しません。
`jqqlib --version` は `jqqlib.__version__` を表示し、設定・引数のユーザー側の誤りは
`error: <message>`（stderr）と exit 2 で終了します。

```text
J-Quants API / bulk CSV
        |
      fetch -> normalize -> store (raw OHLCVA Parquet + adjustment_factor)
                              |
                           validate
                              |
                             dump (CumAdj at dump time → Qlib $factor)
                              |
                          manifest + quality report
                              |
                           publish (atomic symlink)

audit: 営業日カレンダーと保存済みデータの欠損検査 -> last_audit.json
```

Parquet は raw のまま保存し、dump 時にベンダー調整係数の累積積（`CumAdj`）を計算して
Qlib の `$factor` と調整済み価格・出来高を出します。配当は調整しません。
`qlib.adjustment: none` なら Qlib も raw の六フィールドです。
計算と権利落ちの扱いは [株価調整](jquants-price-adjustment.md) を参照してください。

## 実装メモ

- `fetch.py`: J-Quants client / API retry / daily API / bulk API
- `normalize.py`: J-Quants応答からQlib入力列への正規化（`AdjFactor` / `AdjustmentFactor` → `adjustment_factor`、あれば `ExRT` → `ex_rights_type`）
- `store.py`: CSV・Parquetの決定論的upsert
- `validate.py`: 日付範囲と Qlib 準備状況のスタンドアロン検査（`adjustment_factor` が有限かつ正）。同じ検査を dump も書き込み前に実行する
- `adjust.py`: dump 時の累積係数計算（純関数。最新行は `factor=1.0`、ライツイシューは出来高のみ係数 1）
- `dump.py`: Parquet -> Qlib dataset 変換。既定 `vendor_factor` では書き込み前に `validate_qlib_readiness` と同じ検査を行い、`adjust.py` で `$factor` を出す（`qlib.adjustment: none` なら省略）
- `qlib_dump.py`: bin / calendar / instruments の書き込み。microsoft/qlib `scripts/dump_bin.py` からの適応
- `manifest.py`: bootstrap CSV source manifest、dataset manifest、quality report
- `publish.py`: 準備済みproviderへのatomic symlink publish
- `pipeline.py`: CLI と orchestration、`init-config`、ユーザーエラーの exit 2 変換
- `__main__.py`: `python -m jqqlib` を `pipeline.main` に委譲
- `audit.py` / `trading_calendar.py`: XTKS 営業日と欠損を照合し監査結果を保存
- `config.py`: YAML 読み込みと設定検証（任意キー `qlib.adjustment`: `vendor_factor` / `none`）
- `src/jqqlib/contracts/`: JSON schema を同梱し `jqqlib.contracts.load_schema` で提供
- `src/jqqlib/data/config.example.yaml`: `init-config` が書き出す設定例（ルートの `config.example.yaml` と同一内容）

`validate` は変換元の列・値・重複などの入力品質をスタンドアロンで検査します。dump
（`convert` / `run-all` / `run-daily` / `bootstrap-csv`）は既定の `vendor_factor` で
同じ readiness 検査を書き込み前に実行します。`manifest` は生成物の lineage と
品質結果、`audit` は直近営業日の欠損を扱います。schema のソースは
`src/jqqlib/contracts/` です。[artifact の詳細](operations.md#5-dataset-artifacts)を参照してください。
