import asyncio
import logging
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

from database import (
    init_db,
    async_session,
    User,
    Chat,
    Message as ChatMessage,
    QueueEntry,
    ChatHistory,
)
from profanity_filter import check_profanity
from config import BOT_TOKEN, MIN_AGE, MAX_AGE

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния для ConversationHandler
WAITING_AGE, WAITING_ROLE, WAITING_PROBLEM = range(3)


def get_age_range(user_age: int) -> tuple[int, int]:
    """
    Возвращает диапазон возрастов для подбора собеседников в зависимости от возраста пользователя.
    """
    if user_age == 14:
        return (14, 16)
    elif user_age == 15:
        return (14, 17)
    elif user_age == 16:
        return (14, 18)
    elif user_age == 17:
        return (15, 18)
    elif user_age == 18:
        return (16, 18)
    else:
        return (MIN_AGE, MAX_AGE)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик команды /start"""
    user_id = update.effective_user.id

    async with async_session() as session:
        # Проверяем, зарегистрирован ли пользователь
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.telegram_id == user_id))
        user = result.scalar_one_or_none()

        if user:
            if user.is_banned:
                await update.message.reply_text(
                    "❌ Вы заблокированы и не можете использовать бота."
                )
                return ConversationHandler.END

            # Если пользователь уже зарегистрирован, показываем главное меню
            await show_main_menu(update, context, user)
            return ConversationHandler.END
        else:
            # Новый пользователь - запрашиваем возраст
            await update.message.reply_text(
                "👋 Добро пожаловать в бот поддержки!\n\n"
                "Пожалуйста, укажите ваш возраст (от 14 до 18 лет):"
            )
            return WAITING_AGE


async def handle_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик ввода возраста"""
    try:
        age = int(update.message.text)

        if age < MIN_AGE or age > MAX_AGE:
            await update.message.reply_text(
                "⚠️ Данным ботом могут пользоваться лишь пользователи, достигшие возраста 14 лет и не старше 18 лет."
            )
            return WAITING_AGE

        # Сохраняем возраст в контексте
        context.user_data['age'] = age

        # Регистрируем пользователя
        user_id = update.effective_user.id
        async with async_session() as session:
            user = User(
                telegram_id=user_id,
                age=age,
                is_banned=False,
                registered_at=datetime.utcnow()
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            context.user_data['user_id'] = user.id

        # Показываем выбор роли
        keyboard = [
            [InlineKeyboardButton("1️⃣ Поддержать", callback_data="role_support")],
            [InlineKeyboardButton("2️⃣ Получить поддержку", callback_data="role_receive_support")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "✅ Регистрация завершена!\n\n"
            "Выберите категорию действий:",
            reply_markup=reply_markup
        )

        return WAITING_ROLE

    except ValueError:
        await update.message.reply_text(
            "❌ Пожалуйста, введите корректный возраст (число от 14 до 18):"
        )
        return WAITING_AGE


async def handle_role_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик выбора роли"""
    query = update.callback_query
    await query.answer()

    role = query.data.split("_")[1]  # support или receive_support
    context.user_data['role'] = role

    # Показываем выбор проблемы
    keyboard = [
        [InlineKeyboardButton("1️⃣ Стресс и тревожность", callback_data="problem_stress_anxiety")],
        [InlineKeyboardButton("2️⃣ Учеба", callback_data="problem_study")],
        [InlineKeyboardButton("3️⃣ Друзья", callback_data="problem_friends")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "Выберите проблему:",
        reply_markup=reply_markup
    )

    return WAITING_PROBLEM


async def handle_problem_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик выбора проблемы"""
    query = update.callback_query
    await query.answer()

    problem = "_".join(query.data.split("_")[1:])  # stress_anxiety, study, friends
    context.user_data['problem'] = problem

    user_id = update.effective_user.id

    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.telegram_id == user_id))
        user = result.scalar_one_or_none()

        if not user:
            await query.edit_message_text("❌ Ошибка: пользователь не найден.")
            return ConversationHandler.END

        # Обновляем информацию о пользователе
        user.current_role = context.user_data['role']
        user.current_problem = problem
        await session.commit()

        # Добавляем пользователя в очередь
        await add_to_queue(session, user, context.user_data['role'], problem)

        await query.edit_message_text(
            "🔍 Ищем подходящего собеседника...\n"
            "Пожалуйста, подождите."
        )

        # Пытаемся найти собеседника
        match = await find_match(session, user, context.user_data['role'], problem)

        if match:
            # Создаем чат
            chat = await create_chat(session, user, match, context.user_data['role'], problem)
            await session.commit()

            # Удаляем обоих из очереди
            await remove_from_queue(session, user.id)
            await remove_from_queue(session, match.id)

            # Отправляем сообщения обоим пользователям
            await context.bot.send_message(
                chat_id=user.telegram_id,
                text=f"✅ Собеседник найден!\n\n"
                     f"Возраст собеседника: {match.age} лет\n"
                     f"Ваш возраст виден собеседнику: {user.age} лет\n\n"
                     f"Начните общение!"
            )

            await context.bot.send_message(
                chat_id=match.telegram_id,
                text=f"✅ Собеседник найден!\n\n"
                     f"Возраст собеседника: {user.age} лет\n"
                     f"Ваш возраст виден собеседнику: {match.age} лет\n\n"
                     f"Начните общение!"
            )
        else:
            await context.bot.send_message(
                chat_id=user.telegram_id,
                text="⏳ Собеседник пока не найден. Вы добавлены в очередь ожидания.\n"
                     "Мы уведомим вас, когда найдем подходящего собеседника."
            )

    return ConversationHandler.END


async def add_to_queue(session, user: User, role: str, problem: str):
    """Добавляет пользователя в очередь"""
    # Проверяем, не находится ли пользователь уже в очереди
    from sqlalchemy import select
    result = await session.execute(
        select(QueueEntry).where(QueueEntry.user_id == user.id)  # type: ignore
    )
    existing = result.scalar_one_or_none()

    if existing:
        # Обновляем существующую запись
        existing.role = role
        existing.problem_type = problem
        existing.age = user.age
        existing.joined_at = datetime.utcnow()
    else:
        # Создаем новую запись
        queue_entry = QueueEntry(
            user_id=user.id,
            role=role,
            problem_type=problem,
            age=user.age,
            joined_at=datetime.utcnow()
        )
        session.add(queue_entry)

    await session.commit()


async def remove_from_queue(session, user_id: int):
    """Удаляет пользователя из очереди"""
    from sqlalchemy import delete
    await session.execute(delete(QueueEntry).where(QueueEntry.user_id == user_id))  # type: ignore
    await session.commit()


async def find_match(session, user: User, role: str, problem: str) -> Optional[User]:
    """Ищет подходящего собеседника"""
    # Определяем противоположную роль
    opposite_role = "receive_support" if role == "support" else "support"

    # Получаем диапазон возрастов для пользователя
    min_age, max_age = get_age_range(user.age)

    # Ищем подходящего собеседника в очереди
    from sqlalchemy import select, and_
    result = await session.execute(
        select(QueueEntry, User).join(User, QueueEntry.user_id == User.id).where(  # type: ignore
            and_(
                QueueEntry.user_id != user.id,
                QueueEntry.role == opposite_role,
                QueueEntry.problem_type == problem,
                User.age >= min_age,
                User.age <= max_age,
                User.is_banned.is_(False),  # Используем .is_() для правильного типа
                User.current_chat_id.is_(None)
            )
        ).order_by(QueueEntry.joined_at)
    )

    match_entry = result.first()

    if match_entry:
        queue_entry, matched_user = match_entry
        # Проверяем, что найденный пользователь тоже подходит по возрасту
        # (проверяем обратную совместимость)
        match_min_age, match_max_age = get_age_range(matched_user.age)
        if match_min_age <= user.age <= match_max_age:
            return matched_user

    return None


async def create_chat(session, user1: User, user2: User, role1: str, problem: str) -> Chat:
    """Создает чат между двумя пользователями"""
    role2 = "receive_support" if role1 == "support" else "support"

    chat = Chat(
        user1_id=user1.id,
        user2_id=user2.id,
        role1=role1,
        role2=role2,
        problem_type=problem,
        created_at=datetime.utcnow(),
        is_active=True
    )
    session.add(chat)
    await session.flush()

    # Обновляем информацию о текущем чате у пользователей
    user1.current_chat_id = chat.id
    user2.current_chat_id = chat.id

    return chat


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user: User):
    """Показывает главное меню"""
    keyboard = [
        [InlineKeyboardButton("💬 Найти собеседника", callback_data="find_match")],
        [InlineKeyboardButton("📜 История чатов", callback_data="chat_history")],
        [InlineKeyboardButton("❌ Завершить текущий чат", callback_data="end_chat")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = "Главное меню:\n\n"
    if user.current_chat_id:
        text += "✅ У вас есть активный чат"
    else:
        text += "❌ У вас нет активного чата"

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик обычных сообщений"""
    user_id = update.effective_user.id
    message_text = update.message.text

    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.telegram_id == user_id))
        user = result.scalar_one_or_none()

        if not user:
            await update.message.reply_text("Пожалуйста, начните с команды /start")
            return

        if user.is_banned:
            await update.message.reply_text("❌ Вы заблокированы и не можете использовать бота.")
            return

        # Проверка на ненормативную лексику
        if check_profanity(message_text):
            # Блокируем пользователя
            user.is_banned = True
            await session.commit()

            await update.message.reply_text(
                "❌ Вы использовали ненормативную лексику. "
                "Вы получили пожизненный запрет на использование бота."
            )
            return

        # Если у пользователя есть активный чат, пересылаем сообщение
        if user.current_chat_id:
            chat_id = user.current_chat_id
            result = await session.execute(select(Chat).where(Chat.id == chat_id))  # type: ignore
            chat = result.scalar_one_or_none()

            if chat and chat.is_active:
                # Определяем собеседника
                if chat.user1_id == user.id:
                    partner_id = chat.user2_id
                else:
                    partner_id = chat.user1_id

                result = await session.execute(select(User).where(User.id == partner_id))  # type: ignore
                partner = result.scalar_one_or_none()

                if partner:
                    # Сохраняем сообщение в базу
                    message = ChatMessage(
                        chat_id=chat.id,
                        user_id=user.id,
                        text=message_text,
                        sent_at=datetime.utcnow()
                    )
                    session.add(message)
                    await session.commit()

                    # Отправляем сообщение собеседнику
                    await context.bot.send_message(
                        chat_id=partner.telegram_id,
                        text=message_text
                    )
                else:
                    await update.message.reply_text("❌ Собеседник не найден.")
            else:
                await update.message.reply_text("❌ Чат не активен.")
        else:
            await update.message.reply_text(
                "У вас нет активного чата. Используйте /start для поиска собеседника."
            )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик callback-запросов"""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.telegram_id == user_id))
        user = result.scalar_one_or_none()

        if not user or user.is_banned:
            await query.edit_message_text("❌ Доступ запрещен.")
            return

        if query.data == "find_match":
            # Показываем выбор роли
            keyboard = [
                [InlineKeyboardButton("1️⃣ Поддержать", callback_data="menu_role_support")],
                [InlineKeyboardButton("2️⃣ Получить поддержку", callback_data="menu_role_receive_support")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("Выберите категорию действий:", reply_markup=reply_markup)

        elif query.data.startswith("menu_role_"):
            role = query.data.split("_")[2]
            context.user_data['role'] = role

            keyboard = [
                [InlineKeyboardButton("1️⃣ Стресс и тревожность", callback_data=f"menu_problem_{role}_stress_anxiety")],
                [InlineKeyboardButton("2️⃣ Учеба", callback_data=f"menu_problem_{role}_study")],
                [InlineKeyboardButton("3️⃣ Друзья", callback_data=f"menu_problem_{role}_friends")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("Выберите проблему:", reply_markup=reply_markup)

        elif query.data.startswith("menu_problem_"):
            parts = query.data.split("_")
            role = parts[2]
            problem = "_".join(parts[3:])  # stress_anxiety, study, friends

            user.current_role = role
            user.current_problem = problem
            await session.commit()

            await add_to_queue(session, user, role, problem)

            await query.edit_message_text(
                "🔍 Ищем подходящего собеседника...\n"
                "Пожалуйста, подождите."
            )

            # Пытаемся найти собеседника
            match = await find_match(session, user, role, problem)

            if match:
                chat = await create_chat(session, user, match, role, problem)
                await session.commit()

                await remove_from_queue(session, user.id)
                await remove_from_queue(session, match.id)

                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=f"✅ Собеседник найден!\n\n"
                         f"Возраст собеседника: {match.age} лет\n"
                         f"Ваш возраст виден собеседнику: {user.age} лет\n\n"
                         f"Начните общение!"
                )

                await context.bot.send_message(
                    chat_id=match.telegram_id,
                    text=f"✅ Собеседник найден!\n\n"
                         f"Возраст собеседника: {user.age} лет\n"
                         f"Ваш возраст виден собеседнику: {match.age} лет\n\n"
                         f"Начните общение!"
                )
            else:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text="⏳ Собеседник пока не найден. Вы добавлены в очередь ожидания.\n"
                         "Мы уведомим вас, когда найдем подходящего собеседника."
                )

        elif query.data == "chat_history":
            await show_chat_history(query, session, user, context)

        elif query.data == "end_chat":
            if user.current_chat_id:
                result = await session.execute(select(Chat).where(Chat.id == user.current_chat_id))  # type: ignore
                chat = result.scalar_one_or_none()

                if chat:
                    chat.is_active = False
                    chat.ended_at = datetime.utcnow()

                    # Определяем собеседника
                    if chat.user1_id == user.id:
                        partner_id = chat.user2_id
                    else:
                        partner_id = chat.user1_id

                    result = await session.execute(select(User).where(User.id == partner_id))  # type: ignore
                    partner = result.scalar_one_or_none()

                    if partner:
                        partner.current_chat_id = None
                        await context.bot.send_message(
                            chat_id=partner.telegram_id,
                            text="❌ Собеседник завершил чат."
                        )

                    user.current_chat_id = None

                    # Сохраняем историю чата для обоих пользователей
                    # Проверяем, не превышает ли количество сохраненных чатов 3
                    from sqlalchemy import select, desc, and_, func

                    # Для текущего пользователя
                    # Проверяем, не существует ли уже запись для этого чата
                    result = await session.execute(
                        select(ChatHistory).where(
                            and_(ChatHistory.user_id == user.id, ChatHistory.chat_id == chat.id)
                        )
                    )
                    existing_entry = result.scalar_one_or_none()

                    if not existing_entry:
                        result = await session.execute(
                            select(func.count(ChatHistory.id)).where(ChatHistory.user_id == user.id)  # type: ignore
                        )
                        user_history_count = result.scalar()

                        if user_history_count >= 3:
                            # Удаляем самую старую запись
                            result = await session.execute(
                                select(ChatHistory).where(ChatHistory.user_id == user.id)  # type: ignore
                                .order_by(ChatHistory.viewed_at).limit(1)
                            )
                            old_entry = result.scalar_one_or_none()
                            if old_entry:
                                session.delete(old_entry)

                        history_entry = ChatHistory(
                            user_id=user.id,
                            chat_id=chat.id,
                            viewed_at=datetime.utcnow()
                        )
                        session.add(history_entry)
                    else:
                        # Обновляем время просмотра
                        existing_entry.viewed_at = datetime.utcnow()

                    # Для собеседника
                    if partner:
                        result = await session.execute(
                            select(ChatHistory).where(
                                and_(ChatHistory.user_id == partner.id, ChatHistory.chat_id == chat.id)
                            )
                        )
                        existing_entry = result.scalar_one_or_none()

                        if not existing_entry:
                            result = await session.execute(
                                select(func.count(ChatHistory.id)).where(ChatHistory.user_id == partner.id)  # type: ignore
                                # type: ignore
                            )
                            partner_history_count = result.scalar()

                            if partner_history_count >= 3:
                                # Удаляем самую старую запись
                                result = await session.execute(
                                    select(ChatHistory).where(ChatHistory.user_id == partner.id)  # type: ignore
                                    .order_by(ChatHistory.viewed_at).limit(1)
                                )
                                old_entry = result.scalar_one_or_none()
                                if old_entry:
                                    session.delete(old_entry)

                            history_entry = ChatHistory(
                                user_id=partner.id,
                                chat_id=chat.id,
                                viewed_at=datetime.utcnow()
                            )
                            session.add(history_entry)
                        else:
                            # Обновляем время просмотра
                            existing_entry.viewed_at = datetime.utcnow()

                    await session.commit()

                    await query.edit_message_text("✅ Чат завершен.")
                else:
                    await query.edit_message_text("❌ Чат не найден.")
            else:
                await query.edit_message_text("❌ У вас нет активного чата.")

        elif query.data.startswith("view_chat_"):
            chat_id = int(query.data.split("_")[2])
            await view_chat_history(query, session, user, chat_id, context)

        elif query.data == "back_to_menu":
            await show_main_menu(update, context, user)


async def show_chat_history(query, session, user: User, context: ContextTypes.DEFAULT_TYPE):
    """Показывает историю последних 3 чатов"""
    from sqlalchemy import select, desc

    # Получаем последние 3 чата из истории пользователя
    result = await session.execute(
        select(ChatHistory, Chat).join(Chat, ChatHistory.chat_id == Chat.id)  # type: ignore
        .where(ChatHistory.user_id == user.id)  # type: ignore
        .order_by(desc(ChatHistory.viewed_at))
        .limit(3)
    )
    history_entries = result.all()

    if not history_entries:
        await query.edit_message_text("📜 У вас пока нет истории чатов.")
        return

    keyboard = []
    for i, (history_entry, chat) in enumerate(history_entries, 1):
        # Определяем собеседника
        if chat.user1_id == user.id:
            partner_id = chat.user2_id
        else:
            partner_id = chat.user1_id

        result = await session.execute(select(User).where(User.id == partner_id))  # type: ignore
        partner = result.scalar_one_or_none()

        if partner:
            keyboard.append([
                InlineKeyboardButton(
                    f"Чат {i} (Возраст: {partner.age} лет)",
                    callback_data=f"view_chat_{chat.id}"
                )
            ])

    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "📜 История последних чатов:\n\n"
        "Выберите чат для просмотра:",
        reply_markup=reply_markup
    )


async def view_chat_history(query, session, user: User, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Показывает историю конкретного чата"""
    from sqlalchemy import select

    result = await session.execute(select(Chat).where(Chat.id == chat_id))
    chat = result.scalar_one_or_none()

    if not chat:
        await query.edit_message_text("❌ Чат не найден.")
        return

    # Получаем сообщения чата
    result = await session.execute(
        select(ChatMessage).where(ChatMessage.chat_id == chat_id).order_by(ChatMessage.sent_at)
    )
    messages = result.scalars().all()

    if not messages:
        await query.edit_message_text("📜 В этом чате нет сообщений.")
        return

    # Формируем текст истории
    history_text = "📜 История чата:\n\n"

    for message in messages:
        is_own = message.user_id == user.id
        prefix = "Вы" if is_own else "Собеседник"
        history_text += f"{prefix}: {message.text}\n"
        history_text += f"   ({message.sent_at.strftime('%Y-%m-%d %H:%M')})\n\n"

    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="chat_history")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(history_text, reply_markup=reply_markup)


def main():
    """Главная функция запуска бота"""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не установлен! Создайте файл .env с BOT_TOKEN=ваш_токен")
        return

    # Создаем приложение с инициализацией БД
    async def post_init(application: Application) -> None:
        """Инициализация базы данных после создания приложения"""
        await init_db()
        logger.info("База данных инициализирована")

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Создаем ConversationHandler для регистрации
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_age)],
            WAITING_ROLE: [CallbackQueryHandler(handle_role_selection, pattern="^role_")],
            WAITING_PROBLEM: [CallbackQueryHandler(handle_problem_selection, pattern="^problem_")],
        },
        fallbacks=[CommandHandler("start", start)],
        per_chat=True,  # Отслеживать состояние для каждого чата
    )

    # Добавляем обработчики
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CommandHandler("start", start))

    # Функция для периодического подбора собеседников
    async def periodic_matchmaking_task(context: ContextTypes.DEFAULT_TYPE):
        try:
            async with async_session() as session:
                from sqlalchemy import select

                # Получаем всех пользователей в очереди
                result = await session.execute(select(QueueEntry))
                queue_entries = result.scalars().all()

                processed_users = set()

                for queue_entry in queue_entries:
                    if queue_entry.user_id in processed_users:
                        continue

                    result = await session.execute(select(User).where(User.id == queue_entry.user_id))  # type: ignore
                    user = result.scalar_one_or_none()

                    if not user or user.is_banned or user.current_chat_id:
                        await remove_from_queue(session, queue_entry.user_id)
                        continue

                    # Ищем подходящего собеседника
                    match = await find_match(session, user, queue_entry.role, queue_entry.problem_type)

                    if match:
                        # Проверяем, что собеседник еще не в чате
                        result = await session.execute(select(User).where(User.id == match.id))  # type: ignore
                        match_user = result.scalar_one_or_none()

                        if match_user and not match_user.current_chat_id:
                            # Создаем чат
                            chat = await create_chat(session, user, match_user, queue_entry.role,
                                                     queue_entry.problem_type)
                            await session.commit()

                            # Удаляем обоих из очереди
                            await remove_from_queue(session, user.id)
                            await remove_from_queue(session, match_user.id)
                            processed_users.add(match_user.id)

                            # Отправляем уведомления
                            try:
                                await context.bot.send_message(
                                    chat_id=user.telegram_id,
                                    text=f"✅ Собеседник найден!\n\n"
                                         f"Возраст собеседника: {match_user.age} лет\n"
                                         f"Ваш возраст виден собеседнику: {user.age} лет\n\n"
                                         f"Начните общение!"
                                )

                                await context.bot.send_message(
                                    chat_id=match_user.telegram_id,
                                    text=f"✅ Собеседник найден!\n\n"
                                         f"Возраст собеседника: {user.age} лет\n"
                                         f"Ваш возраст виден собеседнику: {match_user.age} лет\n\n"
                                         f"Начните общение!"
                                )
                            except Exception as e:
                                logger.error(f"Ошибка отправки сообщения: {e}")

                    processed_users.add(user.id)

                await session.commit()

        except Exception as e:
            logger.error(f"Ошибка в периодическом подборе: {e}")

    # Запускаем периодический подбор через job_queue (каждые 5 секунд)
    if application.job_queue:
        application.job_queue.run_repeating(
            periodic_matchmaking_task,
            interval=5,
            first=5
        )
    else:
        logger.warning("JobQueue не доступен. Периодический подбор собеседников не будет работать.")
        logger.warning("Установите python-telegram-bot с job-queue: pip install 'python-telegram-bot[job-queue]'")

    # Запускаем бота
    logger.info("Бот запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
