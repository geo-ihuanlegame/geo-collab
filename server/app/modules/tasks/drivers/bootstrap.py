"""导入副作用模块：一次性注册所有平台发布驱动。

Web 应用（``server/app/main.py:create_app()``）和发布 worker
（``server/worker/executor.py``）是**两个独立进程**，各自维护独立的驱动注册表
``drivers._REGISTRY``。两个进程都 import 本模块，保证注册到**完全一致**的驱动集。

历史回归：wechat_mp（API 驱动）只在 main.py 加了 import，漏了同步 worker，导致 worker
进程里 ``is_api_driver("wechat_mp")`` 返回 False，把公众号账号误判成浏览器发布、抛
``PublishError("浏览器发布需要 storage_state，该账号为 API 接入")``。新增驱动**只改这里一处**，
不要再在 main.py / worker 各加一行——那正是漂移的来源。
"""

from __future__ import annotations

import server.app.modules.tasks.drivers.taptap  # noqa: F401  TapTap cookie-session API 驱动
import server.app.modules.tasks.drivers.toutiao  # noqa: F401  默认 DOM 驱动
import server.app.modules.tasks.drivers.toutiao_inpage  # noqa: F401  页内 API 变体
import server.app.modules.tasks.drivers.wechat_mp  # noqa: F401  微信公众号 API 驱动
