# jquants-qlib

[![CI](https://github.com/iyoda/jquants-qlib/actions/workflows/ci.yml/badge.svg)](https://github.com/iyoda/jquants-qlib/actions/workflows/ci.yml)
[![CodeQL](https://github.com/iyoda/jquants-qlib/actions/workflows/codeql.yml/badge.svg)](https://github.com/iyoda/jquants-qlib/actions/workflows/codeql.yml)
[![Python 3.10–3.12](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**English summary** — A daily ETL pipeline that pulls Japanese equity daily bars from the
[J-Quants API](https://jpx-jquants.com/), stores them as deterministic raw Parquet (byte-identical
for identical inputs under the same pandas / pyarrow versions), and builds a
[Qlib](https://github.com/microsoft/qlib) dataset whose prices and volume are adjusted for
splits, reverse splits and rights issues via the vendor factor, with `$factor` in Qlib's
convention (`raw = $close / $factor`). Dividends are not adjusted. Set `qlib.adjustment: none`
for raw Qlib prices (no `$factor` file). It is designed
around the J-Quants *Light* plan: bulk-CSV bootstrap for history, incremental daily fetch with
retry/backoff, self-audit of recent business days, atomic symlink publish of a prepared Qlib
provider, and JSON manifests / quality reports with a published schema
(`src/jqqlib/contracts/`). It is for people who hold a J-Quants subscription and want a
reproducible, auditable Qlib dataset for Japanese equities without hand-rolling the
fetch / retry / validate / publish plumbing. It does not cover intraday data or non-Japanese
markets. The rest of this README is in Japanese; the CLI
(`jqqlib --help`), configuration keys and error messages are in English.

> **Data notice.** This repository contains **code only**. It does not ship any market data.
> You need your own J-Quants account and API key, and the data you download is subject to the
> [J-Quants terms of service](https://jpx-jquants.com/) — in particular it may not be
> redistributed. Keep `data/` and `config.yaml` out of version control (they are gitignored).

## 概要

J-Quants Light プランを想定した日本株の日次 ETL です。API / bulk CSV から日足を取得し、決定論的な Parquet を経由して Qlib データセットを生成します。日次の欠損監査と、準備済み provider への原子的な symlink 切替を備えます。

Parquet は観測したままの raw（未調整）です。Qlib 側の価格・出来高は、ベンダーの調整係数から dump 時に累積した `$factor` で、分割・併合・ライツイシューだけを調整します（Qlib の慣習: `raw = $close / $factor`）。配当は調整しません。raw のまま Qlib に出したい場合は `qlib.adjustment: none` です。
計算式と権利落ちの扱いは
[公式仕様の確認記録](docs/jquants-price-adjustment.md)にまとめています。

## インストール

当面は GitHub から直接インストールしてください。リリース後はタグを指定すると再現性が高くなります
（PyPI への公開は予定していますが時期未定です）。

```bash
pip install "git+https://github.com/iyoda/jquants-qlib@v0.1.0"   # タグ指定（リリース後）
pip install "git+https://github.com/iyoda/jquants-qlib@master"   # 最新リリース（master）
pip install "git+https://github.com/iyoda/jquants-qlib@dev"      # 開発版（dev）
```

pip でインストールした場合、設定ファイルの雛形はパッケージに同梱した
`config.example.yaml` を `init-config` で書き出します（既存ファイルは `--force` なしでは
上書きしません）。

```bash
jqqlib init-config config.yaml
jqqlib --version
```

開発版（テスト・lint・型検査ツールを含む）はリポジトリを clone して editable install します。
clone した場合はルートの `config.example.yaml` をコピーすれば同じ内容です。

```bash
git clone https://github.com/iyoda/jquants-qlib.git
cd jquants-qlib
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp config.example.yaml config.yaml
```

Qlib の PyPI 配布名は `pyqlib` で、依存関係として自動的に入ります。
`scripts/run_daily.sh`（日次ラッパー）と `scripts/check_qlib.py`（Qlib 読み込み確認）は
wheel に含まれない clone 専用ツールです。

## クイックスタート

### API キー不要の合成データ例

まず API キーなしで一連の変換を確認します。合成 CSV の生成器はテスト用モジュール
（`tests/support/synthetic.py`）なので、clone したリポジトリのルートで実行してください。
CSV は架空のデータです。

```bash
python -m tests.support.synthetic --out /tmp/sample
# 分割イベントを含める例（架空コード。繰り返し可。省略時は AdjustmentFactor 列なし）
# python -m tests.support.synthetic --out /tmp/sample --split 10010:2026-06-03:0.5
cat > /tmp/sample/config.yaml <<'YAML'
storage:
  parquet_dir: /tmp/sample/parquet
  qlib_dir: /tmp/sample/qlib
YAML
jqqlib bootstrap-csv --config /tmp/sample/config.yaml --csv /tmp/sample/daily_quotes.csv
jqqlib convert --config /tmp/sample/config.yaml
```

`bootstrap-csv` は CSV → Parquet → Qlib を実行します。続く `convert` は保存済み
Parquet からの再変換例です。どちらも API キー不要です。`storage.parquet_dir` と
`storage.qlib_dir` は必須キーで、省略すると設定読み込み時にエラーになります。

```text
/tmp/sample/
  daily_quotes.csv
  config.yaml
  parquet/daily_quotes.parquet
  qlib/
    calendars/day.txt
    instruments/all.txt
    features/
    dataset_manifest.json
    dataset_quality_report.json
```

同じ手順は `tests/test_cli.py` が end-to-end テストとして実行しています。

生成したデータセットは Qlib から次のように読めます（Qlib に日本向け region は無いため、
非 US 既定の `cn` を指定します。Qlib は multiprocessing を使うので、スクリプトとして実行する場合は
`if __name__ == "__main__":` ガードが必要です。clone した場合は
`python scripts/check_qlib.py --provider-uri /tmp/sample/qlib` でも同じ確認ができます）。

```python
import qlib
from qlib.data import D

if __name__ == "__main__":
    qlib.init(provider_uri="/tmp/sample/qlib", region="cn")
    print(D.features(D.instruments("all"), ["$close", "$volume", "$factor"], freq="day").head())
```

### 実データで動かす

J-Quants の API キーを用意し、`config.yaml` を作成してから `run-all` で取得と変換を行います
（clone している場合は `jqqlib init-config` の代わりに `cp config.example.yaml config.yaml` でも同じです）。

```bash
jqqlib init-config config.yaml
export JQUANTS_API_KEY="<YOUR_API_KEY>"
jqqlib run-all --config config.yaml --start 2026-07-01 --end 2026-07-03
```

設定ファイルの欠落や必須キーの不足などユーザー側の誤りは、traceback ではなく
`error: <message>` を stderr に出して exit 2 で終了します。

取得期間は契約の提供範囲に合わせてください。履歴の初回一括投入には
[bulk CSV の手順](docs/operations.md#0-初回一括投入推奨)、自動実行時の認証には
[API Keyはどこに設定する？](docs/configuration.md#api-keyはどこに設定する)を参照してください。

## 対応環境

Python 3.10–3.12（[.tool-versions](.tool-versions) は 3.12）。macOS / Linux のみ対応
（Windows 非対応: symlink publish と Keychain 分岐のため）。

## 主要コマンド一覧

`jqqlib <command> --help` で引数を確認できます。`python -m jqqlib` は `jqqlib` と同じです。
`init-config` と `publish` 以外は `--config` が必要です。

| コマンド | 役割 |
| --- | --- |
| `--version` | `jqqlib <version>` を表示（`jqqlib.__version__` と同じ値） |
| `init-config [PATH]` | 同梱の設定例を `PATH`（既定 `./config.yaml`）に書き出す。上書きは `--force` |
| `download-csv` | bulk API から初回投入用 CSV を取得 |
| `bootstrap-csv` | CSV → Parquet → Qlib の初回構築 |
| `run-etl` | 指定期間を API から差分取得 |
| `convert` | 保存済み Parquet → Qlib |
| `run-all` | 指定期間の取得と変換 |
| `run-daily` | 最新営業日の探索と未取得期間の更新 |
| `validate` | Qlib 変換元の品質検査 |
| `audit` | 直近営業日の欠損監査 |
| `publish` | 準備済み provider の symlink 切替 |
| `publish-paths` | Parquet / manifest / quality report のパスを出力 |
| `publish-check` | 運用リポジトリの `publish_check.git_ref`（既定 `origin/main`）と比較（変更なし exit 3） |
| `fundamentals_pipeline`（実験的） | 株価 CLI とは別 module の財務データ ETL（PIT パネル）。`python -m jqqlib.fundamentals_pipeline --help` |

設定ファイルや引数の誤りは `error: <message>`（stderr）と exit 2 で終了し、traceback は出ません。
`validate` の品質エラー・`audit` の欠損・`publish-check` の manifest 異常・取得や変換の dataset 状態異常は exit 1、
カレンダー不可と `publish` の置換拒否は exit 2、`publish-check` の変更なしは exit 3 です
（[exit code の一覧](docs/operations.md#exit-code-の見方)）。
実行例と cron は [運用ガイド](docs/operations.md)、障害時の手順は
[障害復旧](docs/operations.md#障害復旧) にまとめています。

## ドキュメント

- [設定と資格情報](docs/configuration.md) — 全キーと解決順序
- [運用ガイド](docs/operations.md) — 日次運用、監査、[障害復旧](docs/operations.md#障害復旧)、atomic publish、artifacts
- [財務パイプライン（実験的）](docs/fundamentals.md) — 設定・実行例と PIT
- [アーキテクチャ](docs/architecture.md) — モジュールとデータフロー
- [J-Quants の株価調整仕様](docs/jquants-price-adjustment.md) — 公式ドキュメントの確認記録と本パッケージでの扱い
- [開発・検証](CONTRIBUTING.md) / [セキュリティ](SECURITY.md) / [行動規範](CODE_OF_CONDUCT.md)
- [パッケージのリリース手順](docs/releasing.md) / [変更履歴](CHANGELOG.md)

JSON schema は `src/jqqlib/contracts/` に同梱し、
`jqqlib.contracts.load_schema("dataset_manifest")` /
`jqqlib.contracts.load_schema("dataset_quality_report")` で読み込めます。
インストール済みバージョンは `jqqlib.__version__` で確認できます。

CI の lint（Ruff）は全体に適用します。mypy は `src/jqqlib` 全体を検査し、`scripts/` と
`tests/` はまだ型検査の対象外です（[CONTRIBUTING](CONTRIBUTING.md) を参照）。

## ステータス

バージョン 0.1.x（Beta）です。CLI のコマンド名と exit code、`config.yaml` のキー、
`schema_version` 付きの manifest / quality report JSON を安定面として扱い、破壊的変更は
[互換性ポリシー](docs/releasing.md#compatibility-and-migration-policy)に従って CHANGELOG に明記します。
`fundamentals_pipeline` は実験的です。Python API（`jqqlib.<module>` の関数）には
まだ安定性の保証がありません。

## 関連プロジェクト

- [microsoft/qlib](https://github.com/microsoft/qlib) — 生成したデータセットの読み手となる研究基盤。
- [J-Quants/jquants-api-client-python](https://github.com/J-Quants/jquants-api-client-python) — 本パッケージが
  内部で使う公式クライアント。API を直接叩くだけならこちらで足ります。

## ライセンス・データ利用上の注意

コードは [MIT License](LICENSE) です。microsoft/qlib から適応した dump 実装の表記は
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) を参照してください。
このリポジトリは市場データを配布しません。
J-Quants 規約により取得データは再配布できません。`data/`、`downloads/`、
資格情報を含む `config.yaml` は Git 管理対象に含めないでください。
