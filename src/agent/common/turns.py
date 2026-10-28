from .snowflake import new_id
TURN_ID_PREFIX="turn_"

def ensure_turn_id(request) -> str:
    """取这一次的轮次 id；缺省（或全空白）时服务端铸一个并**写回请求**（响应要回显）。"""
    turn_id = str(getattr(request, "turn_id", "") or "").strip()
    if turn_id:
        return turn_id
    turn_id = new_id(TURN_ID_PREFIX)
    request.turn_id = turn_id
    return turn_id

