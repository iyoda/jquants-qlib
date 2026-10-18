# 設計メモ（実装判断の背景）

[README](../README.md) · [アーキテクチャ](architecture.md) · [運用](operations.md) · [財務](fundamentals.md)

このページには、名前にもテストにも載せにくい背景（なぜその設計を選んだか、何を避けたか）を残します。
振る舞いそのものは各 docstring と [運用](operations.md)・[設定](configuration.md)・[財務](fundamentals.md) にあり、
ここでは繰り返しません。コメントと docstring の規約は [CONTRIBUTING](../CONTRIBUTING.md#comments-and-docstrings)
にあります。名前を変えたら、このページも同じ変更で直してください（`tests/test_design_notes.py` が、
ここに書いた名前・テスト名・パス・ファイル名の実在を検査します）。

## 取得（`fetch.py`）

### API リトライの分類

HTTP ステータスがあればそれだけで判定し、429 / 500 / 502 / 503 / 504 だけを再試行します。`requests.HTTPError` は `OSError`
のサブクラスなので、型を先に見ると 401 / 403 / 404 や、日単位へのフォールバックを起こす 400 まで再試行して
しまうためです。ステータスも型も持たない一時的な失敗（途切れた chunked 応答、名前解決の失敗、接続リセット）は
メッセージで拾いますが、認証失敗は含めません。API キー（`ClientV2`）は静的で、再試行しても直らないからです。
例外の型の組は、元の実装と同じく例外を捕まえた時点で作ります。

- 名前: `_is_transient`、`_TRANSIENT_STATUSES`、`_TRANSIENT_NETWORK_MESSAGE_RE`
- テスト: `tests/test_fetch_retry.py` の `test_requests_http_client_errors_fail_fast`、
  `test_status_less_auth_failure_message_is_not_mistaken_for_a_network_blip`

### 資格情報

- `jquantsapi`（と pandas）は、資格情報が見つかってから import します。資格情報が無い経路で、無関係な import
  失敗に邪魔されず、すぐに `ValueError` を返すためです。
  テスト: `test_missing_credentials_raise_value_error_without_importing_jquantsapi`
- Keychain は `-w` で値そのものを読みます（存在確認だけの用途では `-w` を付けない）。読んだ値はログ・標準出力・
  例外メッセージのどこにも出してはいけません。値が空、項目が無い、`security` が無い（Linux など）のいずれでも
  次の資格情報源へ進みます。service 名は、別の命名規約のツールと Keychain を共有できるよう環境変数で上書きでき、import 後の設定も効くよう
  呼び出しのたびに読みます。
  名前: `_read_keychain_secret_value_or_none`。テスト: `test_empty_keychain_api_key_falls_through_to_the_refresh_token`、
  `test_missing_security_command_is_no_credential_rather_than_a_crash`、
  `test_keychain_service_name_env_override_is_read_at_call_time`
- v1 の refresh token 用クライアント `jquantsapi.Client` は jquants-api-client 2.6 で削除されたため、その環境で
  refresh token だけがある場合は、素の `ImportError` ではなく `JQUANTS_API_KEY` を促す `ValueError` にします。
  テスト: `test_refresh_token_without_v1_client_raises_actionable_error`

テストはいずれも `tests/test_fetch_credentials.py` にあります。

## 保存（`store.py`）

- 書き込み前に `symbol, date` で並べ替えるのは、同じ内容なら同じバイト列にして、publish-check が使う
  `lineage.parquet_sha256` を取得順序や取得期間に左右されなくするためです。
  名前: `symbol_date_sorted_rows`。テスト: `tests/test_store_determinism.py`
- readiness 検査を置き換えの前に行うのは、不正な行が Parquet に残ると、最終日の翌日から再開する `run-daily` が
  その行を取り直せず、毎回失敗し続けるためです。
  テスト: `tests/test_store_readiness.py` の `test_rejected_fetch_is_refetched_by_the_next_run`
- bootstrap CSV にベンダーの調整済み列（`AdjC` など）と `ExRT` を残すのは、CSV の形を保ったまま、後から
  ベンダーの値と照合できるようにするためです。パイプライン自身はこれらを読みません。
  名前: `VENDOR_PASSTHROUGH_CSV_COLUMNS`。テスト: `tests/test_vendor_factor.py` の
  `test_vendor_adjusted_columns_follow_the_fixed_required_prefix_unchanged`

## 調整係数（`adjust.py`）

累積係数は銘柄ごとの Python ループではなく、groupby でまとめて計算します。銘柄ごとのループが大きなビルドで
処理時間の大半を占めていたためです。積を float64 で計算するのは、Parquet から読み戻した nullable 整数
（`Int64`）の係数が、groupby の shift の埋め値 1.0 を受け付けないためです。

- 名前: `_by_symbol_first_appearance_then_date`、`_per_symbol_product_of_later_values`、`float_factors`
- テスト: `tests/test_adjust.py` の `test_vectorised_adjustment_matches_the_per_symbol_reference`、
  `test_nullable_integer_factor_is_adjusted_like_float`、`test_rows_without_a_symbol_are_dropped_like_the_reference`

## Qlib dump（`dump.py`）

- 生成サブツリーの入れ替えに失敗したら、`BaseException`（Ctrl-C を含む）でも以前の provider に戻します。
  バックアップは空になってから消すので、復元自体が失敗しても古いデータは手で戻せる形で残ります。
  名前: `_roll_back_subtree_swap`、`_remove_backup_root_once_empty`。テスト: `tests/test_dump.py` の
  `test_interrupt_during_subtree_swap_restores_the_previous_provider`、
  `test_failed_restore_after_an_interrupt_keeps_the_backup_recoverable`
- ビルドは解決した `storage.qlib_dir` の隣で行います。`qlib_dir` が別ファイルシステムへのシンボリックリンク
  （publish 済みの provider など）だと、リンクの隣からの rename が `EXDEV` で失敗するためです。
  名前: `beside_resolved_qlib_dir`。テスト: `tests/test_dump.py` の
  `test_symlinked_qlib_dir_on_another_filesystem_is_staged_beside_its_target`

## Manifest と品質レポート（`manifest.py`）

- `vendor_factor` は `$factor` を出すと約束しているので、係数を計算できない Parquet は検査を飛ばさず fail
  にします。テスト: `test_quality_report_fails_when_the_vendor_factor_cannot_be_computed`
- 同じ Parquet でも `qlib.adjustment` が違えば別の dataset なので、dataset ID に `adjustment` を含めます。
  テスト: `test_dataset_id_depends_on_the_adjustment_mode`
- 品質検査は dataset 全体に係数の変換をかけて重いので、manifest と品質レポートで 1 回の検査を共有します。
  テスト: `test_write_dataset_artifacts_inspects_the_dataset_once`
- 最終版の品質レポートのハッシュを manifest に書き込んでから `manifest_sha256` を計算し、品質の判定を
  manifest に結び付けます。名前: `_bind_final_quality_report_into_manifest`。
  テスト: `test_manifest_lineage_binds_the_final_quality_report_bytes`

テストはいずれも `tests/test_manifest.py` にあります。

## パイプラインと CLI（`pipeline.py`）

run-daily の取り込み範囲と、途中で失敗したときの挙動は [運用ガイド](operations.md#日次運用)、終了コードの一覧は
[exit code の見方](operations.md#exit-code-の見方) にあります。ここでは理由だけを書きます。

- `run-daily` は使わない `--window-business-days` も受け付けます。日次ラッパーが同じ引数を `run-daily` と
  `audit` の両方に渡すので、未知の引数でジョブ全体が止まらないようにするためです。
  名前: `_accept_audit_only_flags_forwarded_by_the_daily_wrapper`。
  テスト: `test_run_daily_accepts_the_audit_window_flag_the_daily_wrapper_forwards`
- 401 / 403 は、再実行では直らず設定の修正が要るので exit 2 にします。`requests` / `pyarrow` の例外を
  実行失敗（exit 1）とみなすのは、そのモジュールが既に import されているときだけです。import されていない
  モジュールの例外は起こり得ないので、判定のためだけに import しません。XTKS カレンダーが使えないときの
  exit 2 は、監査そのものができなかったことを欠損（exit 1）と区別するためです。
  名前: `cli_error_exit_code`、`_AUTH_STATUSES`、`_is_run_failure_from_an_already_imported_library`。
  テスト: `CliExitCodeTests`、`test_unavailable_calendar_exits_2_not_as_a_data_gap`
- ログのハンドラは `main()` の実行中だけ付けます。ライブラリとして import した側には何も出さず、CLI では
  進捗を stdout、警告を stderr に出すためです。ライブラリのコードから `logging.basicConfig` は呼びません。
  名前: `_configure_cli_logging`、`_teardown_cli_logging`。テスト: `tests/test_cli.py` の
  `test_run_daily_progress_goes_to_stdout_and_import_attaches_no_handler`
- `last_audit.json` を書けなくても取り込みは失敗させません。サイドカーは監視用で、無ければ下流では
  `audit: not_run` と読まれるからです。テスト: `test_sidecar_write_failure_warns_with_configured_path`

上の 3 件のテストは `tests/test_pipeline_daily.py` にあります。

### publish-check

- `git show` はパスをリポジトリのルートから解決するので、manifest のパスはルートからの相対パスに直します。
  置き換えるのは解決するとルートになる最も長い先頭部分だけなので、`/var` → `/private/var` のような絶対パスも
  通り、リポジトリ内の provider リンクは git 側で辿られます。コミットされたシンボリックリンクは深さ 8 まで辿ります。
  名前: `_manifest_path_from_repo_root`、`read_committed_file_following_symlinks`。
  テスト: `test_absolute_path_through_a_symlinked_parent_keeps_the_provider_symlink`、
  `test_no_op_reads_origin_manifest_through_committed_provider_symlink`
- コミット済み manifest が「無い」（初回 publish）と「調べられない」（git が無い、作業ツリーの外、存在しない ref）
  を区別し、後者はエラーにします。初回 publish と同じに扱うと、毎回「変更あり」と報告してしまうためです。
  名前: `_CommittedManifestLookupFailed`。テスト: `test_unknown_git_ref_is_an_error_not_a_first_publish`
- Parquet が同じでも `adjustment_policy` が違えば Qlib の dataset は変わるので、「変更あり」とします。
  名前: `adjustment_policy_changed`。
  テスト: `test_adjustment_policy_change_with_identical_parquet_requires_publish`

テストはいずれも `tests/test_publish_check_git.py` にあります。

### publish 台帳（`ledger.py`）

`publish_check.baseline: file` は、manifest を git に置かずに比較基準を持つための仕組みです。データ由来の
JSON を毎日公開リポジトリに commit すると J-Quants の再配布の制限に触れるため、git の比較基準だけでは
運用リポジトリを公開できません。

- git と file はどちらか一方だけを使い、失敗しても切り替えません。黙って切り替えると、どちらと比べたのかが
  分からなくなるためです。名前: `_baseline_for_publish_check`
- 台帳が無い・壊れているときは初回 publish にせずエラーにします。git では「無ければ初回」ですが、ファイルでは
  パスの誤りや書きかけのファイルでも「無い」に見え、比較基準を失ったまま publish してしまうためです。
  「初期化済みで基準なし」は、`ledger init` が作る空のファイルとして明示します。
  テスト: `test_missing_ledger_fails_closed_instead_of_a_first_publish`、`test_initialised_empty_ledger_is_a_first_publish`
- 途中の行が 1 行でも読めなければ止めます。書き込みは一時ファイルと rename で丸ごと置き換えるので、正常な
  運用では書きかけの行は生じず、あれば別の何かが壊したと考えるためです。項目が欠けた行も同じです。比べる項目
  だけが揃った手書きの行を受け入れると、壊れた台帳でも「変更なし」と判定してしまいます。
  名前: `_replace_atomically`、`_entry_problem`。
  テスト: `test_unparsable_or_blank_line_anywhere_is_an_error`、`test_ledger_without_a_trailing_newline_is_a_truncated_write`、
  `test_entry_missing_any_field_or_with_a_wrong_type_is_an_error`
- symlink の台帳は拒否します。rename はリンクそのものを置き換え、ロックもリンクの隣にできるので、読む先と書く先・
  ロックが分かれてしまうためです。名前: `_refuse_symlink`。
  テスト: `test_symlinked_ledger_is_refused_so_reads_writes_and_the_lock_cannot_diverge`
- 台帳の読み書きの失敗（権限、容量不足など）は `LedgerError`（exit 1）にします。`OSError` のままだと CLI が設定の
  誤り（exit 2）に分類してしまうためです。exit 2 は `ledger init` が既存の台帳を見つけたときだけで、その判定は
  書き込みの外で行います（一時ファイルの衝突などの `FileExistsError` まで exit 2 にしないため）。
  テスト: `test_unreadable_ledger_location_is_a_ledger_error_not_a_user_error`、
  `test_failed_ledger_write_is_a_ledger_error_and_keeps_the_previous_ledger`、
  `test_init_write_failure_even_a_temporary_file_collision_is_a_ledger_error`、
  `test_directory_fsync_failure_after_the_rename_keeps_the_new_ledger_and_a_rerun_is_a_no_op`
- 追記は末尾の行と同じ dataset なら何もしません。日次処理が追記の後で止まって再実行されても、行が重ならないように
  するためです。追記の読み直しと書き込みはロックの中で行います。名前: `_records_the_same_dataset`。
  テスト: `test_rerun_after_a_completed_append_does_not_duplicate_the_last_entry`、
  `test_interrupted_append_leaves_the_previous_ledger_and_no_temporary_file`、
  `test_append_reads_and_rewrites_while_holding_the_exclusive_lock`、
  `test_append_replaces_the_ledger_while_holding_the_exclusive_lock`
- `ledger append` は `publish-check` と同じ dataset の検査を通します。検査を 1 か所にしておかないと、比較では
  拒否される dataset が台帳に入り得るためです。名前: `_check_current_dataset`。
  テスト: `test_append_refuses_a_dataset_that_publish_check_would_refuse`
- 記録する jqqlib の commit は、パッケージが jquants-qlib の作業ツリーの `src/jqqlib` にあるときだけ取ります。
  別のリポジトリの中の venv に install された wheel で `git rev-parse HEAD` すると、そのリポジトリの commit を
  記録してしまうためです。継承した `GIT_DIR`・`GIT_WORK_TREE` など、別のリポジトリを指す変数（git の hook の中で
  設定される）だけを外して実行します。`GIT_*` を全部外すと、環境変数で渡した `safe.directory` まで消えて、
  所有者の違う checkout で commit が null になるためです。
  版と commit は記録だけで、比較には使いません。版は `pyproject.toml` の固定値で、変換処理の変更とは対応しない
  ためです。名前: `jqqlib_source_commit`、`_environment_without_git_repository_overrides`、
  `_REPOSITORY_LOCATION_VARIABLES`。
  テスト: `test_installed_package_inside_another_repository_records_no_commit`、
  `test_inherited_git_repository_variables_do_not_change_the_recorded_commit`、
  `test_git_configuration_inherited_from_the_environment_still_applies`、
  `test_a_different_producer_version_alone_is_the_same_dataset`
- git と file のどちらでも、エラーの結果にも設定した比較基準を入れます。名前: `_with_configured_baseline`。
  テスト: `test_error_decision_names_the_configured_baseline`、`test_git_baseline_never_falls_back_to_a_matching_ledger`

テストはいずれも `tests/test_publish_ledger.py` にあります。

## 監査とカレンダー（`audit.py` / `trading_calendar.py`）

XTKS の休場日に当たる平日は、以前の月〜金の判定なら欠損になっていました。欠損にせず
`suppressed_non_sessions` に記録するのは、抑制したこと自体を監査できるようにするためです。
`exchange_calendars` が事前計算の範囲外で投げる `DateOutOfBounds` は「カレンダーが答えられない」として
`ExactCalendarRequired` にし、推測では埋めません（`_OUT_OF_PRECOMPUTED_RANGE_ERROR_NAME`）。

- テスト: `tests/test_audit.py` の `test_exchange_holiday_is_suppressed_not_flagged`、
  `test_date_out_of_bounds_fails_closed_not_as_a_gap`

## 設定（`config.py`）

`storage.*` の相対パスは YAML の置き場所ではなく作業ディレクトリを基準にします。後から加えた `audit_dir` も、
それ以前からある `parquet_dir` / `qlib_dir` の利用側に合わせて同じ基準にしています。
テスト: `tests/test_config.py` の `test_storage_paths_keep_working_directory_semantics`

## 財務（`fetch_fundamentals.py` / `normalize_fundamentals.py`）

PIT の規則そのものは [財務](fundamentals.md#pitpoint-in-timeの意味) にあります。

- 試行済みの銘柄は、データのキャッシュとは別のファイルに記録します（`_attempted_codes_sidecar_path`）。
  開示がまだ無い銘柄（ETF、REIT、新規上場など）は空の応答を返してデータのキャッシュに現れないので、
  キャッシュだけを「処理済み」とみなすと、再開のたびに同じ銘柄を問い合わせ続けてしまうためです。
  例外で終わった呼び出しは結果が分からないので、試行済みにしません。
- 再開時は、キャッシュの行のうち試行済みの記録にある銘柄の行だけを読みます。チェックポイントはキャッシュを
  置き換えてから試行済みの記録を書くので、その間で止まると、記録に無い銘柄の行がキャッシュに残ります。
  次の実行はその銘柄を問い合わせ直すため、残った行を読むと同じ行が二重になります。
  チェックポイントは、新しい行が無くても、読まなかった行を除いた内容でキャッシュを置き換えます。置き換えずに
  試行済みの記録だけを進めると、問い直して空だった銘柄の古い行が記録済みになり、次の実行で戻ってきます。
- `Code` 列は応答の値に関係なく要求した銘柄コードで上書きし、結合キーを揃えます（財務データの事前調査で
  確かめた挙動に合わせたもの）。
  テスト（上の 3 項目）: `tests/test_fundamentals_fetch_retry.py` の
  `test_codes_with_empty_response_are_not_requeried_on_resume`、
  `test_rows_cached_before_the_attempted_record_was_written_are_not_duplicated_on_resume`、
  `test_unrecorded_cached_rows_of_a_code_that_now_returns_empty_are_not_revived_by_the_next_run`、
  `test_code_whose_request_raised_is_requested_again_on_resume`、`test_join_key_is_forced_to_the_requested_code`
- `CARRY_NUM`（V2 `/fins/summary` の列）は、実データでの取得（ユニバース 500 銘柄中 483 銘柄、
  `pit_violations == 0`）で確かめてから固定した集合です。項目を増やすときは同じ確認をしてください。
- 同じ日の開示を項目ごとにマージする（`GroupBy.last` は null を飛ばす）のは、業績予想の修正が同時に出た決算の
  値を消さないようにするためです。並べ替えは辞書式なので、欠けたキーが不利になるのは、それより前のキーが
  同じ開示どうしの中だけです（時刻が遅い開示は `DiscNo` が欠けていても優先されます）。
  `DiscNo` の数字判定を ASCII の `[0-9]` に限るのは、`\d` が他の Unicode 数字にも一致し、`lstrip("0")` と
  桁数比較で正しく並ばないためです（`_ASCII_DIGITS_RE`）。
  テスト: `tests/test_fundamentals_normalize.py` の
  `test_a_later_disc_time_outranks_an_earlier_keyed_one_even_without_disc_no`、
  `test_disc_no_with_non_ascii_digits_is_not_a_usable_key`

## スクリプト（`scripts/`）

- `check_comments.py`: CONTRIBUTING の規約を検査します。`src/jqqlib` と `scripts` の外のパスと、存在しない
  ファイル（壊れたリンクを含む）は無視するので、lefthook がステージしたファイルの一覧をそのまま渡せます。
  - シンボリックリンクは、リンク先の実体が `src/jqqlib` か `scripts` にあるときだけ検査します。どちらの規則を
    当てるか（公開 API か、`scripts` か）と拡張子は、リンク自身のパス、つまり import や実行に使われるパスで
    決めます。リンク自身がどちらの外にあるときだけ、実体のパスで決めます（`governed_relative_path`）。
  - 管轄内の、開けないディレクトリや読めないファイルは、traceback でも無言の通過でもなく、違反として報告します。
- `check_qlib.py`: Qlib には JP のリージョン定数が無いので、US 以外の既定の `REG_CN` を使います
  （`QLIB_NON_US_REGION_FOR_JP_DATA`）。
- `dist_smoke.py`: リポジトリの外から、`PYTHONPATH` をチェックアウトに向けずに実行し（手順は
  [リリース手順](releasing.md)）、`jqqlib` がインストール済みの wheel から解決されることを確かめます。CLI は
  pip の利用者と同じく、インタープリタの隣の console script で動かします（`installed_jqqlib_console_script`）。
  wheel には `tests/` が入らないので、CSV はインラインのフォールバック（`write_inline_csv`）で作ります。
