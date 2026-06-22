"""`python -m server.mcp` 入口。

直接 `python -m server.mcp.server` 会触发 Python 的 __main__ vs 包模块双实例 bug：
__main__ 里 `mcp = FastMCP(...)` 建一个实例，tools/*.py 通过 `from server.mcp.server
import mcp` 拿到另一个实例，结果 `mcp.run()` 跑的实例没注册任何 tool。
本 shim 把入口移到 `server.mcp` 包，server.py 只通过 import 进入，全局只剩一个实例。
"""

from server.mcp.server import main

main()
