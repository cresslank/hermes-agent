"""CLI notification ownership, structured queueing and last-moment consumption."""


class CLIProcessNotificationsMixin:
    def _owns_process_notification(self, event: dict) -> bool:
        """Whether this session owns a delegation event (pre-compression keys resolve to their continuation; fail closed)."""
        event_key = str(event.get("session_key") or "")
        current_key = str(getattr(self, "session_id", "") or "")
        if not event_key or not current_key:
            return False
        if event_key == current_key:
            return True
        try:
            session_db = getattr(self, "_session_db", None)
            resolved_key = (
                session_db.resolve_resume_session_id(event_key) if session_db is not None else event_key
            ) or event_key
        except Exception:
            resolved_key = event_key
        return str(resolved_key) == current_key

    def _drain_process_notifications(self, consumer: str) -> None:
        from tools.process_registry import process_registry
        from tools.async_delegation import claim_event_delivery, complete_event_delivery
        from tools.process_registry_notifications import (
            ProcessNotificationBatch, TimelineNotification, group_process_notifications)

        from agent.completion_admission import requeue_due
        try:
            requeue_due(process_registry.completion_queue, getattr(self, "session_id", ""))
        except Exception:
            pass  # retry through the next existing idle pass
        claimed = []
        for event, text in process_registry.drain_notifications(
            session_key=getattr(self, "session_id", "") or "", owns_event=self._owns_process_notification,
        ):
            claim = claim_event_delivery(event, consumer)
            if claim is None:
                continue
            from agent.completion_admission import prepare_event, accept_event
            from tools.async_delegation import defer_completion_delivery
            try:
                prepared = prepare_event(event, claim, getattr(self, "session_id", ""))
                if not prepared.present:
                    continue
                capacity = getattr(self._pending_input, 'maxsize', 0) or 256
                if self._pending_input.qsize() >= capacity or not accept_event(event, capacity=capacity):
                    if claim:
                        defer_completion_delivery(event["delegation_id"], claim)
                    process_registry.completion_queue.put(event)
                    continue
            except Exception:
                process_registry.completion_queue.put(event)
                if claim:
                    defer_completion_delivery(event["delegation_id"], claim)
                continue
            from agent.completion_admission import presentation_text
            text = presentation_text(event, text)
            claimed.append((event, text))
            if not event.get("supervision_delivery_id"):
                complete_event_delivery(event, claim)
        for notifications in group_process_notifications(claimed):
            event, text = notifications[0]
            if event.get("type", "completion") == "completion":
                pending = ProcessNotificationBatch(notifications)
            else:
                pending = TimelineNotification.for_delegation(text, event) if event.get("type") == "async_delegation" else text
                from agent.notification_presentation import diagnostic_process_event
                if diagnostic_process_event(event) and not isinstance(pending, TimelineNotification):
                    pending = TimelineNotification(text, text, "internal_notification", "diagnostic")
            from queue import Full
            try:
                self._pending_input.put_nowait(pending)
            except Full:
                # Reservation survives a lost hint; idle/startup recovery retries it.
                for queued_event, _queued_text in notifications:
                    process_registry.completion_queue.put(queued_event)

    def _tui_unwrap_input(self, user_input):
        """Unwrap ``_VoiceInputMessage`` / ``_SeededQueryMessage`` -> ``(text_or_tuple, is_voice_input, is_seeded_query)``."""
        from cli import _VoiceInputMessage, _SeededQueryMessage
        from tools.process_registry import process_registry
        from tools.process_registry_notifications import (
            PROCESS_COMPLETE_DISPLAY_KIND, ProcessNotificationBatch, TimelineNotification)
        if isinstance(user_input, ProcessNotificationBatch):
            rendered = user_input.render(process_registry)
            user_input = rendered and TimelineNotification(
                rendered, user_input.display_text(process_registry), PROCESS_COMPLETE_DISPLAY_KIND)
        metadata = getattr(user_input, "supervision_metadata", None)
        if metadata:
            from agent.completion_admission import valid_hint
            if not valid_hint(metadata, getattr(self, "session_id", None)):
                return None, False, False
        # Voice-transcribed messages arrive wrapped in a sentinel so only genuine STT output gets the voice
        # prefix (#65827).
        is_voice_input = isinstance(user_input, _VoiceInputMessage)
        if is_voice_input:
            user_input = user_input.text
        is_seeded_query = isinstance(user_input, _SeededQueryMessage)
        if is_seeded_query:
            user_input = (user_input.text, user_input.images) if user_input.images else user_input.text
        return user_input, is_voice_input, is_seeded_query

