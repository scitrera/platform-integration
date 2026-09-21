"""Serialize and invoke MemoryLayer's component-owned fresh/upgrade initializer."""
import asyncio
import os
from urllib.parse import parse_qsl, urlencode, urlsplit


def lock_dsn(url):
    """Convert the SQLAlchemy asyncpg URL to native asyncpg DSN syntax."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if sum(key in {"ssl", "sslmode"} for key, _ in query) > 1:
        raise ValueError("Database URL has ambiguous TLS settings")
    return parts._replace(scheme="postgresql", query=urlencode([
        ("sslmode" if key == "ssl" else key, value) for key, value in query])).geturl()

async def main():
    import asyncpg
    from memorylayer_saas.dependencies import preconfigure
    from memorylayer_saas.storage.postgresql import PostgreSQLBackend
    url=os.environ['MEMORYLAYER_POSTGRESQL_URL']
    lock=await asyncpg.connect(lock_dsn(url))
    backend=None
    try:
        # The lock is held across the component's separate DDL connections.
        await lock.execute("SET statement_timeout = '240s'")
        await lock.execute("SELECT pg_advisory_lock(hashtext('platform-memorylayer-migration'))")
        variables,_=preconfigure()
        backend=PostgreSQLBackend(v=variables,connection_string=url.replace("postgresql://","postgresql+asyncpg://"))
        await backend.connect()
        if not await backend.health_check():raise RuntimeError('MemoryLayer storage readiness failed')
        expected=int(os.environ.get('MEMORYLAYER_EMBEDDING_DIMENSIONS','1536'))
        columns=await lock.fetch("""SELECT c.relname, a.attname, pg_catalog.format_type(a.atttypid,a.atttypmod) AS vector_type
            FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND a.attnum>0 AND NOT a.attisdropped
              AND pg_catalog.format_type(a.atttypid,a.atttypmod) LIKE 'vector(%)'""")
        wrong=[row['relname']+'.'+row['attname'] for row in columns if row['vector_type']!='vector('+str(expected)+')']
        if wrong:raise RuntimeError('Embedding dimension differs from existing schema: '+', '.join(wrong))
        print('MemoryLayer schema initialization and embedding dimension verified')
    finally:
        if backend is not None:await backend.disconnect()
        await lock.close()

if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(),timeout=300))
