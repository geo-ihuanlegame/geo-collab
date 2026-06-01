from sqlalchemy.orm import DeclarativeBase


# SQLAlchemy 声明式基类，所有 ORM 模型继承此类
class Base(DeclarativeBase):
    pass

