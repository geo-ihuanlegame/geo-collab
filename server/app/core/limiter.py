"""全局限流器单例。

挂到 app.state.limiter（见 main.create_app），按客户端 IP 限流。
需要限流的端点用 @limiter.limit(...) 装饰。
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
