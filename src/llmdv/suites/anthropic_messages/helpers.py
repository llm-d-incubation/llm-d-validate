"""Wording shared by the cases that place a system entry inside a conversation."""

OPENING = "Say hi."
REPLY = "Hi."
# The answer is only knowable from the system entry, and NONE gives the model a way out that
# is not a guess. The marker assertion depends on this exact question.
QUESTION = "Reply with only the code word you were given, or NONE."


def mid_system_request(surface, model, note: str | None, max_tokens: int) -> dict:
    return surface.mid_system_body(
        model.public_id, max_tokens, opening=OPENING, reply=REPLY, question=QUESTION, note=note
    )
