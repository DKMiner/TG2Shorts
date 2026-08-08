from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import logging

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import (
    load_config,
    load_raw_config,
    switch_active_template,
    update_template,
)
from dir import ensure_dirs
from jobs import (
    BASE_DIR,
    clear_queue,
    create_job,
    has_media,
    load_active_job_id,
    load_job,
    load_queue,
    save_queue,
    set_active_job_id,
    source_link,
)
from worker import process_job, validate_queue_accessible
from whisper_tool import whisper as whisper_cmd

from youtube_publish import publish as publish_cmd

PENDING_KEY = "pending_settings"


def _current_cfg():
    return load_config()


def _queue_locked(template_name: str) -> bool:
    return load_active_job_id(template_name) is not None


def _pending_map(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.application.bot_data.setdefault(PENDING_KEY, {})


def _set_pending(context: ContextTypes.DEFAULT_TYPE, user_id: int, payload: dict) -> None:
    _pending_map(context)[str(user_id)] = payload


def _pop_pending(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> dict | None:
    return _pending_map(context).pop(str(user_id), None)


def _get_pending(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> dict | None:
    return _pending_map(context).get(str(user_id))


def _chunk(seq: list[str], size: int) -> list[list[str]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def _bool_text(value: bool) -> str:
    return "on" if value else "off"


def _settings_text(cfg) -> str:
    t = cfg.template
    lines = [
        "<b>Settings</b>",
        f"Active template: <code>{escape(cfg.active_template)}</code>",
        f"Templates: <code>{escape(', '.join(cfg.available_templates) if cfg.available_templates else 'none')}</code>",
        "",
        "<b>Current template values</b>",
        f"Video: <code>{t.video.width}x{t.video.height} @ {t.video.fps}fps</code>",
        f"Intro: <code>{_bool_text(t.render.intro)}</code>",
        f"Transition: <code>{_bool_text(t.render.transition)}</code>",
        f"Outro: <code>{_bool_text(t.render.outro)}</code>",
        f"Blur background: <code>{_bool_text(t.render.blur_background)}</code>",
        f"Input chat: <code>{escape(str(t.telegram.input_chat))}</code>",
        f"Review chat: <code>{escape(str(t.telegram.review_chat))}</code>",
        f"Intro part: <code>{_bool_text(t.render.intro_part.enabled)}</code>",
        f"Part timing: <code>{t.render.intro_part.start} → {t.render.intro_part.end}</code>",
    ]
    return "\n".join(lines)


def _settings_keyboard(cfg) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []

    template_buttons = []
    for name in cfg.available_templates:
        label = f"✅ {name}" if name == cfg.active_template else name
        template_buttons.append(InlineKeyboardButton(label, callback_data=f"settings:template:{name}"))
    buttons.extend(_chunk(template_buttons, 2))

    t = cfg.template

    buttons.append([
        InlineKeyboardButton(f"Intro {_bool_text(t.render.intro)}", callback_data="settings:toggle:intro"),
        InlineKeyboardButton(f"Transition {_bool_text(t.render.transition)}", callback_data="settings:toggle:transition"),
    ])
    buttons.append([
        InlineKeyboardButton(f"Outro {_bool_text(t.render.outro)}", callback_data="settings:toggle:outro"),
        InlineKeyboardButton(f"Blur {_bool_text(t.render.blur_background)}", callback_data="settings:toggle:blur_background"),
    ])

    buttons.append([
        InlineKeyboardButton(f"Width {t.video.width}", callback_data="settings:set:width"),
        InlineKeyboardButton(f"Height {t.video.height}", callback_data="settings:set:height"),
        InlineKeyboardButton(f"FPS {t.video.fps}", callback_data="settings:set:fps"),
    ])

    buttons.append([
        InlineKeyboardButton("Input chat", callback_data="settings:set:input_chat"),
        InlineKeyboardButton("Review chat", callback_data="settings:set:review_chat"),
        InlineKeyboardButton(f"Intro part {_bool_text(t.render.intro_part.enabled)}", callback_data="settings:toggle:intro_part.enabled"),
    ])

    buttons.append([
        InlineKeyboardButton("Refresh", callback_data="settings:refresh"),
    ])

    return InlineKeyboardMarkup(buttons)


async def _show_settings_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    cfg = _current_cfg()
    await context.bot.send_message(
        chat_id=chat_id,
        text=_settings_text(cfg),
        parse_mode="HTML",
        reply_markup=_settings_keyboard(cfg),
        disable_web_page_preview=True,
    )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None or update.effective_chat is None:
        return
    await _show_settings_menu(context, update.effective_chat.id)


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    data = query.data or ""
    cfg = _current_cfg()

    async def safe_edit():
        try:
            await query.edit_message_text(
                text=_settings_text(cfg),
                parse_mode="HTML",
                reply_markup=_settings_keyboard(cfg),
                disable_web_page_preview=True,
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

    if data == "settings:refresh":
        await safe_edit()
        return

    if data.startswith("settings:template:"):
        if _queue_locked(cfg.active_template):
            await query.answer("A job is active right now.", show_alert=True)
            return
        target = data.split(":", 2)[2]
        raw = load_raw_config()
        if target not in raw.get("templates", {}):
            await query.answer("Unknown template.", show_alert=True)
            return
        switch_active_template(target)
        cfg = _current_cfg()
        await safe_edit()
        return

    if data.startswith("settings:toggle:"):
        key = data.split(":", 2)[2]
        if _queue_locked(cfg.active_template):
            await query.answer("A job is active right now.", show_alert=True)
            return

        if key == "intro_part.enabled":
            current = bool(cfg.template.render.intro_part.enabled)
            update_template(cfg.active_template, ["render", "intro_part", "enabled"], not current)
        elif key in {"intro", "transition", "outro", "blur_background"}:
            current = bool(getattr(cfg.template.render, key))
            update_template(cfg.active_template, ["render", key], not current)
        else:
            await query.answer("Unknown toggle.", show_alert=True)
            return

        cfg = _current_cfg()
        await safe_edit()
        return

    if data.startswith("settings:set:"):
        field = data.split(":", 2)[2]
        user_id = update.effective_user.id if update.effective_user else None
        if user_id is None or update.effective_chat is None:
            return

        prompt_map = {
            "width": "Reply with a number for width.",
            "height": "Reply with a number for height.",
            "fps": "Reply with a number for FPS.",
            "input_chat": "Reply with a chat ID, or type here.",
            "review_chat": "Reply with a chat ID, or type here.",
        }
        if field not in prompt_map:
            await query.answer("Unknown field.", show_alert=True)
            return

        prompt = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=prompt_map[field],
            reply_markup=ForceReply(selective=True),
        )
        _set_pending(
            context,
            user_id,
            {
                "template_name": cfg.active_template,
                "field": field,
                "prompt_message_id": prompt.message_id,
                "chat_id": update.effective_chat.id,
            },
        )
        return


async def settings_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None or update.effective_user is None:
        return

    pending = _get_pending(context, update.effective_user.id)
    if not pending:
        return

    reply_to = msg.reply_to_message
    if reply_to is None or not getattr(reply_to.from_user, "is_bot", False):
        return

    template_name = pending["template_name"]
    field = pending["field"]
    text = (msg.text or "").strip()

    try:
        if field in {"width", "height", "fps"}:
            value = int(text)
            update_template(template_name, ["video", field], value)
        elif field in {"input_chat", "review_chat"}:
            if text.lower() == "here":
                if update.effective_chat is None:
                    raise RuntimeError("No current chat.")
                value = update.effective_chat.id
            else:
                value = int(text)
            update_template(template_name, ["telegram", field], value)
        else:
            raise RuntimeError("Unknown setting field.")
    except Exception as exc:
        await msg.reply_text(f"Could not update setting: {type(exc).__name__}: {exc}")
        return
    finally:
        _pop_pending(context, update.effective_user.id)

    await msg.reply_text("Updated.")
    await _show_settings_menu(context, update.effective_chat.id)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _current_cfg()
    template_name = cfg.active_template
    queue = load_queue(template_name)
    active_job_id = load_active_job_id(template_name)
    job = load_job(template_name, active_job_id) if active_job_id else None

    lines: list[str] = [
        "<b>Violet pipeline</b>",
        f"Template: <code>{escape(template_name)}</code>",
        f"Queued items: <code>{len(queue)}</code>",
        f"Active job: <code>{escape(active_job_id) if active_job_id else 'none'}</code>",
    ]

    if queue:
        lines += ["", "<b>Queue</b>"]
        for idx, item in enumerate(queue, start=1):
            link = source_link(int(item["source_chat_id"]), int(item["source_message_id"]))
            media = escape(str(item.get("media_type", "media")).title())
            state = escape(str(item.get("status", "queued")))
            open_link = f'<a href="{escape(link)}">Open</a>' if link else "Open"
            lines.append(f"{idx}. {media} — {open_link} — {state}")

    if job:
        lines += ["", "<b>Active job</b>"]
        lines.append(f"ID: <code>{escape(str(job.get('job_id', active_job_id)))}</code>")
        lines.append(f"State: <code>{escape(str(job.get('status', 'unknown')))}</code>")
        lines.append(f"Text: <code>{escape(str(job.get('make_text', '1')))}</code>")

        items = job.get("items", [])
        if items:
            done = sum(1 for item in items if item.get("status") == "downloaded")
            lines.append(f"Progress: <code>{done}/{len(items)}</code>")

    if update.message:
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _current_cfg()
    msg = update.message
    if msg is None or update.effective_chat is None:
        return

    if cfg.template.telegram.input_chat is not None and update.effective_chat.id != cfg.template.telegram.input_chat:
        await msg.reply_text("Wrong chat.")
        return

    if _queue_locked(cfg.active_template):
        await msg.reply_text("A job is active right now. Try again after it finishes.")
        return

    replied = msg.reply_to_message
    if replied is None or not has_media(replied):
        await msg.reply_text("Reply to a media message with /add.")
        return

    queue = load_queue(cfg.active_template)
    next_id = max((int(item["id"]) for item in queue), default=0) + 1

    item = {
        "id": next_id,
        "source_chat_id": replied.chat_id,
        "source_message_id": replied.message_id,
        "queued_by": update.effective_user.id if update.effective_user else None,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "status": "queued",
        "media_type": (
            "video"
            if replied.video
            else "document"
            if replied.document
            else "photo"
            if replied.photo
            else "audio"
            if replied.audio
            else "animation"
            if replied.animation
            else "voice"
            if replied.voice
            else "media"
        ),
        "caption": replied.caption,
    }

    queue.append(item)
    save_queue(cfg.active_template, queue)

    link = source_link(int(replied.chat_id), int(replied.message_id))
    if link:
        await msg.reply_text(
            f'Queued item <code>{next_id}</code>\n<a href="{escape(link)}">Open source</a>',
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    else:
        await msg.reply_text(f"Queued item {next_id}")


async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _current_cfg()
    msg = update.message
    if msg is None:
        return

    if _queue_locked(cfg.active_template):
        await msg.reply_text("A job is active right now. Try again after it finishes.")
        return

    queue = load_queue(cfg.active_template)
    if not queue:
        await msg.reply_text("Queue is empty.")
        return

    if not context.args:
        await msg.reply_text("Use /remove <number>.")
        return

    try:
        position = int(context.args[0])
    except ValueError:
        await msg.reply_text("Use /remove <number>.")
        return

    if position < 1 or position > len(queue):
        await msg.reply_text(f"Item #{position} not found.")
        return

    removed = queue.pop(position - 1)
    save_queue(cfg.active_template, queue)
    await msg.reply_text(f"Removed item #{position} (source msg {removed['source_message_id']}).")


async def empty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _current_cfg()
    msg = update.message
    if msg is None:
        return

    if _queue_locked(cfg.active_template):
        await msg.reply_text("A job is active right now. Try again after it finishes.")
        return

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Empty queue", callback_data="empty_step1")]]
    )
    await msg.reply_text("This will wipe the current template queue.", reply_markup=keyboard)


async def empty_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _current_cfg()
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    data = query.data or ""

    if data == "empty_step1":
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("Confirm", callback_data="empty_confirm"),
                InlineKeyboardButton("Cancel", callback_data="empty_cancel"),
            ]]
        )
        await query.edit_message_text("Confirm emptying the queue?", reply_markup=keyboard)
        return

    if data == "empty_cancel":
        await query.edit_message_text("Cancelled.")
        return

    if data == "empty_confirm":
        if _queue_locked(cfg.active_template):
            await query.edit_message_text("A job is active right now. Try again after it finishes.")
            return

        clear_queue(cfg.active_template)
        await query.edit_message_text("Queue emptied.")
        return


async def make(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _current_cfg()
    msg = update.message
    if msg is None or update.effective_chat is None:
        return

    if cfg.template.telegram.input_chat is not None and update.effective_chat.id != cfg.template.telegram.input_chat:
        await msg.reply_text("Wrong chat.")
        return

    if _queue_locked(cfg.active_template):
        await msg.reply_text("A job is active right now. Try again after it finishes.")
        return

    queue = load_queue(cfg.active_template)
    if not queue:
        await msg.reply_text("Queue is empty.")
        return

    make_text = " ".join(context.args).strip()

    if cfg.active_template == "ranking":
        make_text = make_text or "Ranking funny viral videos"
    else:
        make_text = make_text or "Violet #meme"

    await msg.reply_text(f"Validating {len(queue)} item(s)...")

    errors = await validate_queue_accessible(cfg, queue)
    if errors:
        preview = "\n".join(errors[:20])
        more = "" if len(errors) <= 20 else f"\n...and {len(errors) - 20} more"
        await msg.reply_text(f"Cannot start job yet:\n{preview}{more}")
        return

    job_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    create_job(
        cfg.active_template,
        job_id,
        update.effective_user.id if update.effective_user else None,
        queue,
        make_text,
    )
    set_active_job_id(cfg.active_template, job_id)
    clear_queue(cfg.active_template)

    await msg.reply_text(
        f"Job <code>{job_id}</code> created.\n"
        f"Text: <code>{escape(make_text)}</code>\n"
        f"Processing now...",
        parse_mode="HTML",
    )

    context.application.create_task(
        process_job(
            cfg,
            job_id,
            bot=context.bot,
            notify_chat_id=update.effective_chat.id,
        )
    )


def main() -> None:
    cfg = load_config()
    ensure_dirs(BASE_DIR)

    from logger import setup_logging
    setup_logging(BASE_DIR, cfg.active_template)

    application = Application.builder().token(cfg.bot_token).build()
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("add", add))
    application.add_handler(CommandHandler("remove", remove))
    application.add_handler(CommandHandler("empty", empty))
    application.add_handler(CommandHandler("make", make))
    application.add_handler(CommandHandler("settings", settings))
    application.add_handler(CommandHandler("whisper", whisper_cmd))
    application.add_handler(CommandHandler("publish", publish_cmd))
    application.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^settings:"))
    application.add_handler(CallbackQueryHandler(empty_callback, pattern=r"^empty_(step1|confirm|cancel)$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, settings_reply))
    application.run_polling()


if __name__ == "__main__":
    main()
