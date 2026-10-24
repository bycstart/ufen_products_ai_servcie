"""切换到本地 Docker 依赖；先备份，再更新指定配置，保留模型参数。"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import secrets
from zipfile import ZIP_DEFLATED, ZipFile


LOCAL = Path(__file__).resolve().parent
AI = LOCAL.parents[1]
SOURCE = AI.parents[1]
ECOMMERCE = SOURCE / 'backend/backend/ecommerce-service'
CUSTOMER = SOURCE / 'customer-service/customer-service'
FRONTENDS = [SOURCE / 'frontend/frontend' / name for name in ('user-frontend', 'admin-frontend')]


def read_values(path: Path) -> dict:
    """这里只读取连接字段；写回时逐行保留不相关内容及中文注释。"""
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        match = re.match(r'^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*?)\s*$', line)
        if match:
            values[match[1]] = match[2].strip('\"\'')
    return values


def updated_text(path: Path, updates: dict) -> str:
    lines = path.read_text(encoding='utf-8-sig').splitlines() if path.exists() else []
    remaining = dict(updates)
    output = []
    for line in lines:
        match = re.match(r'^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=', line)
        if match and match[1] in updates:
            key = match[1]
            if key in remaining:
                output.append(f'{key}={remaining.pop(key)}')
        else:
            output.append(line)
    output.extend(f'{key}={value}' for key, value in remaining.items())
    return '\n'.join(output) + '\n'


def configurations(local_values: dict) -> dict:
    shared = {'JWT_SECRET': local_values['JWT_SECRET'], 'JWT_ALGORITHM': 'HS256'}
    internal = {'INTERNAL_SERVICE_TOKEN': local_values['INTERNAL_SERVICE_TOKEN']}
    postgres = (f"postgresql+psycopg://customer_service:{local_values['POSTGRES_PASSWORD']}"
                f"@127.0.0.1:{local_values['POSTGRES_PORT']}/")
    files = {
        AI / '.env': {**shared, **internal, 'AI_DATABASE_URL': postgres + 'ai_customer_service',
                      'ECOMMERCE_BASE_URL': 'http://127.0.0.1:8001/api/v1'},
        ECOMMERCE / '.env': {**shared, 'ECOM_DATABASE_URL': postgres + 'ecommerce'},
        CUSTOMER / '.env': {**shared, **internal, 'DATABASE_URL': postgres + 'customer_service',
                            'REDIS_URL': f"redis://127.0.0.1:{local_values['REDIS_PORT']}/0",
                            'AI_SERVICE_URL': 'http://127.0.0.1:8002'},
    }
    for frontend in FRONTENDS:
        files[frontend / '.env.local'] = {
            'VITE_ECOMMERCE_SERVICE_URL': 'http://127.0.0.1:8001',
            'VITE_CUSTOMER_SERVICE_URL': 'http://127.0.0.1:8000',
            'VITE_AI_SERVICE_URL': 'http://127.0.0.1:8002',
        }
    return files


def verify_local_configuration() -> None:
    """同时核对磁盘和进程环境，避免 Pydantic 优先级使本地服务误连旧环境。"""
    values = read_values(LOCAL / '.env')
    required = ('POSTGRES_PASSWORD', 'JWT_SECRET', 'INTERNAL_SERVICE_TOKEN', 'POSTGRES_PORT', 'REDIS_PORT')
    if not all(values.get(key) for key in required):
        raise SystemExit('本地 Docker 配置尚未生成')
    environment = {key.upper(): value for key, value in os.environ.items()}
    for path, updates in configurations(values).items():
        current = read_values(path)
        for key, value in updates.items():
            if current.get(key) != value:
                raise SystemExit(f'本地配置不匹配：{path.parent.name}/{key}')
            if key in environment and environment[key] != value:
                raise SystemExit(f'进程环境变量覆盖了本地配置，请先清除该变量：{key}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='只检查本地配置是否匹配，不输出密钥')
    args = parser.parse_args()
    if not all((root / 'pyproject.toml').is_file() for root in (AI, ECOMMERCE, CUSTOMER)):
        raise SystemExit('服务目录结构不匹配，未修改配置')
    local_env = LOCAL / '.env'
    values = read_values(local_env)
    defaults = {'POSTGRES_PASSWORD': secrets.token_hex(24), 'JWT_SECRET': secrets.token_hex(32),
                'INTERNAL_SERVICE_TOKEN': secrets.token_hex(32), 'POSTGRES_PORT': '15432',
                'REDIS_PORT': '16379'}
    if args.check:
        verify_local_configuration()
        print('本地连接、JWT、内部令牌及前端地址检查通过（未输出密钥）')
        return
    for key, value in defaults.items():
        values.setdefault(key, value)
    files = configurations(values)
    # 写入前验证全部目标目录，备份完整原文（含敏感配置，仅保留在本机）。
    if any(not path.parent.is_dir() for path in files):
        raise SystemExit('配置目标目录缺失，未修改配置')
    backup = AI / 'backups' / f'before-local-docker-{datetime.now():%Y%m%d-%H%M%S-%f}.zip'
    backup.parent.mkdir(exist_ok=True)
    manifest = []
    with ZipFile(backup, 'w', ZIP_DEFLATED) as archive:
        for path in [*files, local_env]:
            name = str(path.relative_to(SOURCE)).replace('\\', '/')
            manifest.append({'path': name, 'existed': path.exists()})
            if path.exists():
                archive.write(path, name)
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    for path, updates in files.items():
        path.write_text(updated_text(path, updates), encoding='utf-8')
    local_env.write_text(updated_text(local_env, values), encoding='utf-8')
    print(f'原配置已备份：{backup.name}')
    print('本地配置切换完成；模型配置保持原值，重启服务后生效')


if __name__ == '__main__':
    main()
