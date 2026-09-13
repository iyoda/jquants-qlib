# J-Quants の株価調整仕様（公式ドキュメントの確認記録）

[README](../README.md) · [設定](configuration.md) · [運用](operations.md) · [アーキテクチャ](architecture.md)

本パッケージは Parquet に観測したままの raw 四本値とベンダーの調整係数を保存し、
Qlib へ dump するときに累積係数 `$factor` を出します。ここに公式ドキュメントで確認した
調整の定義と、本パッケージがそれに合わせて実装している計算を記録します。
確認日は 2026-09-16 です。実データの観測値は含めていません。

## 公式仕様の要点

| 項目 | 公式の記述（要約） | 出典 |
| --- | --- | --- |
| 調整対象 | 株式分割・株式併合・ライツイシューに対応。配当など一部のコーポレートアクションは対象外 | [株価四本値][bars], [計算方法][adj] |
| 無調整の銘柄 | 外国株と TOKYO PRO MARKET 上場銘柄は調整しない（`AdjFactor = 1`） | [Help][help] |
| `AdjFactor` | 権利落ち日の行に調整係数が入る。株式分割 1:2 なら `0.5` | [株価四本値][bars] |
| 調整済み株価 | 過去日の株価は、未来側（より新しい日付）に現れた `AdjFactor` を累積（掛け算）した `CumAdj` を掛けて求める。最新日の `CumAdj` は 1.0 | [計算方法][adj] |
| 調整済み出来高 | 調整前出来高 ÷ `CumAdj` | [計算方法][adj] |
| ライツイシュー | 価格は調整するが、出来高（`Vo` / `AdjVo`）は調整しない | [株価四本値][bars] |
| 丸め | 調整済み株価は小数点第 2 位で四捨五入（日本語仕様。英語仕様の桁表記は一致しないため、丸め幅は仕様どおりの値をそのまま受け入れる） | [株価四本値][bars] |
| 遡及 | 分割・併合が発生した銘柄は、配信対象データの最も古いデータまで遡って調整済み株価が再計算される。遡及期間に上限はない | [Help][help] |
| 訂正 | データ訂正は既存データの上書きで反映され、旧版は保持されない。必要な範囲の定期的な再取得を推奨 | [更新タイミング][update] |
| 権利落種類 | `ExRT` 列に権利落ちの種類（1: 株式分割、2: 株式併合、3: ライツイシューなど）が入る | [株価四本値][bars] |
| 履歴と更新 | Light プランは 5 年前まで（Standard 10 年、Premium 20 年、データ開始 2008-05-07）。日次更新は 16:30 頃 | [データ格納期間][spec], [更新タイミング][update] |

V2 API のレスポンス列名は短縮形で、`AdjFactor` / `AdjO` / `AdjH` / `AdjL` / `AdjC` / `AdjVo` です。
bulk CSV では `AdjustmentFactor` / `AdjustmentOpen` などの長い列名が使われることがあります
（[Help][help] の V1 → V2 変更点）。

## 本パッケージでの扱い

- **Parquet は raw。** 必須列 `adjustment_factor`（`AdjFactor` / `AdjustmentFactor` を float に写す。欠落や null は `1.0`）と、ソースに `ExRT` があるときだけの任意列 `ex_rights_type` を足します。累積係数は保存しません。列順は `symbol, date, open, high, low, close, volume, amount, adjustment_factor[, ex_rights_type]`。
- **Qlib は dump 時に累積します**（既定 `qlib.adjustment: vendor_factor`）。銘柄ごとに日付昇順で、`price_cum[i] = prod(adjustment_factor[j] for j > i)`（最新行は `1.0`）。`volume_cum` も同じ積ですが、`ex_rights_type == "3"`（ライツイシュー）の行は係数 `1` として扱います。出すフィールドは `open/high/low/close` × `price_cum`、`volume` ÷ `volume_cum`、`amount` はそのまま、`factor` = `price_cum`。丸めはしません。
- **Qlib の慣習。** `raw_price = $close / $factor`、最新日は `$factor = 1.0` で保存価格は raw と一致します。配当は調整しません。
- **raw の Qlib が欲しいとき**は `qlib.adjustment: none` です。六フィールドだけで `$factor` ファイルは出しません。
- **`ExRT` は `download-csv` → `bootstrap-csv` で保持します。** `download-csv` が作る bootstrap CSV は、bulk が返した `AdjustmentFactor` などの列に加えて `ExRT` もパススルーし、`bootstrap-csv` はそれを Parquet の `ex_rights_type` に写します。合成データで分割を試すには、架空コードで `--split CODE:DATE:FACTOR` を使います。

  ```bash
  python -m tests.support.synthetic --out /tmp/sample --split 10010:2026-06-03:0.5
  ```

- 訂正は上書きで配信されるため、raw であっても過去の値が変わることがあります。運用ガイドの
  [再取得手順](operations.md#取りこぼし欠損期間の再取得)に従い、必要な範囲を定期的に取り直してください。

## 含意

公式の計算式は「保存済みの `AdjFactor` 列から銘柄ごとに未来側へ累積積を取る」だけなので、
`CumAdj`（本パッケージの `$factor`）はローカルで再現できます。新しい分割が起きて変わるのは
過去行の `CumAdj` であり、保存済みの `adjustment_factor` から次の dump で再計算できるため、
ベンダーの遡及再計算に追随する目的で履歴を取り直す必要はありません。取り直しが必要なのは
訂正（上書き）を拾う目的であり、これは調整の有無と無関係です。

ライツイシューは出来高を調整しないので、価格用の累積と出来高用の累積だけ係数の扱いが違います。
検証の目安は `$close / $factor` が Parquet の raw close と一致すること、および各銘柄の最新日で
`$factor == 1.0` であることです。設定キーは [設定リファレンス](configuration.md) の
`qlib.adjustment` です。

[bars]: https://jpx-jquants.com/ja/spec/eq-bars-daily
[adj]: https://jpx-jquants.com/ja/spec/eq-bars-daily/adj
[help]: https://jpx-jquants.com/ja/help/data
[update]: https://jpx-jquants.com/ja/spec/data-update
[spec]: https://jpx-jquants.com/ja/spec/data-spec
