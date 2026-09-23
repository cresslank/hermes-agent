"""The native child context recipe, shared by construction, run and read-only preflight.

Separate conversation is not independent training, competence, or a filesystem sandbox.
No constructor, credential resolution, reservation or spawn accounting happens here.
"""


def constructor_context(parent):
    return dict(prefill_messages=getattr(parent, "prefill_messages", None),
                skip_context_files=True, skip_memory=True, iteration_budget=None)


def run_context(user_message, task_id, stream_callback):
    # Deliberately no conversation_history, inherited messages or system_message.
    return dict(user_message=user_message, task_id=task_id, stream_callback=stream_callback)


def separate_conversation(parent):
    recipe = constructor_context(parent)
    return (not recipe["prefill_messages"] and recipe["skip_context_files"] is True
            and recipe["skip_memory"] is True and recipe["iteration_budget"] is None
            and "conversation_history" not in run_context("", "", None))
