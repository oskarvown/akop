"""Telegram FSM flow for receiving and assigning weekly Excel files."""
from __future__ import annotations

import logging
import tempfile
from html import escape
from pathlib import Path
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import (
    AddResult,
    AuditCycleNotFoundError,
    CycleImmutableError,
    DepartmentSlotTakenError,
    DuplicateSourceFileError,
    SourceFileNotFoundError,
    StaleReplacementError,
    WithdrawNotAllowedError,
    add_source_file_atomic,
    count_collecting_cycles,
    find_audit_cycle_by_report_date,
    find_source_file_by_sha256,
    get_active_source_file,
    replace_source_file_atomic,
    withdraw_source_file_atomic,
)
from app.bot.keyboards.confirm import (
    department_confirm_keyboard,
    new_cycle_keyboard,
    replacement_keyboard,
    undo_confirm_keyboard,
    undo_offer_keyboard,
)
from app.bot.keyboards.department import DEPARTMENT_LABELS, department_keyboard
from app.config import get_settings
from app.domain.enums import Department
from app.domain.models import AuditCycleStatus, SourceFileLifecycle
from app.infrastructure.excel.checksum import compute_sha256
from app.infrastructure.excel.validator import (
    ValidationResult,
    validate_confirmed_template_file,
)

logger = logging.getLogger(__name__)


class UploadStates(StatesGroup):
    choosing_department = State()
    confirming_department = State()
    confirming_new_cycle = State()
    confirming_replace = State()
    can_undo = State()
    confirming_undo = State()


async def handle_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Загрузка отменена.")


async def handle_undo(message: Message, state: FSMContext) -> None:
    """Open undo confirmation; never executes withdraw immediately."""
    current = await state.get_state()
    if current not in {
        UploadStates.can_undo.state,
        UploadStates.confirming_undo.state,
    }:
        await message.answer(
            "Сейчас нечего отменять. Отмена доступна сразу после сохранения "
            "файла в открытый сбор (до следующей загрузки или /cancel)."
        )
        return
    await _prompt_undo_confirmation(message, state)


async def handle_document(
    message: Message,
    state: FSMContext,
    bot: Bot,
    session: AsyncSession,
) -> None:
    document = message.document
    if document is None:
        return

    current_state = await state.get_state()
    if current_state == UploadStates.can_undo.state:
        # New upload abandons the undo buffer for the previous save.
        await state.clear()
        current_state = None

    if current_state is not None:
        data = await state.get_data()
        filename = escape(str(data.get("original_filename", "предыдущий файл")))
        await message.answer(
            f"Предыдущая загрузка ({filename}) ещё не завершена — сначала "
            "выберите для неё отдел или отмените (/cancel)."
        )
        return

    original_filename = document.file_name or "upload"
    suffix = Path(original_filename).suffix.lower()
    if suffix not in {".xls", ".xlsx"}:
        await message.answer("Поддерживаются только Excel-файлы .xls и .xlsx.")
        return

    max_size = get_settings().max_upload_size_bytes
    if document.file_size is not None and document.file_size > max_size:
        await message.answer(
            f"Файл слишком большой. Максимальный размер — {max_size // (1024 * 1024)} МБ."
        )
        return

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temporary:
            temp_path = Path(temporary.name)
        await bot.download(document, destination=temp_path)
        sha256 = compute_sha256(temp_path)
        result = validate_confirmed_template_file(temp_path)
    except Exception:
        logger.exception("Не удалось обработать загруженный Excel-файл")
        await state.clear()
        await message.answer("Произошла ошибка, попробуйте загрузить файл снова.")
        return
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    if not result.is_valid or result.parsed is None:
        reasons = "\n".join(
            f"• {escape(reason)}" for reason in result.rejection_reasons
        )
        await message.answer(f"Файл отклонён:\n{reasons}")
        return

    try:
        duplicate = await find_source_file_by_sha256(session, sha256)
    except Exception:
        logger.exception("Не удалось проверить SHA-256 загруженного файла")
        await state.clear()
        await message.answer("Произошла ошибка, попробуйте загрузить файл снова.")
        return

    if duplicate is not None:
        if duplicate.audit_cycle_id is None:
            await message.answer(
                "Файл уже загружен ранее, но не привязан к недельному циклу."
            )
        else:
            lifecycle_note = (
                " Он был заменён и хранится в истории."
                if duplicate.lifecycle_status == SourceFileLifecycle.SUPERSEDED
                else ""
            )
            await message.answer(
                "Файл уже загружен: "
                f"{DEPARTMENT_LABELS[duplicate.department]}, "
                f"{duplicate.report_date:%d.%m.%Y}.{lifecycle_note}"
            )
        return

    upload_token = uuid4().hex[:12]
    await state.set_state(UploadStates.choosing_department)
    await state.set_data(
        {
            "upload_token": upload_token,
            "result": result,
            "sha256": sha256,
            "original_filename": original_filename,
            "report_date": result.parsed.report_date,
        }
    )
    await message.answer(
        f"Файл валиден. Дата отчёта: {result.parsed.report_date:%d.%m.%Y}.\n"
        "Выберите отдел:",
        reply_markup=department_keyboard(upload_token),
    )


async def handle_department_callback(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    try:
        if await state.get_state() != UploadStates.choosing_department.state:
            await _callback_message(callback, "Эта кнопка устарела. Загрузите файл заново.")
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            await _callback_message(callback, "Некорректная кнопка.")
            return
        _, token, department_value = parts
        data = await state.get_data()
        if data.get("upload_token") != token:
            await _callback_message(callback, "Эта кнопка устарела. Загрузите файл заново.")
            return

        try:
            department = Department(department_value)
        except ValueError:
            await _callback_message(callback, "Неизвестный отдел.")
            return

        report_date = data["report_date"]
        cycle = await find_audit_cycle_by_report_date(session, report_date)
        if cycle is not None and cycle.status != AuditCycleStatus.COLLECTING:
            await state.clear()
            await _callback_message(
                callback,
                f"Аудит за {report_date:%d.%m.%Y} уже завершён; загрузка заблокирована.",
            )
            return

        await state.update_data(department=department.value)
        await state.set_state(UploadStates.confirming_department)
        filename = escape(str(data.get("original_filename", "файл")))
        result = data.get("result")
        debt = "не указан"
        if isinstance(result, ValidationResult) and result.parsed is not None:
            total = result.parsed.grand_total.total_debt
            if total is not None:
                debt = f"{total:,.2f}"
        await _callback_message(
            callback,
            f"Назначить файл «{filename}» отделу "
            f"{DEPARTMENT_LABELS[department]} "
            f"(дата отчёта {report_date:%d.%m.%Y}, долг: {debt})?",
            reply_markup=department_confirm_keyboard(token),
        )
    except Exception:
        logger.exception("Ошибка обработки выбора отдела")
        await state.clear()
        await _callback_message(
            callback, "Произошла ошибка, попробуйте загрузить файл снова."
        )
    finally:
        await callback.answer()


async def handle_department_confirm_callback(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    try:
        if await state.get_state() != UploadStates.confirming_department.state:
            await _callback_message(callback, "Эта кнопка устарела. Загрузите файл заново.")
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            await _callback_message(callback, "Некорректная кнопка.")
            return
        _, token, action = parts
        data = await state.get_data()
        if data.get("upload_token") != token:
            await _callback_message(callback, "Эта кнопка устарела. Загрузите файл заново.")
            return

        if action == "cancel":
            await state.clear()
            await _callback_message(callback, "Загрузка отменена.")
            return
        if action == "other":
            await state.set_state(UploadStates.choosing_department)
            await state.update_data(department=None)
            await _callback_message(
                callback,
                "Выберите другой отдел:",
                reply_markup=department_keyboard(token),
            )
            return
        if action != "confirm":
            await _callback_message(callback, "Некорректное действие.")
            return

        department = Department(str(data["department"]))
        report_date = data["report_date"]
        cycle = await find_audit_cycle_by_report_date(session, report_date)  # type: ignore[arg-type]
        if cycle is not None and cycle.status != AuditCycleStatus.COLLECTING:
            await state.clear()
            await _callback_message(
                callback,
                f"Аудит за {report_date:%d.%m.%Y} уже завершён; загрузка заблокирована.",
            )
            return

        if cycle is None:
            collecting = await count_collecting_cycles(session)
            if collecting:
                dates = ", ".join(
                    item.report_date.strftime("%d.%m.%Y") for item in collecting
                )
                await state.set_state(UploadStates.confirming_new_cycle)
                await _callback_message(
                    callback,
                    f"Уже открыт сбор за {dates}. Этот файл — за "
                    f"{report_date:%d.%m.%Y} и создаст отдельный цикл. Продолжить?",
                    reply_markup=new_cycle_keyboard(token),
                )
                return

        await _save_or_prompt_replacement(
            callback=callback,
            state=state,
            session=session,
            cycle_id=cycle.id if cycle is not None else None,
            department=department,
        )
    except Exception:
        logger.exception("Ошибка подтверждения отдела")
        await state.clear()
        await _callback_message(
            callback, "Произошла ошибка, попробуйте загрузить файл снова."
        )
    finally:
        await callback.answer()


async def handle_new_cycle_callback(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    try:
        if await state.get_state() != UploadStates.confirming_new_cycle.state:
            await _callback_message(callback, "Эта кнопка устарела. Загрузите файл заново.")
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            await _callback_message(callback, "Некорректная кнопка.")
            return
        _, token, action = parts
        data = await state.get_data()
        if data.get("upload_token") != token:
            await _callback_message(callback, "Эта кнопка устарела. Загрузите файл заново.")
            return
        if action == "cancel":
            await state.clear()
            await _callback_message(callback, "Создание нового цикла отменено.")
            return
        if action != "confirm":
            await _callback_message(callback, "Некорректное действие.")
            return

        department = Department(data["department"])
        await _persist_add(callback, state, session, data, department)
    except Exception:
        logger.exception("Ошибка подтверждения нового недельного цикла")
        await state.clear()
        await _callback_message(
            callback, "Произошла ошибка, попробуйте загрузить файл снова."
        )
    finally:
        await callback.answer()


async def handle_replace_callback(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    try:
        if await state.get_state() != UploadStates.confirming_replace.state:
            await _callback_message(callback, "Эта кнопка устарела. Загрузите файл заново.")
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            await _callback_message(callback, "Некорректная кнопка.")
            return
        _, token, action = parts
        data = await state.get_data()
        if data.get("upload_token") != token:
            await _callback_message(callback, "Эта кнопка устарела. Загрузите файл заново.")
            return
        if action == "cancel":
            await state.clear()
            await _callback_message(callback, "Оставляю старый файл.")
            return
        if action != "confirm":
            await _callback_message(callback, "Некорректное действие.")
            return

        result: ValidationResult = data["result"]  # type: ignore[assignment]
        department = Department(data["department"])
        try:
            add_result = await replace_source_file_atomic(
                session,
                result=result,
                department=department,
                sha256=data["sha256"],
                original_filename=data["original_filename"],
                report_date=data["report_date"],
                expected_active_source_file_id=data["expected_active_source_file_id"],
            )
        except StaleReplacementError:
            await state.clear()
            await _callback_message(
                callback,
                "Файл отдела уже изменился после показа кнопки. "
                "Проверьте /status и загрузите файл повторно.",
            )
            return
        except (
            CycleImmutableError,
            DuplicateSourceFileError,
            DepartmentSlotTakenError,
            AuditCycleNotFoundError,
        ) as exc:
            await state.clear()
            await _callback_message(callback, _business_error_message(exc))
            return

        reply_markup = await _finalize_save(state, data, add_result)
        await _callback_message(
            callback, _success_message(add_result), reply_markup=reply_markup
        )
    except Exception:
        logger.exception("Ошибка подтверждения замены файла")
        await state.clear()
        await _callback_message(
            callback, "Произошла ошибка, попробуйте загрузить файл снова."
        )
    finally:
        await callback.answer()


async def handle_undo_offer_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    try:
        if await state.get_state() != UploadStates.can_undo.state:
            await _callback_message(callback, "Эта кнопка устарела. Загрузите файл заново.")
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            await _callback_message(callback, "Некорректная кнопка.")
            return
        _, token, action = parts
        data = await state.get_data()
        if data.get("undo_token") != token:
            await _callback_message(callback, "Эта кнопка устарела. Загрузите файл заново.")
            return
        if action != "start":
            await _callback_message(callback, "Некорректное действие.")
            return
        await _prompt_undo_confirmation(callback.message, state)  # type: ignore[arg-type]
    except Exception:
        logger.exception("Ошибка предложения отмены")
        await state.clear()
        await _callback_message(
            callback, "Произошла ошибка. Проверьте /status."
        )
    finally:
        await callback.answer()


async def handle_undo_confirm_callback(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    try:
        if await state.get_state() != UploadStates.confirming_undo.state:
            await _callback_message(callback, "Эта кнопка устарела. Загрузите файл заново.")
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            await _callback_message(callback, "Некорректная кнопка.")
            return
        _, token, action = parts
        data = await state.get_data()
        if data.get("undo_token") != token:
            await _callback_message(callback, "Эта кнопка устарела. Загрузите файл заново.")
            return

        if action == "keep":
            # Stay undo-capable with a fresh token so repeated old buttons fail.
            undo_token = uuid4().hex[:12]
            await state.set_state(UploadStates.can_undo)
            await state.update_data(undo_token=undo_token)
            await _callback_message(
                callback,
                "Файл оставлен без изменений.",
                reply_markup=undo_offer_keyboard(undo_token),
            )
            return
        if action != "confirm":
            await _callback_message(callback, "Некорректное действие.")
            return

        source_file_id = data.get("undo_source_file_id")
        if not isinstance(source_file_id, int):
            await state.clear()
            await _callback_message(
                callback, "Данные для отмены устарели. Загрузите файл снова."
            )
            return
        previous_id = data.get("undo_previous_source_file_id")
        previous_source_file_id = previous_id if isinstance(previous_id, int) else None

        try:
            withdraw = await withdraw_source_file_atomic(
                session,
                source_file_id=source_file_id,
                previous_source_file_id=previous_source_file_id,
            )
        except (
            SourceFileNotFoundError,
            WithdrawNotAllowedError,
            CycleImmutableError,
            AuditCycleNotFoundError,
        ) as exc:
            await state.clear()
            await _callback_message(callback, _withdraw_error_message(exc))
            return

        upload_token = uuid4().hex[:12]
        await state.set_state(UploadStates.choosing_department)
        await state.set_data(
            {
                "upload_token": upload_token,
                "result": data["result"],
                "sha256": data["sha256"],
                "original_filename": data["original_filename"],
                "report_date": data["report_date"],
            }
        )
        restored_note = ""
        if withdraw.restored_source_file_id is not None:
            restored_note = " Предыдущий файл отдела восстановлен."
        await _callback_message(
            callback,
            f"Снял файл с отдела {DEPARTMENT_LABELS[withdraw.department]} "
            f"(аудит {withdraw.report_date:%d.%m.%Y}, "
            f"сейчас {len(withdraw.summary.present)}/5).{restored_note}\n"
            "Выберите правильный отдел:",
            reply_markup=department_keyboard(upload_token),
        )
    except Exception:
        logger.exception("Ошибка подтверждения отмены")
        await state.clear()
        await _callback_message(
            callback, "Не удалось отменить назначение. Проверьте /status."
        )
    finally:
        await callback.answer()


async def _prompt_undo_confirmation(
    target: Message | None,
    state: FSMContext,
) -> None:
    if target is None:
        return
    undo_token = uuid4().hex[:12]
    await state.set_state(UploadStates.confirming_undo)
    await state.update_data(undo_token=undo_token)
    await target.answer(
        "Отменить последнюю загрузку? Файл останется в истории как отозванный, "
        "слот отдела освободится.",
        reply_markup=undo_confirm_keyboard(undo_token),
    )


async def _save_or_prompt_replacement(
    *,
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    cycle_id: int | None,
    department: Department,
) -> None:
    active = (
        await get_active_source_file(session, cycle_id, department)
        if cycle_id is not None
        else None
    )
    if active is None:
        data = await state.get_data()
        await _persist_add(callback, state, session, data, department)
        return

    token = (await state.get_data())["upload_token"]
    await state.update_data(expected_active_source_file_id=active.id)
    await state.set_state(UploadStates.confirming_replace)
    debt = f"{active.total_debt:,.2f}" if active.total_debt is not None else "не указан"
    await _callback_message(
        callback,
        f"У отдела {DEPARTMENT_LABELS[department]} уже есть файл "
        f"(долг: {debt}). Заменить?",
        reply_markup=replacement_keyboard(token),
    )


async def _persist_add(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    data: dict[str, object],
    department: Department,
) -> None:
    result = data["result"]
    if not isinstance(result, ValidationResult):
        raise TypeError("FSM contains an invalid validation result")
    try:
        add_result = await add_source_file_atomic(
            session,
            result=result,
            department=department,
            sha256=str(data["sha256"]),
            original_filename=str(data["original_filename"]),
            report_date=data["report_date"],  # type: ignore[arg-type]
            notification_chat_id=get_settings().audit_notification_chat_id,
        )
    except (
        CycleImmutableError,
        DuplicateSourceFileError,
        DepartmentSlotTakenError,
    ) as exc:
        await state.clear()
        await _callback_message(callback, _business_error_message(exc))
        return

    reply_markup = await _finalize_save(state, data, add_result)
    await _callback_message(
        callback, _success_message(add_result), reply_markup=reply_markup
    )


async def _finalize_save(
    state: FSMContext,
    data: dict[str, object],
    add_result: AddResult,
) -> object | None:
    """Arm undo only while cycle stays COLLECTING; never after 5/5 COMPLETED."""
    if add_result.status != AuditCycleStatus.COLLECTING:
        await state.clear()
        return None

    undo_token = uuid4().hex[:12]
    await state.set_state(UploadStates.can_undo)
    await state.set_data(
        {
            "upload_token": data.get("upload_token"),
            "result": data["result"],
            "sha256": data["sha256"],
            "original_filename": data["original_filename"],
            "report_date": data["report_date"],
            "undo_token": undo_token,
            "undo_source_file_id": add_result.source_file_id,
            "undo_previous_source_file_id": add_result.previous_source_file_id,
        }
    )
    return undo_offer_keyboard(undo_token)


def _business_error_message(exc: Exception) -> str:
    if isinstance(exc, CycleImmutableError):
        return (
            f"Аудит за {exc.report_date:%d.%m.%Y} уже закрыт; "
            "загрузка заблокирована. Проверьте /status."
        )
    if isinstance(exc, DuplicateSourceFileError):
        return "Этот файл уже был загружен. Проверьте /status."
    if isinstance(exc, DepartmentSlotTakenError):
        return "Файл этого отдела уже обработан параллельно. Проверьте /status."
    if isinstance(exc, AuditCycleNotFoundError):
        return "Недельный цикл больше не найден. Проверьте /status."
    return "Операцию выполнить не удалось. Проверьте /status."


def _withdraw_error_message(exc: Exception) -> str:
    if isinstance(exc, SourceFileNotFoundError):
        return "Файл для отмены уже не найден. Проверьте /status."
    if isinstance(exc, CycleImmutableError):
        return (
            f"Аудит за {exc.report_date:%d.%m.%Y} уже закрыт; "
            "отмена недоступна. Проверьте /status."
        )
    if isinstance(exc, WithdrawNotAllowedError):
        return f"Отмена недоступна: {exc}. Проверьте /status."
    if isinstance(exc, AuditCycleNotFoundError):
        return "Недельный цикл больше не найден. Проверьте /status."
    return "Отмену выполнить не удалось. Проверьте /status."


def _success_message(result: AddResult) -> str:
    debt = (
        f", долг {result.total_debt:,.2f}"
        if result.total_debt is not None
        else ""
    )
    if result.status == AuditCycleStatus.COMPLETED:
        return (
            f"Аудит за {result.report_date:%d.%m.%Y}: 5/5 — комплект собран{debt}."
        )
    return (
        f"Аудит за {result.report_date:%d.%m.%Y}: "
        f"{len(result.summary.present)}/5 файлов получено{debt}.\n"
        "Если отдел выбран ошибочно — нажмите кнопку ниже или /undo."
    )


async def _callback_message(
    callback: CallbackQuery,
    text: str,
    *,
    reply_markup: object | None = None,
) -> None:
    if callback.message is not None:
        await callback.message.answer(text, reply_markup=reply_markup)


def get_upload_router() -> Router:
    router = Router(name="upload")
    router.message.register(handle_cancel, Command("cancel"))
    router.message.register(handle_undo, Command("undo"))
    router.message.register(handle_document, F.document)
    # Longer prefixes before shorter ones (deptconfirm before dept, etc.).
    router.callback_query.register(
        handle_department_confirm_callback,
        F.data.startswith("deptconfirm:"),
    )
    router.callback_query.register(
        handle_department_callback,
        F.data.startswith("dept:"),
    )
    router.callback_query.register(
        handle_new_cycle_callback,
        F.data.startswith("newcycle:"),
    )
    router.callback_query.register(
        handle_replace_callback,
        F.data.startswith("replace:"),
    )
    router.callback_query.register(
        handle_undo_offer_callback,
        F.data.startswith("undooffer:"),
    )
    router.callback_query.register(
        handle_undo_confirm_callback,
        F.data.startswith("undoconfirm:"),
    )
    return router
