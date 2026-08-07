# Реестр требований — Roadmap → тест → модуль

Иерархия источников истины: (1) подтверждённые Александром бизнес-решения и уточнения Stage 0, (2) актуальные документы Stage 0 (`docs/*`), (3) Roadmap «Дебиторка-бот» v1.3 — для требований, не уточнённых Stage 0. Тесты — планируемые (Stage 0/2/3/5/6), модули — планируемые пути по структуре `docs/IMPLEMENTATION_PLAN.md`. На Stage 0 код не создаётся; таблица фиксирует контракт для последующих этапов.

## Модель файлов и комплектность (Roadmap §4.1, уточнено Stage 0)

| Требование | Тест | Модуль |
|---|---|---|
| Файл = отдел, 5 отделов (включая «Фокин», один менеджер), динамическое число ManagerGroup | `tests/integration/test_excel_persistence_postgres.py::test_confirmed_template_applies_to_every_department` | `app/domain/models/source_file.py` |
| Атрибуция отдела не только по имени файла | `tests/integration/test_bot_upload_flow.py::test_department_selection_saves_new_file` | `app/bot/handlers/upload.py`, `app/bot/keyboards/department.py` |
| 5 отделов, загруженные 5 сообщениями → один аудит | `tests/integration/test_audit_service_stage3.py::test_full_set_completes_only_at_five_of_five` | `app/application/audit_service.py` |
| Отсутствует один отдел → нет автофинализации, показаны недостающие | `tests/integration/test_audit_service_stage3.py::test_full_set_completes_only_at_five_of_five` | `app/application/audit_service.py`, `app/bot/handlers/status.py` |
| Два файла одного отдела → требуется подтверждение замены | `tests/integration/test_bot_upload_flow.py::test_cancel_replacement_keeps_old_file_active` | `app/application/audit_service.py`, `app/bot/handlers/upload.py` |
| Подтверждённая замена файла отдела сохраняет историю active/superseded | `tests/integration/test_bot_upload_flow.py::test_confirmed_replacement_supersedes_old_file` | `app/application/audit_service.py` |
| Точный дубликат по checksum отклоняется глобально, включая superseded | `tests/integration/test_audit_service_stage3.py::test_superseded_sha_remains_globally_duplicate` | `app/application/audit_service.py`, `app/domain/models/source_file.py` |
| Разные отчётные даты → разные циклы с подтверждением второго | `tests/integration/test_bot_upload_flow.py::test_second_date_requires_confirmation_before_cycle_creation` | `app/application/audit_service.py`, `app/bot/handlers/upload.py` |
| Параллельные 4-й и 5-й файлы атомарно завершают цикл | `tests/integration/test_audit_service_stage3.py::test_concurrent_fourth_and_fifth_files_complete_cycle` | `app/application/audit_service.py` |
| Конкурентное создание одной даты создаёт ровно один AuditCycle | `tests/integration/test_audit_service_stage3.py::test_concurrent_cycle_creation_creates_exactly_one_cycle` | `app/application/audit_service.py` |
| Один active-файл отдела на цикл защищён PostgreSQL partial unique index | `tests/integration/test_audit_service_stage3.py::test_partial_unique_index_rejects_two_active_department_files` | `alembic/versions/a3c100000001_stage3_weekly_audit_cycle.py` |
| Read-only lookup не оставляет autobegin и не возвращает expired ORM | `tests/integration/test_audit_service_stage3.py::test_read_dtos_are_safe_with_expire_on_commit_and_legacy_rows` | `app/application/audit_service.py` |
| `/status` показывает все открытые collecting-циклы (новые первыми) и до 3 последних completed | `tests/integration/test_bot_upload_flow.py::test_status_shows_all_collecting_cycles_and_recent_completed` | `app/bot/handlers/status.py`, `app/application/audit_service.py` |
| Callback-токен недействителен после рестарта бота / очистки FSM (`MemoryStorage`) | `tests/integration/test_bot_upload_flow.py::test_callback_token_rejected_after_state_cleared_like_bot_restart` | `app/bot/handlers/upload.py`, `app/main.py` |
| IntegrityError retry открывает новую транзакцию; сессия остаётся пригодной | `tests/integration/test_audit_service_stage3.py::test_integrity_error_retry_opens_new_transaction_and_keeps_session_usable` | `app/application/audit_service.py` |
| Замена атомарна: при сбое до insert старый файл остаётся ACTIVE | `tests/integration/test_audit_service_stage3.py::test_replace_never_commits_superseded_without_new_active_file` | `app/application/audit_service.py` |
| Изменение числа ManagerGroup — не ошибка | `tests/unit/domain/test_manager_group_count_dynamic.py` | `app/domain/models/manager_group.py` |
| ManagerGroup как организационная группа/филиал, не только ФИО | `tests/unit/domain/test_manager_group_opaque_name.py` | `app/domain/models/manager_group.py` |
| Стабильная идентичность ManagerGroup между двумя аудитами (один и тот же `manager_group_id` по каноническому ключу `department_id + normalized_name`, не новый ID на каждый цикл) | `tests/integration/test_manager_group_stable_across_cycles.py` | `app/domain/models/manager_group.py`, `app/application/audit_service.py` |
| Восстановление незавершённого аудита после перезапуска (неполный комплект) | Stage 3, часть 2 (ещё не реализовано) | будущий `recovery.py` |
| Восстановление аудита после перезапуска (полный, но не финализированный) | Stage 3, часть 2 (ещё не реализовано) | будущий `recovery.py` |
| Idle timeout, напоминания и присвоение expired | Stage 3, часть 2 (ещё не реализовано) | будущий scheduler/recovery-модуль |
| Неполный комплект после timeout не переходит в completed | Stage 3, часть 2 (ещё не реализовано) | `app/application/audit_service.py` |

## Excel-парсер и валидация (Roadmap §4.2, §6, уточнено Stage 0)

| Требование | Тест | Модуль |
|---|---|---|
| 17-колоночный fingerprint отдела «Региональный» | `tests/unit/excel/test_fingerprint_regional.py` | `app/infrastructure/excel/fingerprint.py` |
| Позиционная (не по названию) идентификация колонок | `tests/unit/excel/test_column_position_matching.py` | `app/infrastructure/excel/column_map.py` |
| Многострочные/объединённые заголовки | `tests/unit/excel/test_multirow_header.py` | `app/infrastructure/excel/header_parser.py` |
| Скрытая колонка не считается отсутствующей | `tests/unit/excel/test_hidden_column_ignored.py` | `app/infrastructure/excel/column_map.py` |
| Визуально сжатая колонка (малая ширина) не считается отсутствующей | `tests/unit/excel/test_narrow_column_ignored.py` | `app/infrastructure/excel/column_map.py` |
| outline levels 0–4 распознаются; уровни 2–4 (договор/объект/документ) сохраняются в модели с родительскими связями, не выбрасываются | `tests/unit/excel/test_outline_levels.py` | `app/infrastructure/excel/row_classifier.py` |
| Иерархия ManagerGroup → контрагент → договор → объект → документ сохраняется целиком (детализация доступна для отчёта) | `tests/unit/domain/test_hierarchy_preserved.py` | `app/domain/models/debt_snapshot.py` |
| Уровни 2–4 исключены из reconciliation агрегатов — нет двойного суммирования агрегированной и вложенной суммы одной и той же метрики | `tests/unit/calculations/test_no_double_counting_nested_rows.py` | `app/domain/calculations/reconciliation.py` |
| Исключение псевдо-level1 строк шапки (Параметры/Отбор) | `tests/unit/excel/test_header_pseudo_rows_excluded.py` | `app/infrastructure/excel/row_classifier.py` |
| Файл с 12 колонками вместо 17 отклоняется | `tests/unit/excel/test_invalid_column_count_rejected.py` (fixture `invalid_2026-07-15_missing_columns`) | `app/infrastructure/excel/validator.py` |
| Reconciliation «Итого» по метрикам, не блоком | `tests/unit/calculations/test_totals_reconciliation_per_metric.py` | `app/domain/calculations/reconciliation.py` |
| Расхождение «Итого» по «Сумме кредита» — диагностика, не отказ | `tests/unit/calculations/test_credit_limit_diagnostic_mismatch.py` (fixtures на основе `regional_2026-07-01`, `regional_2026-07-08`) | `app/domain/calculations/reconciliation.py` |
| Fallback `not_due = total_debt − Σ buckets` — только для legacy/alternative template или другого подтверждённого fingerprint, не для regional | `tests/unit/calculations/test_not_due_fallback_scope_restricted.py` | `app/domain/calculations/debt.py` |
| Regional-файл без физической колонки «Не просрочено» отклоняется по fingerprint, fallback не применяется | `tests/unit/excel/test_regional_missing_not_due_column_rejected.py` | `app/infrastructure/excel/validator.py` |
| Отрицательный `not_due` (там, где fallback применим) — ошибка | `tests/unit/calculations/test_not_due_negative_error.py` | `app/domain/calculations/debt.py` |
| «Отсрочка платежа» — неаддитивная метрика, проверка на уровне записи, не суммой | `tests/unit/calculations/test_payment_deferral_not_summed.py` | `app/domain/calculations/reconciliation.py` |
| Извлечение raw-комментария из безымянной правой колонки | `tests/unit/excel/test_comment_column_extraction.py` | `app/infrastructure/excel/comment_extractor.py` |
| Regression fixture `legacy_parser_fixture.xls` (3 менеджерские группы, 110 контрагентов, 658 вложенных, 15 комментариев) | `tests/unit/excel/test_legacy_regression_fixture.py` | `app/infrastructure/excel/legacy_xls_reader.py` |
| Повреждённый/неподдерживаемый файл | `tests/unit/excel/test_corrupted_file_rejected.py` | `app/infrastructure/excel/reader.py` |

## Контрагенты и matching (Roadmap §4.4, переписано Stage 0 по решению Александра)

| Требование | Тест | Модуль |
|---|---|---|
| Ключ идентичности: department + manager_group + normalized_name | `tests/unit/matching/test_counterparty_identity_key.py` | `app/domain/matching/identity.py` |
| Одноимённые контрагенты у разных ManagerGroup — независимые сущности | `tests/unit/matching/test_same_name_different_manager_independent.py` | `app/domain/matching/identity.py` |
| Кредитные лимиты одноимённых компаний разных ManagerGroup суммируются раздельно | `tests/unit/calculations/test_credit_limit_independent_sum.py` | `app/domain/calculations/credit_limit.py` |
| Fuzzy matching только для кандидатов, без автообъединения | `tests/unit/matching/test_fuzzy_candidates_only.py` | `app/domain/matching/fuzzy.py` |
| Нормализация: NFKC, trim, регистр, кавычки, ОПФ | `tests/unit/matching/test_normalization.py` | `app/domain/matching/normalization.py` |
| Неоднозначное сопоставление требует подтверждения | `tests/integration/test_matching_ambiguous_requires_confirmation.py` | `app/application/comparison_service.py` |
| Исчезновение компании оценивается только по полному комплекту 5 отделов | `tests/integration/test_missing_counterparty_full_set_only.py` | `app/application/comparison_service.py` |
| Смена ManagerGroup/отдела не объединяется автоматически | `tests/integration/test_manager_or_department_change_not_auto_merged.py` | `app/application/comparison_service.py` |

## Сравнение недель (Roadmap §7)

| Требование | Тест | Модуль |
|---|---|---|
| Изменение долга, чистое снижение/рост долга | `tests/unit/calculations/test_delta_formulas.py` | `app/domain/calculations/comparison.py` |
| Ключ сопоставления между неделями: department+manager_group+name | `tests/integration/test_weekly_matching_key.py` | `app/application/comparison_service.py` |
| Воспроизводимый ComparisonResult | `tests/integration/test_comparison_result_reproducible.py` | `app/application/comparison_service.py` |
| Acceptance: 2 полных комплекта (10 файлов, 5 отделов × 2 даты) | `tests/acceptance/test_two_full_weekly_sets.py` | `app/application/comparison_service.py` |

## Обещания и алерты (Roadmap §4.5, §6.1, §7.2)

| Требование | Тест | Модуль |
|---|---|---|
| Детерминированный парсинг даты/суммы/типа обещания | `tests/unit/domain/test_promise_parser.py` | `app/domain/calculations/promise_parser.py` |
| Правило года для даты без года (в т.ч. через Новый год) | `tests/unit/domain/test_promise_year_rollover.py` | `app/domain/calculations/promise_parser.py` |
| Статусы: не наступил/выполнено/частично/не выполнено/ошибка данных | `tests/unit/domain/test_promise_status.py` | `app/application/promise_service.py` |
| LLM только fallback, structured output, без полного Excel | `tests/unit/infrastructure/test_llm_fallback_contract.py` | `app/infrastructure/llm/client.py` |
| Приоритетный alert-блок перед полным отчётом | `tests/unit/application/test_alert_block_ordering.py` | `app/application/report_service.py` |
| Новый критический долг >31 дня | `tests/unit/application/test_new_critical_debt_alert.py` | `app/application/report_service.py` |

## Отчётность (Roadmap §4, уточнено Stage 0 — уровни агрегации)

| Требование | Тест | Модуль |
|---|---|---|
| Агрегация: общий итог → отдел → ManagerGroup → контрагент → договор → документ | `tests/unit/application/test_report_aggregation_levels.py` | `app/application/report_service.py` |
| Разбиение длинного отчёта на сообщения Telegram | `tests/unit/application/test_report_message_splitting.py` | `app/application/report_service.py` |
| Формулировка «чистое снижение долга», не «оплата» | `tests/unit/application/test_report_wording_net_reduction.py` | `app/application/report_service.py` |

## Безопасность и эксплуатация (Roadmap §7 чек-лист, §8 тесты)

| Требование | Тест | Модуль |
|---|---|---|
| Allowlist по Telegram user_id, запрет групповых чатов | `tests/integration/test_allowlist_middleware.py` | `app/bot/middlewares/auth.py` |
| Отсутствие коммерческих данных в логах | `tests/unit/infrastructure/test_log_redaction.py` | `app/infrastructure/telegram/logging.py` |
| Гарантированное удаление временных файлов | `tests/integration/test_temp_file_cleanup.py` | `app/infrastructure/storage/tempfiles.py` |
| Cleanup после аварийного перезапуска | `tests/integration/test_startup_cleanup.py` | `app/infrastructure/storage/tempfiles.py` |
| Секреты только через env, не в репозитории | `tests/unit/config/test_no_hardcoded_secrets.py` | `app/config/settings.py` |
| Только Decimal для денег (запрет float) | `tests/unit/domain/test_no_float_for_money.py` | весь `app/domain/calculations/` |

## Сводка обязательных сценариев Stage 3 (ревизия 4)

Все 20 сценариев из ревизии 2 сохранены (строки в разделах «Модель файлов и комплектность» и «Excel-парсер и валидация» / «Контрагенты и matching» выше). Сценарий №21 «Стабильная идентичность ManagerGroup между двумя аудитами» (ревизия 3) сохранён. Сценарий №22 «Сохранение иерархии ManagerGroup → контрагент → договор → объект → документ без двойного суммирования» добавлен в ревизии 4 (см. раздел «Excel-парсер и валидация»).

Примечание: пути модулей и тестов — планируемые ориентиры по структуре Roadmap §5, актуализируются на каждом этапе по правилу §9.13 Roadmap.

**Обновление 30.07.2026:** состав отделов расширен с 4 до 5 (добавлен «Фокин», см. `docs/DATA_CONTRACT.md` §2.4) — количество сценариев и их формулировки не изменились, обновлены только числовые упоминания состава отделов («4»→«5», «8 файлов»→«10 файлов» и т.п.) в строках выше.
