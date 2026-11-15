import asyncio
import html
import io
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    Message, ReplyKeyboardMarkup,
)

import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ssm")

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Dushanbe"))
LOCK = asyncio.Lock()
_last_empty_alert = 0.0
EMPTY_ALERT_COOLDOWN = 6 * 3600

admin_router = Router()
admin_router.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))
user_router = Router()


class Pay(StatesGroup):
    receipt = State()


class Adm(StatesGroup):
    price_tjs = State()
    price_rub = State()
    duration = State()
    add = State()
    delete = State()
    det_tjs = State()
    det_rub = State()


# ---------------- helpers ----------------
def esc(x) -> str:
    return html.escape(str(x))


def num(d) -> str:
    return format(Decimal(str(d)).normalize(), "f")


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def fmt_left(dt: datetime) -> str:
    sec = max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))
    h, rem = divmod(sec, 3600)
    return f"{h} ч {rem // 60} мин"


def parse_lines(text: str) -> list[str]:
    seen, out = set(), []
    for line in text.splitlines():
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return out


async def send_long(msg: Message, text: str):
    for i in range(0, len(text), 3900):
        await msg.answer(text[i:i + 3900])


async def notify_admins(bot: Bot, text: str):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text)
        except Exception as e:
            log.warning("admin %s notify failed: %s", aid, e)


def menu_kb(uid: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="💎 Тариф")],
        [KeyboardButton(text="📊 Моя подписка"), KeyboardButton(text="📜 История")],
    ]
    if uid in ADMIN_IDS:
        rows.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def tariff_text(s: dict) -> str:
    h, per = int(s["duration_hours"]), int(s["per_hour"])
    return (
        "💎 <b>ТАРИФ: 1 LIMIT</b>\n\n"
        "💰 Стоимость:\n"
        f"- {num(s['price_tjs'])} сомони\n"
        f"- {num(s['price_rub'])} рублей\n\n"
        f"⏰ Продолжительность: {h} ч.\n\n"
        "📦 Выдача аккаунтов:\n"
        f"- {per} акк. каждый час\n"
        f"- {per * h} акк. за {h} ч."
    )


# ---------------- user handlers ----------------
@user_router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await db.upsert_user(m.from_user.id, m.from_user.username, m.from_user.full_name)
    await m.answer(
        "👋 Добро пожаловать в SSM ACCOUNT BOT!\nВыберите действие в меню.",
        reply_markup=menu_kb(m.from_user.id),
    )


@user_router.message(F.text == "💎 Тариф")
async def show_tariff(m: Message, state: FSMContext):
    await state.clear()
    await db.upsert_user(m.from_user.id, m.from_user.username, m.from_user.full_name)
    s = await db.get_settings()
    if s["plan_enabled"] != "1":
        await m.answer("⛔ Тариф временно недоступен.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {num(s['price_tjs'])} сомони", callback_data="pay:TJS")],
        [InlineKeyboardButton(text=f"💳 Оплатить {num(s['price_rub'])} руб.", callback_data="pay:RUB")],
    ])
    await m.answer(tariff_text(s), reply_markup=kb)


@user_router.message(F.text == "📊 Моя подписка")
async def my_sub(m: Message, state: FSMContext):
    await state.clear()
    sub = await db.active_sub(m.from_user.id)
    if not sub:
        await m.answer("У вас нет активной подписки. Нажмите «💎 Тариф».")
        return
    text = (
        "📊 <b>Ваша подписка</b>\n\n"
        f"⏳ Осталось: {fmt_left(sub['expires_at'])}\n"
        f"📅 Действует до: {fmt_dt(sub['expires_at'])}\n"
        f"📦 Выдано аккаунтов: {sub['issued_count']}\n"
    )
    if sub["next_issue_at"] < sub["expires_at"]:
        nxt = max(sub["next_issue_at"], datetime.now(timezone.utc))
        text += f"🕒 Следующая выдача: {fmt_dt(nxt)}"
    else:
        text += "Все выдачи по подписке завершены."
    await m.answer(text)


@user_router.message(F.text == "📜 История")
async def my_history(m: Message, state: FSMContext):
    await state.clear()
    rows = await db.history(m.from_user.id, 30)
    if not rows:
        await m.answer("История пуста.")
        return
    lines = [f"{fmt_dt(r['issued_at'])} — <code>{esc(r['data'])}</code>" for r in rows]
    await send_long(m, "📜 <b>Последние выданные аккаунты:</b>\n\n" + "\n".join(lines))


@user_router.callback_query(F.data.startswith("pay:"))
async def start_pay(c: CallbackQuery, state: FSMContext):
    cur = c.data.split(":")[1]
    s = await db.get_settings()
    if s["plan_enabled"] != "1":
        await c.answer("Тариф отключён", show_alert=True)
        return
    if await db.pending_payment_of(c.from_user.id):
        await c.answer("У вас уже есть платёж на проверке. Дождитесь ответа.", show_alert=True)
        return
    price = s["price_tjs"] if cur == "TJS" else s["price_rub"]
    label = "сомони" if cur == "TJS" else "руб."
    details = s["pay_details_tjs"] if cur == "TJS" else s["pay_details_rub"]
    await state.set_state(Pay.receipt)
    await state.update_data(currency=cur)
    await c.message.answer(
        f"💳 К оплате: <b>{num(price)} {label}</b>\n\n"
        f"{esc(details)}\n\n"
        "После оплаты отправьте сюда <b>скриншот чека</b> (фото или файл).\n"
        "Отмена: /cancel"
    )
    await c.answer()


@user_router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=menu_kb(m.from_user.id))


@user_router.message(Pay.receipt, F.photo | F.document)
async def got_receipt(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    cur = data.get("currency", "TJS")
    s = await db.get_settings()
    if s["plan_enabled"] != "1":
        await state.clear()
        await m.answer("⛔ Тариф временно недоступен.")
        return
    if await db.pending_payment_of(m.from_user.id):
        await state.clear()
        await m.answer("У вас уже есть платёж на проверке.")
        return
    if m.photo:
        file_id, ftype = m.photo[-1].file_id, "photo"
    else:
        file_id, ftype = m.document.file_id, "document"
    amount = Decimal(s["price_tjs"] if cur == "TJS" else s["price_rub"])
    p = await db.create_payment(m.from_user.id, cur, amount, file_id, ftype)
    await state.clear()
    await m.answer("✅ Чек получен. Администратор проверит оплату, обычно это быстро.")
    for aid in ADMIN_IDS:
        await send_receipt(bot, aid, p)


@user_router.message(Pay.receipt)
async def receipt_wrong(m: Message):
    await m.answer("Отправьте скриншот чека (фото или файл) или /cancel.")


async def send_receipt(bot: Bot, chat_id: int, p):
    u = await db.get_user(p["user_id"])
    name = esc(u["full_name"]) if u else "?"
    uname = f" @{esc(u['username'])}" if u and u["username"] else ""
    cap = (
        f"🧾 <b>Платёж #{p['id']}</b>\n"
        f"👤 {name}{uname} (<code>{p['user_id']}</code>)\n"
        f"💰 {num(p['amount'])} {'сомони' if p['currency'] == 'TJS' else 'руб.'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"pok:{p['id']}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"pno:{p['id']}"),
    ]])
    try:
        if p["file_type"] == "photo":
            await bot.send_photo(chat_id, p["file_id"], caption=cap, reply_markup=kb)
        else:
            await bot.send_document(chat_id, p["file_id"], caption=cap, reply_markup=kb)
    except Exception as e:
        log.warning("send_receipt to %s failed: %s", chat_id, e)


# ---------------- admin: payments ----------------
@admin_router.callback_query(F.data.startswith("pok:") | F.data.startswith("pno:"))
async def decide(c: CallbackQuery, bot: Bot):
    action, pid = c.data.split(":")
    pid = int(pid)
    ok = action == "pok"
    p = await db.decide_payment(pid, c.from_user.id, "approved" if ok else "rejected")
    if not p:
        await c.answer("Платёж уже обработан", show_alert=True)
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if ok:
        s = await db.get_settings()
        hours = int(s["duration_hours"])
        sub, extended = await db.activate_subscription(p["user_id"], pid, hours)
        await c.message.answer(f"✅ Платёж #{pid} подтверждён. Подписка активна.")
        try:
            await bot.send_message(
                p["user_id"],
                ("✅ Оплата подтверждена! Подписка продлена." if extended
                 else "✅ Оплата подтверждена! Подписка «1 LIMIT» активирована.")
                + f"\n⏳ Действует до: {fmt_dt(sub['expires_at'])}\n"
                  "Первый аккаунт придёт сейчас, далее — каждый час.",
            )
        except Exception as e:
            log.warning("notify user failed: %s", e)
        await process_due(bot)
    else:
        await c.message.answer(f"❌ Платёж #{pid} отклонён.")
        try:
            await bot.send_message(
                p["user_id"],
                "❌ Оплата не подтверждена. Если вы платили — свяжитесь с администратором.",
            )
        except Exception as e:
            log.warning("notify user failed: %s", e)
    await c.answer()


# ---------------- admin: panel ----------------
async def panel_text() -> str:
    s = await db.get_settings()
    st = await db.stock_counts()
    h, per = int(s["duration_hours"]), int(s["per_hour"])
    return (
        "⚙️ <b>Админ-панель: 1 LIMIT</b>\n\n"
        f"Статус: {'🟢 включён' if s['plan_enabled'] == '1' else '🔴 отключён'}\n"
        f"💰 Цена: {num(s['price_tjs'])} сомони / {num(s['price_rub'])} руб.\n"
        f"⏰ Длительность: {h} ч.\n"
        f"📦 Выдача: {per} акк./час ({per * h} за подписку)\n"
        f"🗄 Склад: свободно {st['free']}, выдано {st['issued']}"
    )


async def panel_kb() -> InlineKeyboardMarkup:
    s = await db.get_settings()
    on = s["plan_enabled"] == "1"
    B = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(text="💰 Цена (сомони)", callback_data="adm:price_tjs"),
         B(text="💰 Цена (рубли)", callback_data="adm:price_rub")],
        [B(text="📦 Акк./час", callback_data="adm:per"),
         B(text="⏰ Длительность", callback_data="adm:dur")],
        [B(text="➕ Добавить акк.", callback_data="adm:add"),
         B(text="➖ Удалить акк.", callback_data="adm:del")],
        [B(text="📊 Склад", callback_data="adm:stock"),
         B(text="🔴 Отключить" if on else "🟢 Включить", callback_data="adm:toggle")],
        [B(text="🏦 Реквизиты TJS", callback_data="adm:det_tjs"),
         B(text="🏦 Реквизиты RUB", callback_data="adm:det_rub")],
        [B(text="🧾 Платежи на проверке", callback_data="adm:pending")],
    ])


async def show_panel(m: Message):
    await m.answer(await panel_text(), reply_markup=await panel_kb())


@admin_router.message(Command("admin"))
@admin_router.message(F.text == "⚙️ Админ-панель")
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await show_panel(m)


@admin_router.message(Command("cancel"))
async def admin_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.")
    await show_panel(m)


ASK = {
    "price_tjs": (Adm.price_tjs, "Введите новую цену в сомони (число):"),
    "price_rub": (Adm.price_rub, "Введите новую цену в рублях (число):"),
    "dur": (Adm.duration, "Введите длительность подписки в часах (1–720):"),
    "add": (Adm.add, "Отправьте аккаунты: по одному в строке (или .txt файлом).\nДубликаты пропускаются."),
    "del": (Adm.delete, "Отправьте аккаунты для удаления: по одному в строке, точно как в складе.\n"
                        "Удаляются только невыданные."),
    "det_tjs": (Adm.det_tjs, "Отправьте реквизиты для оплаты в сомони (текст):"),
    "det_rub": (Adm.det_rub, "Отправьте реквизиты для оплаты в рублях (текст):"),
}


@admin_router.callback_query(F.data.startswith("adm:"))
async def admin_cb(c: CallbackQuery, state: FSMContext, bot: Bot):
    act = c.data.split(":")[1]
    if act in ASK:
        st, text = ASK[act]
        await state.set_state(st)
        await c.message.answer(text + "\n\nОтмена: /cancel")
    elif act == "toggle":
        s = await db.get_settings()
        await db.set_setting("plan_enabled", "0" if s["plan_enabled"] == "1" else "1")
        await c.message.edit_text(await panel_text(), reply_markup=await panel_kb())
    elif act == "stock":
        st = await db.stock_counts()
        await c.message.answer(
            f"🗄 Склад:\nСвободно: <b>{st['free']}</b>\nВыдано: {st['issued']}\nВсего: {st['total']}"
        )
    elif act == "per":
        s = await db.get_settings()
        B = InlineKeyboardButton
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [B(text="➖", callback_data="adm:per_dec"), B(text=f"{s['per_hour']} акк./час", callback_data="adm:noop"),
             B(text="➕", callback_data="adm:per_inc")],
            [B(text="⬅️ Назад", callback_data="adm:back")],
        ])
        await c.message.edit_text("📦 Сколько аккаунтов выдавать каждый час:", reply_markup=kb)
    elif act in ("per_inc", "per_dec"):
        s = await db.get_settings()
        v = int(s["per_hour"]) + (1 if act == "per_inc" else -1)
        v = max(1, min(100, v))
        await db.set_setting("per_hour", str(v))
        B = InlineKeyboardButton
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [B(text="➖", callback_data="adm:per_dec"), B(text=f"{v} акк./час", callback_data="adm:noop"),
             B(text="➕", callback_data="adm:per_inc")],
            [B(text="⬅️ Назад", callback_data="adm:back")],
        ])
        try:
            await c.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
    elif act == "back":
        await c.message.edit_text(await panel_text(), reply_markup=await panel_kb())
    elif act == "pending":
        rows = await db.pending_payments()
        if not rows:
            await c.message.answer("Нет платежей на проверке.")
        for p in rows:
            await send_receipt(bot, c.from_user.id, p)
    await c.answer()


async def _set_number(m: Message, state: FSMContext, key: str, integer: bool, lo, hi):
    try:
        v = Decimal((m.text or "").strip().replace(",", "."))
        if integer and v != v.to_integral_value():
            raise InvalidOperation
        if not (lo <= v <= hi):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await m.answer(f"Нужно число от {lo} до {hi}. Попробуйте ещё раз или /cancel.")
        return
    await db.set_setting(key, str(int(v)) if integer else num(v))
    await state.clear()
    await m.answer("✅ Сохранено.")
    await show_panel(m)


@admin_router.message(Adm.price_tjs)
async def set_price_tjs(m: Message, state: FSMContext):
    await _set_number(m, state, "price_tjs", False, Decimal("0.01"), Decimal("1000000"))


@admin_router.message(Adm.price_rub)
async def set_price_rub(m: Message, state: FSMContext):
    await _set_number(m, state, "price_rub", False, Decimal("0.01"), Decimal("10000000"))


@admin_router.message(Adm.duration)
async def set_duration(m: Message, state: FSMContext):
    await _set_number(m, state, "duration_hours", True, 1, 720)


@admin_router.message(Adm.det_tjs)
async def set_det_tjs(m: Message, state: FSMContext):
    if not m.text:
        await m.answer("Отправьте текст.")
        return
    await db.set_setting("pay_details_tjs", m.text.strip())
    await state.clear()
    await m.answer("✅ Сохранено.")
    await show_panel(m)


@admin_router.message(Adm.det_rub)
async def set_det_rub(m: Message, state: FSMContext):
    if not m.text:
        await m.answer("Отправьте текст.")
        return
    await db.set_setting("pay_details_rub", m.text.strip())
    await state.clear()
    await m.answer("✅ Сохранено.")
    await show_panel(m)


async def _read_lines(m: Message, bot: Bot) -> list[str] | None:
    if m.document:
        if (m.document.file_size or 0) > 2_000_000:
            await m.answer("Файл слишком большой (макс. 2 МБ).")
            return None
        buf = io.BytesIO()
        await bot.download(m.document, destination=buf)
        return parse_lines(buf.getvalue().decode("utf-8", errors="ignore"))
    if m.text:
        return parse_lines(m.text)
    await m.answer("Отправьте текст или .txt файл.")
    return None


@admin_router.message(Adm.add)
async def add_accounts(m: Message, state: FSMContext, bot: Bot):
    lines = await _read_lines(m, bot)
    if lines is None:
        return
    if not lines:
        await m.answer("Пусто. Отправьте аккаунты или /cancel.")
        return
    added, dups = await db.add_accounts(lines)
    await state.clear()
    st = await db.stock_counts()
    await m.answer(f"✅ Добавлено: {added}\nДубликатов пропущено: {dups}\nСвободно на складе: {st['free']}")
    await show_panel(m)


@admin_router.message(Adm.delete)
async def del_accounts(m: Message, state: FSMContext, bot: Bot):
    lines = await _read_lines(m, bot)
    if lines is None:
        return
    if not lines:
        await m.answer("Пусто. Отправьте аккаунты или /cancel.")
        return
    n = await db.delete_accounts(lines)
    await state.clear()
    st = await db.stock_counts()
    await m.answer(f"🗑 Удалено: {n} из {len(lines)}\nСвободно на складе: {st['free']}")
    await show_panel(m)


# ---------------- scheduler ----------------
async def alert_empty(bot: Bot, user_id: int):
    global _last_empty_alert
    if time.time() - _last_empty_alert < EMPTY_ALERT_COOLDOWN:
        return
    _last_empty_alert = time.time()
    await notify_admins(
        bot,
        "⚠️ <b>Склад аккаунтов пуст!</b>\n"
        f"Не удалось выдать аккаунт пользователю <code>{user_id}</code>.\n"
        "Добавьте аккаунты: /admin → ➕ Добавить акк. Выдача возобновится автоматически.",
    )


async def process_due(bot: Bot):
    async with LOCK:
        s = await db.get_settings()
        n = max(1, int(s["per_hour"]))
        for sub in await db.due_subscriptions():
            for _ in range(48):  # защита от бесконечного цикла при догоне после простоя
                res = await db.issue_accounts(sub["id"], sub["user_id"], n)
                if res is None:
                    await alert_empty(bot, sub["user_id"])
                    break
                if not res:
                    break
                text = "🎁 <b>Ваши аккаунты:</b>\n\n" + "\n".join(f"<code>{esc(a)}</code>" for a in res)
                try:
                    await bot.send_message(sub["user_id"], text)
                except Exception as e:
                    log.warning("deliver to %s failed: %s", sub["user_id"], e)
        for sub in await db.expire_due():
            try:
                await bot.send_message(
                    sub["user_id"],
                    f"⏰ Подписка «1 LIMIT» завершена. Выдано аккаунтов: {sub['issued_count']}.\n"
                    "История: «📜 История». Для продления — «💎 Тариф».",
                )
            except Exception as e:
                log.warning("expire notify %s failed: %s", sub["user_id"], e)


async def scheduler(bot: Bot):
    while True:
        try:
            await process_due(bot)
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(30)


async def main():
    token = os.environ["BOT_TOKEN"]
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS не задан — админ-панель недоступна!")
    await db.init()
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)
    dp.include_router(user_router)
    await bot.delete_webhook(drop_pending_updates=False)
    task = asyncio.create_task(scheduler(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
