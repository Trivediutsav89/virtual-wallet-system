"""Models package. Importing it registers every table on ``Base.metadata``."""

from app.models.base import Base
from app.models.user import User

__all__ = ["Base", "User"]
