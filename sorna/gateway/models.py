import asyncio
from datetime import datetime
import enum
import logging
import uuid

import asyncpg, asyncpgsa as pg
import sqlalchemy as sa
from sqlalchemy.types import TypeDecorator, CHAR
from sqlalchemy.dialects.postgresql import UUID

from .config import load_config, init_logger

log = logging.getLogger('sorna.gateway.models')
metadata = sa.MetaData()

default_user_email = 'x-zmq-user@sorna.io'


class CurrencyTypes(enum.Enum):
    KRW = 'KRW'
    USD = 'USD'


class GUID(TypeDecorator):
    '''
    Platform-independent GUID type.
    Uses PostgreSQL's UUID type, otherwise uses CHAR(16) storing as raw bytes.
    '''
    impl = CHAR

    def load_dialect_impl(self, dialect):
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(UUID())
        else:
            return dialect.type_descriptor(CHAR(16))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        elif dialect.name == 'postgresql':
            return str(value)
        else:
            if isinstance(value, uuid.UUID):
                return value.bytes
            else:
                return uuid.UUID(value).bytes

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        else:
            return uuid.UUID(value)


def IDColumn(name='id'):
    return sa.Column('id', GUID, primary_key=True,
                     default=uuid.uuid4,
                     server_default=sa.text("uuid_generate_v4()"))


User = sa.Table(
    'users', metadata,
    IDColumn('id'),
    sa.Column('email', sa.String(length=254)),
    sa.Column('password', sa.String(length=128)),
    sa.Column('created_at', sa.DateTime),
    sa.Column('last_login', sa.DateTime, nullable=True),
    # TODO: add payment information
    sa.Index('idx_email', 'email', unique=True),
)

KeyPair = sa.Table(
    'keypairs', metadata,
    sa.Column('access_key', sa.String, primary_key=True),
    sa.Column('secret_key', sa.String),
    sa.Column('belongs_to', sa.ForeignKey('users.id')),
    sa.Column('created_at', sa.DateTime),
    sa.Column('last_used', sa.DateTime, nullable=True),
    sa.Column('total_num_queries', sa.Integer, server_default='0'),
    sa.Column('num_queries', sa.Integer, server_default='0'),  # reset every month
    # Below limits are per-month.
    # NOTE: per-day calculation upon creation of a new key?
    sa.Column('cpu_limit', sa.Integer),
    sa.Column('mem_limit', sa.Integer),
    sa.Column('io_limit', sa.Integer),
    sa.Column('net_limit', sa.Integer),
    # NOTE: API rate-limiting is done using Redis, not DB.
)

Usage = sa.Table(
    'usage', metadata,
    IDColumn('id'),
    sa.Column('access_key', sa.ForeignKey('keypairs.access_key')),
    sa.Column('kernel_type', sa.String),
    sa.Column('kernel_id', sa.String),
    sa.Column('launched_at', sa.DateTime),
    sa.Column('terminated_at', sa.DateTime),
    sa.Column('cpu_used', sa.Integer, server_default='0'),
    sa.Column('mem_used', sa.Integer, server_default='0'),
    sa.Column('io_used', sa.Integer, server_default='0'),
    sa.Column('net_used', sa.Integer, server_default='0'),
    sa.Index('idx_ktype', 'kernel_type'),
    sa.Index('idx_launch', 'launched_at'),
)

# Bill is regularly calculated by summing Usage records for a given month.
# Each month is a calendar month.

Bill = sa.Table(
    'bills', metadata,
    IDColumn('id'),
    sa.Column('user', sa.ForeignKey('users.id')),
    sa.Column('access_key', sa.ForeignKey('keypairs.access_key')),
    sa.Column('month', sa.Date),  # first day of the billing month
    sa.Column('total_cpu_used', sa.Integer),
    sa.Column('total_mem_used', sa.Integer),
    sa.Column('total_io_used', sa.Integer),
    sa.Column('total_net_used', sa.Integer),
    sa.Column('total_queries', sa.Integer),
    sa.Column('amount', sa.Integer),
    sa.Column('currency', sa.Enum(CurrencyTypes)),
    sa.Index('idx_monthly_bill', 'user', 'access_key', 'month', unique=True),
)


def mock_engine():
    '''
    Creates a pair of io.StringIO buffer object and a mock engine that does NOT
    execute SQL queries but only dumps the compiled statements into the buffer
    object.
    '''
    from sqlalchemy import create_engine as ce
    from io import StringIO
    buf = StringIO()

    def dump(sql, *multiparams, **params):
        buf.write(str(sql.compile(dialect=engine.dialect)) + ';\n')

    engine = ce('postgresql://', echo=True, strategy='mock', executor=dump)
    return buf, engine


def generate_sql(sa_callable):
    '''
    Generates a compiled SQL statement as a string from SQLAlchemy methods that
    accepts engine as the first function argument.
    '''
    buf, engine = mock_engine()
    sa_callable(engine)
    return buf.getvalue()


if __name__ == '__main__':

    def model_args(parser):
        parser.add('--drop-tables', action='store_true', default=False,
                   help='Drops all tables.')
        parser.add('--create-tables', action='store_true', default=False,
                   help='Creates all tables. If anyone already exists, it will raise an error.')
        parser.add('--populate-fixtures', action='store_true', default=False,
                   help='Populates initial fixture data.')

    config = load_config(extra_args_func=model_args)
    init_logger(config)

    async def drop_tables(config, pool):
        async with pool.acquire() as conn:
            log.warning('Dropping tables... (all data is lost!)')
            await conn.execute(generate_sql(metadata.drop_all))

    async def create_tables(config, pool):
        async with pool.acquire() as conn:
            log.info('Creating tables...')
            # Load an extension to use "uuid_generate_v4()" SQL function
            await conn.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')
            await conn.execute(generate_sql(metadata.create_all))

    async def populate_fixtures(config, pool):
        log.info('Populating fixtures...')
        async with pool.acquire() as conn:
            default_user_cnt = await conn.fetchval(
                User.count(User.c.email == default_user_email),
                column=0)
            if default_user_cnt == 0:
                log.info('Creating the default legacy ZMQ API user')
                uid = uuid.uuid4()
                await conn.execute(User.insert().values(
                    id=uid,
                    email=default_user_email,
                    password='x-unused',
                    created_at=datetime.utcnow(),
                ))
                log.info('Creating a default keypair for the ZMQ user')
                await conn.execute(KeyPair.insert().values(
                    belongs_to=uid,
                    access_key='AKIAIOSFODNN7EXAMPLE',
                    secret_key='wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
                    created_at=datetime.utcnow(),
                    # since this is a locally-served legacy user,
                    # we set all limits infinte.
                    cpu_limit=-1,
                    mem_limit=-1,
                    io_limit=-1,
                    net_limit=-1,
                ))

    async def init_db(config):
        async with pg.create_pool(
            host=config.db_addr[0],
            port=config.db_addr[1],
            user=config.db_user,
            password=config.db_password,
            database=config.db_name,
            min_size=1, max_size=3,
        ) as pool:
            if config.drop_tables:
                await drop_tables(config, pool)
            if config.create_tables:
                await create_tables(config, pool)
            if config.populate_fixtures:
                await populate_fixtures(config, pool)

    loop = asyncio.get_event_loop()
    log.info('NOTICE: If you see SQL errors, you may need to drop and recreate tables.')
    try:
        loop.run_until_complete(init_db(config))
    finally:
        log.info('Done.')
        loop.close()
