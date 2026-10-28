from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from app.config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def init_db():
    try:
        import app.models  # noqa: F401
        Base.metadata.create_all(bind=engine)
        if "sqlite" in DATABASE_URL:
            with engine.connect() as conn:
                cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(schedules)").fetchall()]
                if "reference_image" not in cols:
                    conn.exec_driver_sql("ALTER TABLE schedules ADD COLUMN reference_image VARCHAR")
                if "compartment_number" not in cols:
                    conn.exec_driver_sql("ALTER TABLE schedules ADD COLUMN compartment_number INTEGER DEFAULT 1")
                conn.commit()
    except Exception:
        pass

init_db()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
