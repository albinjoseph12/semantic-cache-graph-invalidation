"""PostgreSQL integration regression. Needs asyncpg and PG_DSN pointing to a test database.
Uses a dedicated schema with a random name and removes only that schema on exit.
Usage: python postgres_snapshot_test.py /path/to/corrected_semcache-testbed
"""
import ast
import asyncio
import os
import sys
import uuid
from pathlib import Path

def load_build_context(source):
    """Load the archived reader without starting the gateway or its dependencies."""
    tree = ast.parse(source.read_text(), filename=str(source))
    reader = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == 'build_context'
    )
    module = ast.Module(body=[reader], type_ignores=[])
    namespace = {}
    exec(compile(module, str(source), 'exec'), namespace)
    return namespace['build_context']


async def run():
    import asyncpg
    reader = await asyncpg.connect(os.environ['PG_DSN'])
    writer = await asyncpg.connect(os.environ['PG_DSN'])
    schema = 'semcache_regression_' + uuid.uuid4().hex
    build = load_build_context(Path(sys.argv[1]) / 'app/main.py')
    try:
        await reader.execute(f'CREATE SCHEMA {schema}')
        for conn in (reader,writer):
            await conn.execute(f'SET search_path TO {schema}')
        await reader.execute('CREATE TABLE products(id integer,name text,price_cents integer,stock integer,version integer,category text); CREATE TABLE categories(category text,epoch integer)')
        class Interleave:
            async def fetch(self,sql,*args):
                rows=await reader.fetch(sql,*args)
                async with writer.transaction():
                    await writer.execute('UPDATE products SET price_cents=500,version=2')
                    await writer.execute('UPDATE categories SET epoch=2')
                return rows
            async def fetchval(self,sql,*args):
                return await reader.fetchval(sql,*args)
        for snapshot in (False,True):
            await reader.execute("TRUNCATE products,categories; INSERT INTO products VALUES(1,'Lamp',1000,1,1,'lighting'); INSERT INTO categories VALUES('lighting',1)")
            if snapshot:
                async with reader.transaction(isolation='repeatable_read',readonly=True):
                    ctx,versions,_=await build(Interleave(),['category:lighting'])
            else:
                ctx,versions,_=await build(Interleave(),['category:lighting'])
            current=await reader.fetchval('SELECT epoch FROM categories')
            assert ctx['category:lighting'][0]['price_cents']==1000
            assert current==2 and versions['category:lighting']==(1 if snapshot else 2)
            print(dict(snapshot=snapshot,observed_stamp=versions['category:lighting'],current_stamp=current))
    finally:
        try:
            await reader.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        finally:
            await writer.close()
            await reader.close()
if __name__=='__main__':
    asyncio.run(run())
