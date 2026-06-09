"""客户端可见异常族（HTTP 4xx）。

服务层抛这些命名异常，由 main.create_app 的全局处理器映射到对应状态码：
ConflictError→409、ValidationError/AccountError→400、ClientError→400。
注意：没有针对裸 ValueError 的兜底，服务层不要抛 ValueError。
"""


class ClientError(Exception):
    """所有客户端可见异常的基类（HTTP 4xx）；服务层用它替代 ValueError。"""


class ConflictError(ClientError):
    """检测到乐观版本冲突或幂等冲突时抛出（HTTP 409）。"""


class ValidationError(ClientError):
    """用户输入校验失败时抛出（HTTP 400）。"""


class AccountError(ClientError):
    """账号相关错误时抛出，例如过期、不存在或平台不匹配（HTTP 400）。"""
