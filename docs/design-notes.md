# 設計メモ（実装判断の背景）

[README](../README.md) · [アーキテクチャ](architecture.md) · [運用](operations.md) · [財務](fundamentals.md)

`src/jqqlib/` と `scripts/` には `#` コメントを書きません。docstring は、型付きパッケージ（`py.typed`）の
公開 API、つまり `_` で始まらないモジュール・クラス・関数にだけ書きます。検査は `scripts/check_comments.py`
が行い、unittest・CI・lefthook から同じものを呼びます。
意図は関数・定数・変数の名前で表し、守るべき振る舞いはその理由を名前にしたテストで固定します。
このページには、名前にもテストにも載せにくい背景（なぜその設計を選んだか、何を避けたか）を残します。
各項目の「名前」「テスト」から実装と回帰テストを辿れます。

## 取得（`fetch.py`）

### API リトライの分類

- 判断: HTTP ステータスを持つエラーはステータスだけで判定し、429 / 500 / 502 / 503 / 504 だけを
  再試行する。ステータスが無ければ `TimeoutError` / `ConnectionError` / `OSError` /
  `requests.exceptions.RetryError` を再試行し、型でも分からないものはメッセージで判定する。
- 理由: `requests.HTTPError` は `OSError` のサブクラスなので、型の判定を先にすると
  401 / 403 / 404 や、日単位へのフォールバックを起こす 400 まで再試行してしまう。
  メッセージ判定は、途切れた chunked 応答・名前解決の一時失敗・接続リセットのように
  型もステータスも持たない一時的な失敗を拾うためのもの。認証失敗は意図的に含めない。
  API キー（`ClientV2`）は静的で再認証の余地がないので、即座に失敗させる。
- 名前: `_is_transient`、`_TRANSIENT_STATUSES`、`_TRANSIENT_NETWORK_MESSAGE_RE`
- テスト: `tests/test_fetch_retry.py` の `test_requests_http_client_errors_fail_fast`、
  `test_status_less_auth_failure_message_is_not_mistaken_for_a_network_blip`

### 資格情報が無いときは jquantsapi を import しない

- 判断: `jquantsapi`（とその依存の pandas）は、資格情報が見つかってクライアントを作る箇所で
  初めて import する。
- 理由: 資格情報が無い経路では、import の時間をかけずに `ValueError` を返したい。呼び出し側が
  「資格情報なし」を軽い no-op として扱えるようにし、無関係な import 失敗でそれが隠れないようにする。
- テスト: `tests/test_fetch_credentials.py` の
  `test_missing_credentials_raise_value_error_without_importing_jquantsapi`

### Keychain からの読み取り

- 判断: `security find-generic-password` には `-w` を付けて値そのものを読む。値が空、項目が無い、
  `security` を実行できない（Linux など）のいずれでも `None` を返し、次の資格情報源へ進む。
- 理由: 呼び出し側が必要としているのは値なので `-w` が要る。存在確認だけの用途では `-w` を付けない。
  読んだ値はログ・標準出力・例外メッセージのどこにも出してはならない。
- 判断: Keychain の service 名（`JQQLIB_KEYCHAIN_API_KEY_SERVICE` /
  `JQQLIB_KEYCHAIN_REFRESH_TOKEN_SERVICE`）は、import 時ではなく呼び出しのたびに環境変数から読む。
- 理由: import 後に設定した環境変数も効くようにするため。別の命名規約を使うツールと Keychain を
  共有する場合に上書きする。
- 名前: `_read_keychain_secret_value_or_none`
- テスト: `tests/test_fetch_credentials.py` の
  `test_empty_keychain_api_key_falls_through_to_the_refresh_token`、
  `test_missing_security_command_is_no_credential_rather_than_a_crash`、
  `test_keychain_service_name_env_override_is_read_at_call_time`

### 旧 refresh token クライアント

`jquantsapi.Client`（v1 の refresh token 用クライアント）は jquants-api-client 2.6 で削除されました。
そのバージョンでは API キー（`ClientV2`）しか使えないので、refresh token が見つかった場合は素の
`ImportError` ではなく、`JQUANTS_API_KEY` を設定するよう促す `ValueError` にします。
テスト: `tests/test_fetch_credentials.py` の `test_refresh_token_without_v1_client_raises_actionable_error`

### YAML 資格情報の抜け道

`JQQLIB_ALLOW_YAML_CREDENTIALS=1` は、ローカルでのデバッグに限った、わざと目立たせた抜け道です。
使われるたびに stderr へ ALERT を出します。詳細は `create_jquants_client` の docstring を参照してください。

## 保存（`store.py`）

### Parquet の行順を固定する

- 判断: 書き込み前に `symbol, date` で並べ替え、列順も固定する。
- 理由: 論理的に同じ内容なら同じバイト列になり、`lineage.parquet_sha256` が取得順序や取得期間に
  左右されなくなる。publish-check はこのハッシュで「変更あり / 変更なし」を判定する。
- 名前: `symbol_date_sorted_rows`
- テスト: `tests/test_store_determinism.py`

### Parquet を置き換える前に readiness 検査を行う

- 判断: マージ後の行に `validate` と同じ検査を行い、失敗したら Parquet を変更しない。
- 理由: 不正な行が Parquet に残ると、`run-daily` は Parquet の最終日の翌日から再開するため、
  その行を再取得できずに毎回失敗し続ける。
- テスト: `tests/test_store_readiness.py` の `test_rejected_fetch_is_refetched_by_the_next_run`

### bootstrap CSV にベンダーの調整済み列を残す

- 判断: CSV は必須列（`BOOTSTRAP_CSV_COLUMNS`）を固定の先頭に置き、bulk エンドポイントが付ける
  ベンダーの調整済み値（`AdjC` など）や `ExRT` はそのまま後ろに残す。それ以外の列は落とす。
- 理由: CSV の形を安定させつつ、ベンダーの値を捨てずに後から照合できるようにする。
  パイプライン自身はこれらの調整済み値を読まない。
- 名前: `VENDOR_PASSTHROUGH_CSV_COLUMNS`
- テスト: `tests/test_vendor_factor.py` の
  `test_vendor_adjusted_columns_follow_the_fixed_required_prefix_unchanged`

## 調整係数（`adjust.py`）

- 判断: 累積係数は銘柄ごとの Python ループではなく、全銘柄をまとめて groupby でベクトル化して計算する。
- 理由: 銘柄ごとのループが大きなビルドで処理時間の大半を占めていたため。出力は銘柄の初出順に
  まとまり、各銘柄の中は日付順になる。`symbol` が欠けた行は groupby と同じく落とす
  （全行で `symbol` が欠けていれば、行をそのまま返して `factor=1.0` とする）。
- 判断: 積の計算は float64 で行う。
- 理由: Parquet から読み戻した nullable 整数（`Int64`）の係数は、groupby の shift の
  埋め値 1.0 を受け付けないため。
- 名前: `_by_symbol_first_appearance_then_date`、`float_factors`
- テスト: `tests/test_adjust.py` の `test_vectorised_adjustment_matches_the_per_symbol_reference`、
  `test_nullable_integer_factor_is_adjusted_like_float`、`test_rows_without_a_symbol_are_dropped_like_the_reference`

## Qlib dump（`dump.py`）

### 生成サブツリーの入れ替えと巻き戻し

- 判断: `features` / `calendars` / `instruments` の入れ替えに失敗したときは、`BaseException`
  （Ctrl-C の `KeyboardInterrupt` や `SystemExit` を含む）を捕まえて元に戻す。バックアップ用の
  ディレクトリは空になったときだけ削除する。
- 理由: 中断されたときも以前の provider を復元するため。復元自体が失敗した場合でも、古いデータを
  消さずに手で戻せる状態で残すため。
- 名前: `_roll_back_subtree_swap`、`_remove_backup_root_once_empty`
- テスト: `tests/test_dump.py` の `test_interrupt_during_subtree_swap_restores_the_previous_provider`、
  `test_failed_restore_after_an_interrupt_keeps_the_backup_recoverable`

### ステージング先は実体ディレクトリの隣

- 判断: ビルド用の一時ディレクトリは、`storage.qlib_dir` を解決した先（実体）の親に作る。
- 理由: `qlib_dir` が別ファイルシステム上の実体を指すシンボリックリンク（publish 済みの
  provider など）の場合、リンクの隣にステージングすると最後の rename が `EXDEV`
  （Invalid cross-device link）で失敗する。
- 名前: `beside_resolved_qlib_dir`
- テスト: `tests/test_dump.py` の `test_symlinked_qlib_dir_on_another_filesystem_is_staged_beside_its_target`

## Manifest と品質レポート（`manifest.py`）

- 判断: `vendor_factor` で係数を計算できない Parquet は、検査を飛ばさず品質レポートを fail にする。
- 理由: `vendor_factor` は `$factor` 系列を出すと約束しているため、計算できなければ失敗した dataset として扱う。
- テスト: `tests/test_manifest.py` の `test_quality_report_fails_when_the_vendor_factor_cannot_be_computed`
- 判断: dataset ID の元になる識別情報に `adjustment` を含める。
- 理由: 同じ Parquet でも、`qlib.adjustment` が違えば別の dataset になるため。
- テスト: `tests/test_manifest.py` の `test_dataset_id_depends_on_the_adjustment_mode`
- 判断: `write_dataset_artifacts` では Parquet の読み込みと品質検査を一度だけ行い、manifest と
  品質レポートの両方で使う。
- 理由: 品質検査は dataset 全体に vendor-factor 変換をかけるため、重い。
- テスト: `tests/test_manifest.py` の `test_write_dataset_artifacts_inspects_the_dataset_once`
- 判断: 品質レポートを書き終えてから、そのハッシュを manifest の lineage に入れ、最後に
  `manifest_sha256` を計算し直す。
- 理由: 品質の判定結果を、最終版のレポートのバイト列と manifest に結び付けるため。
- 名前: `_bind_final_quality_report_into_manifest`
- テスト: `tests/test_manifest.py` の `test_manifest_lineage_binds_the_final_quality_report_bytes`

## パイプラインと CLI（`pipeline.py` / `fundamentals_pipeline.py`）

### run-daily の取り込み範囲

`run-daily` は `[start_date, target_date]` の連続した範囲を一度の呼び出しで取得します。

- 取得に失敗したり、データが検査で拒否されたりした場合、`write_parquet` は検査の後に
  アトミック（一時ファイル + replace）に書き込むので、Parquet の最終日は変わらない。
  次回の実行が同じ範囲を埋め直す。
- Qlib dump が後で失敗した場合、Parquet は先に進んだままになる。ただし dump は毎回 Parquet 全体から
  provider を作り直すので、次に成功した実行で追いつく。
- 範囲内のどこにもデータが無ければ `NoDataFetchedError` を投げ、前営業日へのフォールバックを起こす。

テスト: `tests/test_pipeline_daily.py` の `test_run_daily_catches_up_missing_range_in_single_fetch`、
`tests/test_store_readiness.py` の `test_rejected_fetch_is_refetched_by_the_next_run`

### run-daily が監査用フラグを受け付ける理由

- 判断: `run-daily` は `--window-business-days` を受け付けるが、使わない（help にも出さない）。
- 理由: 日次ラッパーは同じ引数リストを `run-daily` と `audit` の両方に渡す。未知の引数で
  ジョブ全体が止まらないように、`run-daily` 側で監査専用フラグを許容する。
- 名前: `_accept_audit_only_flags_forwarded_by_the_daily_wrapper`
- テスト: `tests/test_pipeline_daily.py` の
  `test_run_daily_accepts_the_audit_window_flag_the_daily_wrapper_forwards`

### 終了コードの境界

- CLI は traceback の代わりに `error: <message>` を stderr に 1 行出す。終了コードで
  ユーザー側の誤り（2）と実行の失敗（1）を区別する。財務 CLI も同じ境界を使う。
- 401 / 403 は 2 とする。キーが誤っているか期限切れなので、再実行しても直らず、設定の修正が要る。
- `requests` / `pyarrow` の例外を実行失敗（1）と判定するのは、そのモジュールが既に import
  されているときだけ。import されていないモジュールの型の例外が発生することはあり得ないので、
  判定のためだけに import しない。
- XTKS カレンダーが使えないときは 2 とする。監査そのものが実行できなかったことを、欠損（1）と
  区別するため。日次ラッパーは、ありもしない欠損ではなく「カレンダー」の問題として通知できる。
- 名前: `cli_error_exit_code`、`_AUTH_STATUSES`、`_is_run_failure_from_an_already_imported_library`
- テスト: `tests/test_pipeline_daily.py` の `CliExitCodeTests`、
  `test_unavailable_calendar_exits_2_not_as_a_data_gap`、`tests/test_fundamentals_cli.py`

### CLI のログ出力

- 判断: `jqqlib` のロガーにハンドラを付けるのは `main()` の実行中だけ。INFO 以下は stdout、
  WARNING 以上は stderr に出し、終わったら外す。ライブラリのコードからは `logging.basicConfig` を呼ばない。
- 理由: ライブラリとして import した側には何も出さない（既定は無音）一方で、CLI では従来の `print()` と
  同じく進捗を stdout、警告を stderr に出すため。
- 名前: `_configure_cli_logging`、`_teardown_cli_logging`、`_BelowWarningFilter`
- テスト: `tests/test_cli.py` の `test_run_daily_progress_goes_to_stdout_and_import_attaches_no_handler`、
  `test_cli_logging_sends_warnings_to_stderr`

`--as-of today` は Asia/Tokyo の現在日付です。現在時刻は `_now` から取り、テストはこれを差し替えて固定します
（`tests/test_pipeline_daily.py` の `test_parse_as_of_date_today_is_asia_tokyo`）。

### 監査サイドカーの書き込み失敗

- 判断: `last_audit.json` を書けなくても警告だけ出し、取り込みは失敗させない。
- 理由: サイドカーは監視用の成果物で、無ければ下流では `audit: not_run` と読まれるため。
- テスト: `tests/test_pipeline_daily.py` の `test_sidecar_write_failure_warns_with_configured_path`

### publish-check

- 判断: コミット済み manifest は、親ディレクトリのいずれかがシンボリックリンクとしてコミット
  されていても辿って読む。リンクを見つけたら置き換えて再帰し、深さは 8 までとする。
- 理由: provider のパスが、ビルドディレクトリへのシンボリックリンクとしてコミットされている
  場合があるため。
- 名前: `read_committed_file_following_symlinks`
- テスト: `tests/test_publish_check_git.py` の
  `test_no_op_reads_origin_manifest_through_committed_provider_symlink`
- 判断: 作業ツリーの manifest のパスは、リポジトリのルートからの相対パスに直してから
  `git show <ref>:<path>` に渡す。置き換えるのは、解決するとルートになる最も長い先頭部分だけにする。
- 理由: `git show` はパスをカレントディレクトリではなくリポジトリのルートから解決するので、絶対パスの
  `storage.qlib_dir` やサブディレクトリからの実行はそのままでは読めない。先頭だけを置き換えるので、
  シンボリックリンク経由の絶対パス（macOS の `/var` → `/private/var`）も対応でき、リポジトリ内の
  シンボリックリンク（publish 用の provider リンク）は git のパスに残って git 側で辿られる。
- 名前: `_manifest_path_from_repo_root`
- テスト: `tests/test_publish_check_git.py` の `test_absolute_qlib_dir_is_mapped_to_the_repo_path`、
  `test_absolute_path_through_a_symlinked_parent_keeps_the_provider_symlink`、
  `test_run_from_subdirectory_is_mapped_to_the_repo_path`
- 判断: コミット済み manifest の読み取りは、「無い」（初回 publish、`None`）と「調べられない」
  （git が無い、作業ツリーの外、存在しない ref、manifest が作業ツリーの外）を区別する。後者は例外にする。
- 理由: 調べられないことを初回 publish と同じに扱うと、毎回「変更あり」と報告してしまうため。
- 名前: `_committed_manifest`、`_CommittedManifestLookupFailed`
- テスト: `tests/test_publish_check_git.py` の `test_unknown_git_ref_is_an_error_not_a_first_publish`、
  `test_outside_a_git_work_tree_is_an_error`、`test_missing_git_binary_is_an_error`
- 判断: Parquet のハッシュが同じでも、`adjustment_policy` が違えば「変更あり」とする。
- 理由: `qlib.adjustment` を切り替えたり、係数の方針を変えたりすると、Qlib の dataset が変わるため。
- 名前: `adjustment_policy_changed`
- テスト: `tests/test_publish_check_git.py` の
  `test_adjustment_policy_change_with_identical_parquet_requires_publish`

## 監査とカレンダー（`audit.py` / `trading_calendar.py`）

- XTKS の休場日に当たる平日は、以前の月〜金のヒューリスティックなら欠損と判定していた。
  この日は欠損にせず `suppressed_non_sessions` に記録して、抑制したこと自体を監査できるようにする。
- `exchange_calendars` は、事前計算した範囲の外の日付で `DateOutOfBounds` を投げる。
  これは「カレンダーが答えられない」として `ExactCalendarRequired` に変換し、推測では埋めない。
  例外の型は import せず名前で照合する（`_OUT_OF_PRECOMPUTED_RANGE_ERROR_NAME`）。
- テスト: `tests/test_audit.py` の `test_exchange_holiday_is_suppressed_not_flagged`、
  `test_date_out_of_bounds_fails_closed_not_as_a_gap`

## 設定（`config.py`）

`storage.*` の相対パスは YAML ファイルの置き場所ではなく、プロセスの作業ディレクトリを基準に解決します。
後から加わった `audit_dir` も、それ以前からある `parquet_dir` / `qlib_dir` の利用側に合わせています。
テスト: `tests/test_config.py` の `test_storage_paths_keep_working_directory_semantics`

## 財務（`fetch_fundamentals.py` / `normalize_fundamentals.py` / `store_fundamentals.py`）

### 取得の再開

- 生の取得結果のキャッシュ（`DEFAULT_RAW_CACHE_FILENAME`）は、強制終了や再起動の後に取得を
  再開できるようにするためのもの。既に API 呼び出しを費やした銘柄を取り直さずに済む。
- 試行済みの銘柄は、データのキャッシュとは別のファイル（`_attempted_codes_sidecar_path`）に記録する。
  まだ財務の開示が無い銘柄（ETF、REIT、新規上場など）は正当に空の応答を返し、データのキャッシュには
  現れない。データのキャッシュだけを「処理済み」とみなすと、再開のたびにその銘柄を問い合わせ続け、
  まだ試していない銘柄へ進めなくなる。
- 銘柄を「試行済み」とするのは、API 呼び出しが戻った後（データありか、正当な空の結果）だけ。
  例外で終わった呼び出しは結果が分からない状態なので、次回の実行で再試行の対象に残す。
- `Code` 列は、API 応答が返す値に関係なく、要求した銘柄コードで上書きする。結合キーを揃えるためで、
  財務データの事前調査（probe）で確かめた挙動に合わせている。
- テスト: `tests/test_fundamentals_fetch_retry.py` の
  `test_code_whose_request_raised_is_requested_again_on_resume`、
  `test_codes_with_empty_response_are_not_requeried_on_resume`、
  `test_join_key_is_forced_to_the_requested_code`

### 取り込む数値項目

`CARRY_NUM`（V2 `/fins/summary` の列）は、実データでの取得（ユニバース 500 銘柄中 483 銘柄、
`pit_violations == 0`）で確かめてから固定した集合です。項目を増やすときは、同じ確認をしてから変更してください。

### PIT パネルの組み立て

- 有効日は `DiscDate` より厳密に後の最初の営業日とし、開示当日には使わない
  （`first_session_strictly_after_disclosure`）。
- 同じ日に有効になる開示は、`DiscDate` → `DiscTime`（時刻として比較）→ `DiscNo`（整数として、桁数、
  次に数字列で厳密に比較）の順に並べる。休場日をはさんで別の日に出た開示が同じ日に有効になる場合は、
  項目ごとに `DiscDate` の新しい方の値が優先される。どれも同点の開示だけは、安定ソートで入力順を保つ。
- 並べ替えは辞書式で、欠けているか使えないキーの値は、それより前のキーが同じ行どうしの中で先頭に並ぶ。
  各項目は最後の null でない値を採るので、たとえば時刻が同じなら `DiscNo` のある行が `DiscNo` のない行より
  優先される。一方、時刻が遅い行は `DiscNo` が欠けていても、時刻が早い行より優先される。
- `DiscNo` は先頭のゼロを除いた数字列に正規化する（`_disc_no_canonical_digits`）。数字の文字列と
  整数値の数（欠損を含む整数列は float64 で読まれる）を受け付けるので、`"10"`、`10`、`10.0` は同じになる。
- `DiscNo` の数字判定は ASCII の `[0-9]` だけにする。`\d` は他の Unicode 数字にも一致し、
  `lstrip("0")` と桁数比較では正しく並ばないため（`_ASCII_DIGITS_RE`）。
- 同じ日の開示は項目ごとにマージする（`GroupBy.last` は null を飛ばす）。業績予想の修正が、
  同時に出た決算の値を消さないようにするため。
- 空白だけの文字列は値なし（null）とし、同じ日の開示も前日からの前方補完の値も上書きしない
  （`_blank_strings_as_null`）。
- 最初の開示より前の行は、その時点で分かっている財務値が無いので落とす（`has_disclosure_as_of`）。
- テスト: `tests/test_fundamentals_normalize.py`

## スクリプト（`scripts/`）

- `check_comments.py`: このページ冒頭の規約を検査する。渡されたパスのうち `src/jqqlib` と `scripts`
  の外にあるものは無視するので、lefthook がステージしたファイルの一覧をそのまま渡せる。
- `check_qlib.py`
  - Qlib には JP のリージョン定数が無いため、US 以外の既定である `REG_CN` を
    `QLIB_NON_US_REGION_FOR_JP_DATA` として使う。
  - `qlib.adjustment: none` で作った provider には `factor.day.bin` が無いので、調べた銘柄に factor の
    bin が無ければ `$factor` を読み取り対象から外す（`fields_for_provider`）。
- `dist_smoke.py`
  - リポジトリの外から、`PYTHONPATH` をチェックアウトに向けずに実行する（手順は
    [リリース手順](releasing.md)）。`jqqlib` がインストール済みの wheel から解決されることを確かめるため。
    wheel には `tests/` が入らないので、CI が通るのは CSV のインライン・フォールバックの経路で、
    `tests.support.synthetic` は unittest 側で確かめる。
  - CLI は `python -m jqqlib.pipeline` ではなく、実行中のインタープリタの隣にある `jqqlib` console script
    で動かす（`installed_jqqlib_console_script`）。pip の利用者が使うエントリポイントを検査するため。
  - インストール済みの wheel だけで動かすときの CSV フォールバック（`write_inline_csv`）は、
    `tests.support.synthetic` と同じ列の約束と形で、価格だけを固定値にしたもの。
  - `init-config` の確認は、設定例がチェックアウトだけでなく wheel（package-data）に同梱されて
    いることを確かめるためのもの。
  - Qlib の bin はリトルエンディアンの float32 で、先頭が開始インデックス、その後に値が並ぶ
    （`read_qlib_bin_values`）。既定の `vendor_factor` で dump した場合、各銘柄の最終日の
    `$factor` は 1.0 になる。
