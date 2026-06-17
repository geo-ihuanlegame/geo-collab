"""AI 模型注册表模块：写作 + 格式·配图模型的 DB 化管理。

把"可选模型清单"从环境变量（GEO_AI_ENGINES / GEO_AI_FORMAT_MODEL）搬到 DB，
admin 可增删改、即时生效（DB 行实时读、不缓存）；**密钥永不入库**——行只存
api_key_env（环境变量名），运行时按名从 env 取，没设则回落该用途全局 Key。
GEO_AI_ENGINES + 两个默认模型作为首次播种与回落。
"""
