"""Transient, allowlisted table interactions for the Lotus WS rooms."""

import secrets
import time


REQUEST_KIND = 'table_interaction_request'
QUICK_CHAT_IDS = frozenset({
    'listen', 'hurry', 'praise', 'think', 'let-chi', 'hello', 'stay',
    'let-draw', 'feed-peng', 'hurt', 'oops-hu', 'no-friends', 'hundred-ways',
})
EMOTE_IDS = frozenset({'tomato', 'egg', 'pan', 'banana', 'kiss', 'like'})


class TableInteractionRelay:
    def __init__(self):
        self._hand = None
        self._scope: str | None = None
        self._id = 0
        self._chats: dict[int, tuple[str, float]] = {}
        self._emotes: dict[int, float] = {}

    def scope_for(self, hand: object | None) -> str | None:
        if hand != self._hand:
            self._hand = hand
            self._scope = secrets.token_hex(12) if hand is not None else None
            self._chats.clear()
            self._emotes.clear()
        return self._scope

    def accept(self, seat: int, message: dict, scope: str | None) -> dict | None:
        action = message.get('action')
        if not scope or scope != message.get('scope') or not isinstance(action, dict):
            return None

        now = time.monotonic()
        if action.get('type') == 'quick_chat':
            phrase = action.get('phraseId')
            if not isinstance(phrase, str) or phrase not in QUICK_CHAT_IDS:
                return None
            previous, last = self._chats.get(seat, ('', -float('inf')))
            if now - last < (5 if previous == phrase else 0.7):
                return None
            self._chats[seat] = (phrase, now)
            accepted = {'type': 'quick_chat', 'phraseId': phrase}
        elif action.get('type') == 'emote':
            emote, target = action.get('emote'), action.get('targetSeat')
            if (not isinstance(emote, str) or emote not in EMOTE_IDS
                    or type(target) is not int or not 0 <= target < 4 or target == seat):
                return None
            if now - self._emotes.get(seat, -float('inf')) < 0.35:
                return None
            self._emotes[seat] = now
            accepted = {'type': 'emote', 'emote': emote, 'targetSeat': target}
        else:
            return None

        self._id += 1
        return {'kind': 'table_interaction', 'scope': scope, 'id': self._id,
                'fromSeat': seat, 'action': accepted}
