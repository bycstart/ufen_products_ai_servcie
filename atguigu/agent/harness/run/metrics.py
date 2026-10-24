"""记录逐次模型调用的耗时和 Token，不记录提示词、请求头或异常原文。"""
from copy import deepcopy
from time import perf_counter

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration


class ModelTimingCallback(AsyncCallbackHandler):
    def __init__(self):
        self.records = {}

    async def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        metadata = metadata or {}
        self.records[run_id] = {'call_index': len(self.records) + 1,
                                'model': metadata.get('ls_model_name', ''),
                                'agent_code': metadata.get('agent_code', 'unknown'),
                                'agent_role': metadata.get('agent_role', 'unknown'),
                                'agent_name': metadata.get('agent_name', '未知智能体'),
                                'task_id': metadata.get('task_id'),
                                'started': perf_counter(), 'status': 'INCOMPLETE', 'token_usage': None}

    async def on_llm_end(self, response, *, run_id, **kwargs):
        self._finish(run_id, 'SUCCESS')
        record = self.records.get(run_id)
        if record is not None and response is not None and response.generations and response.generations[0]:
            generation = response.generations[0][0]
            if isinstance(generation, ChatGeneration) and isinstance(generation.message, AIMessage):
                # 与框架累计统计采用同一来源；缓存和推理明细保留，但不重复加总。
                record['token_usage'] = deepcopy(generation.message.usage_metadata)

    async def on_llm_error(self, error, *, run_id, **kwargs):
        self._finish(run_id, 'FAILED')

    def _finish(self, run_id, status):
        record = self.records.get(run_id)
        if record is not None:
            record['latency_ms'] = round((perf_counter() - record['started']) * 1000)
            record['status'] = status

    def summary(self) -> dict:
        calls = []
        for record in self.records.values():
            latency = record.get('latency_ms')
            if latency is None:
                latency = round((perf_counter() - record['started']) * 1000)
            calls.append({key: value for key, value in {**record, 'latency_ms': latency}.items()
                          if key != 'started'})
        # 以首个模型开始为零点，累计耗时与墙钟区间分别记录。
        origin = next(iter(self.records.values()))['started'] if self.records else 0
        events = []
        for record, call in zip(self.records.values(), calls):
            call['started_after_ms'] = round((record['started'] - origin) * 1000)
            call['finished_after_ms'] = call['started_after_ms'] + call['latency_ms']
            if call['latency_ms'] > 0:
                events.extend([(call['started_after_ms'], 1), (call['finished_after_ms'], -1)])
        active, peak = 0, 0
        for _, change in sorted(events):
            active += change
            peak = max(peak, active)
        return {'call_count': len(calls), 'total_latency_ms': sum(call['latency_ms'] for call in calls),
                'max_parallel_calls': peak, 'calls': calls}
