"""异步调用 FastAPI 执行客服20问，逐题保存报告，不输出密钥。"""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys
from time import perf_counter

import httpx

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from atguigu.common.config import get_settings


async def post_with_progress(client, url, headers, case_id):
    """等待期间持续输出心跳；真实模型和工具统计在接口完成后展示。"""
    started = perf_counter()
    task = asyncio.create_task(client.post(url, headers=headers, json={'case_id': case_id}))
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=10)
            if not done:
                print(f'  [运行中] 已等待 {perf_counter() - started:.1f}s，等待智能体返回……', flush=True)
        return await task
    finally:
        if not task.done():
            task.cancel()


def save_report(path: Path, results: list[dict], total: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {'total': total, 'completed': len(results),
              'execution_succeeded': sum(item.get('execution_succeeded', False) for item in results),
              'passed': sum(item.get('passed', False) for item in results), 'results': results}
    # 中途失败也保留此前结果，报告只保存业务结果，不保存请求头。
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


async def main(args) -> int:
    settings = get_settings()
    headers = {'X-Internal-Service-Token': settings.internal_service_token}
    async with httpx.AsyncClient(timeout=settings.multi_agent_timeout_seconds + 30, trust_env=False) as client:
        response = await client.get(f'{args.ai_url}/internal/v1/tests/cases', headers=headers)
        response.raise_for_status()
        cases = response.json()
        if args.cases:
            selected = set(args.cases.split(','))
            if selected - {case['case_id'] for case in cases}:
                raise ValueError('存在未知用例编号，请先运行 --list')
            cases = [case for case in cases if case['case_id'] in selected]
        if args.list:
            for case in cases:
                print(f"{case['case_id']} [{case['category']}] {case['question']}")
            return 0
        response = await client.post(f'{settings.ecommerce_base_url.rstrip("/")}/auth/demo-token',
                                     json={'user_id': args.user_id, 'role': 'customer'})
        response.raise_for_status()
        headers['Authorization'] = 'Bearer ' + response.json()['access_token']
        results = []
        for index, case in enumerate(cases, 1):
            print(f"[{index}/{len(cases)}] {case['case_id']} {case['question']}", flush=True)
            client_started = perf_counter()
            try:
                response = await post_with_progress(client, f'{args.ai_url}/internal/v1/tests/run',
                                                     headers, case['case_id'])
                response.raise_for_status()
                result = response.json()
            except httpx.HTTPError as error:
                # 不保存异常的原始请求或响应，避免意外记录认证信息。
                result = {'case_id': case['case_id'], 'question': case['question'],
                          'execution_succeeded': False, 'passed': False, 'error': type(error).__name__}
            result['client_latency_ms'] = round((perf_counter() - client_started) * 1000)
            results.append(result)
            save_report(args.output, results, len(cases))
            if result.get('error'):
                print(f"  FAIL | 客户端等待 {result['client_latency_ms']/1000:.3f}s"
                      f" | {result['error']} | 服务端未返回统计：模型、工具、Token 均未知", flush=True)
                continue
            model, tokens = result.get('model_metrics', {}), result.get('token_usage', {})
            model_count = model.get('call_count', '未知')
            model_latency = (f"{model['total_latency_ms']/1000:.3f}s"
                             if 'total_latency_ms' in model else '未知')
            print(f"  {'PASS' if result['passed'] else 'FAIL'} | 总耗时 {result.get('latency_ms', 0)/1000:.3f}s"
                  f" | 模型 {model_count}次/{model_latency}"
                  f" | 业务工具 {result.get('tool_call_count', 0)}次（成功{result.get('tool_success_count', 0)}次）"
                  f"，已记录耗时 {result.get('tool_total_latency_ms', 0)/1000:.3f}s"
                  f" | Token 输入 {tokens.get('input_tokens', 0)} / 输出 {tokens.get('output_tokens', 0)}", flush=True)
            if 'max_parallel_calls' in model:
                print(f"    模型并发峰值：{model['max_parallel_calls']}（同一时刻进行中的模型调用数）", flush=True)
            for call in model.get('calls', []):
                usage = call.get('token_usage')
                token_text = (f"Token 输入 {usage['input_tokens']} / 输出 {usage['output_tokens']}"
                              f" / 合计 {usage['total_tokens']}" if usage is not None else 'Token 未知')
                interval = (f" | 开始+{call['started_after_ms']/1000:.3f}s"
                            f" → 结束+{call['finished_after_ms']/1000:.3f}s"
                            if 'started_after_ms' in call else '')
                print(f"    模型#{call['call_index']} [{call.get('agent_name', '未知智能体')}/"
                      f"{call.get('agent_code', 'unknown')}] {call['model']}"
                      f" {call['status']} {call['latency_ms']/1000:.3f}s{interval} | {token_text}", flush=True)
            for index, call in enumerate(result.get('tool_calls', []), 1):
                state = '成功' if call['success'] is True else '未完成' if call['success'] is None else '失败'
                latency = f"{call['latency_ms']/1000:.3f}s" if call.get('latency_ms') is not None else '耗时未记录'
                print(f"    工具#{index} [{call.get('agent_name', '未知智能体')}/"
                      f"{call.get('agent_code', 'unknown')}] {call['tool_name']}"
                      f" {state} {latency} code={call['code']}", flush=True)
            for check in result.get('checks', []):
                if not check['passed']:
                    print(f"    未通过：{check['name']}", flush=True)
            if result.get('reply_content'):
                print('  ' + result['reply_content'].replace('\n', ' ')[:250], flush=True)
        passed = sum(item['passed'] for item in results)
        print(f'检查通过 {passed}/{len(cases)}；报告：{args.output}', flush=True)
        return 0 if passed == len(cases) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ai-url', default='http://127.0.0.1:8002')
    parser.add_argument('--user-id', default='user_001')
    parser.add_argument('--cases', help='只运行指定编号，例如 01,05,20')
    parser.add_argument('--list', action='store_true', help='只列出问题，不调用大模型')
    parser.add_argument('--output', type=Path,
                        default=PROJECT / 'reports' / f'customer-scenarios-{datetime.now():%Y%m%d-%H%M%S}.json')
    arguments = parser.parse_args()
    arguments.ai_url = arguments.ai_url.rstrip('/')
    try:
        raise SystemExit(asyncio.run(main(arguments)))
    except (httpx.HTTPError, ValueError) as error:
        print(f'测试未能启动：{type(error).__name__}；请检查服务、鉴权和用例编号。', file=sys.stderr)
        raise SystemExit(2)
