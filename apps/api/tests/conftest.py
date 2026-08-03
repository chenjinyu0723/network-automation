from __future__ import annotations

import pytest
from app.db import Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def session():  # type: ignore[no-untyped-def]
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    database_session = maker()
    try:
        yield database_session
    finally:
        database_session.close()
