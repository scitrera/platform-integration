"""Serialize and invoke MemoryLayer's component-owned fresh/upgrade initializer."""
import asyncio
import os
import asyncpg
from memorylayer_saas.dependencies import preconfigure
from memorylayer_saas.storage.postgresql import PostgreSQLBackend

async def main():
    url=os.environ['MEMORYLAYER_POSTGRESQL_URL']
    lock=await asyncpg.connect(url.replace('postgresql+asyncpg://','postgresql://'))
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

asyncio.run(asyncio.wait_for(main(),timeout=300))
