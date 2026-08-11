"""Pull the requested reply shape out of a question message.

Question text ends with a template like
    {"answer": {"state": "<state name>"}, "log_url": "<public URL>"}
or
    {"values": [<numbers>]}

The first is valid JSON, the second is not (`[<numbers>]`), so everything here
is a tolerant scanner rather than json.loads.
"""
import json


def _balanced_objects(text):
    """Every top-level {...} span in text, string- and escape-aware."""
    out, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                out.append(text[start:i + 1])
    return out


def extract_shape(text):
    """The template the message is asking us to reply with, or None."""
    objs = _balanced_objects(text or "")
    return objs[-1] if objs else None


def top_level_fields(tpl):
    """{"a": 1, "b": {"c": 2}} -> {"a": "1", "b": '{"c": 2}'} (raw substrings)."""
    body = tpl.strip()
    if not (body.startswith("{") and body.endswith("}")):
        return {}
    body = body[1:-1]

    parts, depth, in_str, esc, buf = [], 0, False, False, []
    for ch in body:
        if in_str:
            buf.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))

    fields = {}
    for part in parts:
        depth, in_str, esc, cut = 0, False, False, None
        for i, ch in enumerate(part):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
            elif ch == ":" and depth == 0:
                cut = i
                break
        if cut is None:
            continue
        key = part[:cut].strip().strip('"').strip()
        fields[key] = part[cut + 1:].strip()
    return fields


def plan(tpl):
    """What to ask the model for, and how to assemble the final reply.

    Returns (inner_template, wrapper) where wrapper is True when the reply must
    be {"answer": ..., "log_url": ...} and inner_template is the shape of the
    "answer" value alone.
    """
    if not tpl:
        return None, False
    fields = top_level_fields(tpl)
    if "answer" in fields and "log_url" in fields:
        return fields["answer"], True
    return tpl, False


def assemble(answer, wrapper, log_url):
    if not wrapper:
        return answer
    # The model is asked for the inner value only, but sometimes returns the
    # whole wrapper anyway. Wrapping that again produces
    # {"answer": {"answer": ..., "log_url": ...}, "log_url": ...}, which never
    # matches. Unwrap one level when it is unmistakably our own wrapper.
    if (isinstance(answer, dict) and set(answer) == {"answer", "log_url"}):
        answer = answer["answer"]
    return {"answer": answer, "log_url": log_url}


def fallback(tpl, log_url):
    """Something structurally valid to send when the agent fails outright.

    A wrong answer and a format error both score zero, but a parseable reply
    keeps the attempt's status at 'ok' instead of 'timeout' — and timeout is
    terminal in collect.py, never retried.
    """
    inner, wrapper = plan(tpl)
    guess = None
    try:
        guess = _blank(json.loads(inner)) if inner else None
    except Exception:
        fields = top_level_fields(inner or "")
        guess = {k: None for k in fields} or None
    return assemble(guess, wrapper, log_url)


def _blank(value):
    """Replace "<placeholder>" strings with None so we never send the template
    back verbatim."""
    if isinstance(value, dict):
        return {k: _blank(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_blank(v) for v in value]
    if isinstance(value, str) and value.startswith("<") and value.endswith(">"):
        return None
    return value


if __name__ == "__main__":
    for t in [
        'Reply with ONLY {"answer": {"state": "<state name>"}, "log_url": "<url>"}',
        'Reply with ONLY {"values": [<numbers>]}',
        'Reply with ONLY a JSON object like {"state": "<state name>"}',
    ]:
        tpl = extract_shape(t)
        print(tpl, "->", plan(tpl))
