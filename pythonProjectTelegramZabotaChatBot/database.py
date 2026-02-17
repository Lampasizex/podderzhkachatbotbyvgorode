from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from datetime import datetime

Base = declarative_base()


class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False, index=True)
    age = Column(Integer, nullable=False)
    is_banned = Column(Boolean, default=False)
    registered_at = Column(DateTime, default=datetime.utcnow)
    current_chat_id = Column(Integer, ForeignKey('chats.id'), nullable=True)
    current_role = Column(String, nullable=True)  # support или receive_support
    current_problem = Column(String, nullable=True)  # stress или anxiety

    # Связи
    chats = relationship("Chat", foreign_keys="Chat.user1_id", back_populates="user1")
    chats2 = relationship("Chat", foreign_keys="Chat.user2_id", back_populates="user2")
    messages = relationship("Message", back_populates="user")
    chat_history = relationship("ChatHistory", back_populates="user")


class Chat(Base):
    __tablename__ = 'chats'

    id = Column(Integer, primary_key=True)
    user1_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    user2_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    role1 = Column(String, nullable=False)  # support или receive_support
    role2 = Column(String, nullable=False)
    problem_type = Column(String, nullable=False)  # stress или anxiety
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    ended_at = Column(DateTime, nullable=True)

    # Связи
    user1 = relationship("User", foreign_keys=[user1_id], back_populates="chats")
    user2 = relationship("User", foreign_keys=[user2_id], back_populates="chats2")
    messages = relationship("Message", back_populates="chat")


class Message(Base):
    __tablename__ = 'messages'

    id = Column(Integer, primary_key=True)
    chat_id = Column(Integer, ForeignKey('chats.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    text = Column(Text, nullable=False)
    sent_at = Column(DateTime, default=datetime.utcnow)

    # Связи
    chat = relationship("Chat", back_populates="messages")
    user = relationship("User", back_populates="messages")


class ChatHistory(Base):
    __tablename__ = 'chat_history'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    chat_id = Column(Integer, ForeignKey('chats.id'), nullable=False)
    viewed_at = Column(DateTime, default=datetime.utcnow)

    # Связи
    user = relationship("User", back_populates="chat_history")
    chat = relationship("Chat")


class QueueEntry(Base):
    __tablename__ = 'queue_entries'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), unique=True, nullable=False)
    role = Column(String, nullable=False)  # support или receive_support
    problem_type = Column(String, nullable=False)  # stress или anxiety
    age = Column(Integer, nullable=False)
    joined_at = Column(DateTime, default=datetime.utcnow)

    # Связи
    user = relationship("User")


# Создание движка и сессии
engine = create_async_engine('sqlite+aiosqlite:///bot_database.db', echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """Инициализация базы данных"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session():
    """Получение сессии базы данных"""
    async with async_session() as session:
        yield session