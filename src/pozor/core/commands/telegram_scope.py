"""Canonical conversation scope for Telegram group messages and callbacks."""

from collections.abc import Mapping
from typing import Any


def topic_id(message: Mapping[str, Any]) -> int:
    """Ordinary group reply chains are not independent forum topics."""
    chat = message.get('chat')
    forum = isinstance(chat, Mapping) and chat.get('is_forum') is True
    if not forum and message.get('is_topic_message') is not True:
        return 0
    thread = message.get('message_thread_id')
    return thread if type(thread) is int and thread > 0 else 0
