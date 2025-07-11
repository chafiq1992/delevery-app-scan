from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship
import os
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable not set")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

Base = declarative_base()

class Driver(Base):
    __tablename__ = "drivers"
    id = Column(String, primary_key=True)
    order_tab = Column(String)
    payouts_tab = Column(String)

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, autoincrement=True)
    driver_id = Column(String, ForeignKey("drivers.id"))
    timestamp = Column(DateTime, default=datetime.utcnow)
    order_name = Column(String, index=True)
    customer_name = Column(String)
    customer_phone = Column(String)
    address = Column(Text)
    tags = Column(String)
    fulfillment = Column(String)
    order_status = Column(String)
    store = Column(String)
    delivery_status = Column(String)
    notes = Column(Text)
    scheduled_time = Column(String)
    scan_date = Column(String)
    cash_amount = Column(Float)
    driver_fee = Column(Float)
    payout_id = Column(String)
    status_log = Column(Text)
    comm_log = Column(Text)

    driver = relationship("Driver")

class Payout(Base):
    __tablename__ = "payouts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    driver_id = Column(String, ForeignKey("drivers.id"))
    payout_id = Column(String, index=True)
    date_created = Column(DateTime, default=datetime.utcnow)
    orders = Column(Text)
    total_cash = Column(Float)
    total_fees = Column(Float)
    total_payout = Column(Float)
    status = Column(String)
    date_paid = Column(DateTime)

    driver = relationship("Driver")

class EmployeeLog(Base):
    __tablename__ = "employee_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    employee = Column(String)
    order = Column(String)
    amount = Column(Float)

async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
