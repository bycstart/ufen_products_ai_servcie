"""本机 API 启动入口，统一使用 Psycopg 兼容的事件循环。"""
import argparse
import asyncio
import os
import selectors
import sys

import uvicorn

from configure import AI, CUSTOMER, ECOMMERCE, verify_local_configuration


SERVICES = {
    'ecommerce': (ECOMMERCE, 'ecommerce_service.main:app', 8001),
    'customer': (CUSTOMER, 'atguigu.app.app:app', 8000),
    'ai': (AI, 'atguigu.app.app:app', 8002),
}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('service', choices=SERVICES)
    root, app, port = SERVICES[parser.parse_args().service]
    verify_local_configuration()
    os.chdir(root)
    sys.path.insert(0, str(root))
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, loop='none'))
    asyncio.run(server.serve(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
