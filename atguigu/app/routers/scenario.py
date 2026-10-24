"""开发联调接口：逐题调用现有智能体，沿用内部调用和用户鉴权。"""
from typing import Annotated

from fastapi import APIRouter, Depends, Header

from atguigu.app.dependencies import AgentRunCoordinatorDep, get_auth_service, require_internal_service
from atguigu.app.schemas.scenario import ScenarioCase, ScenarioTestRequest
from atguigu.app.services.scenario import SCENARIO_CASES, ScenarioTestService

router = APIRouter(prefix='/internal/v1/tests', tags=['客服场景测试'],
                   dependencies=[Depends(require_internal_service)])


@router.get('/cases', response_model=list[ScenarioCase])
async def list_cases():
    """列出20个测试问题及工具检查标准，不调用大模型。"""
    return list(SCENARIO_CASES)


@router.post('/run')
async def run_case(request: ScenarioTestRequest, coordinator: AgentRunCoordinatorDep,
                   authorization: Annotated[str | None, Header()] = None):
    """执行一个测试问题，会调用配置中的真实大模型并写入 AI 审计记录。"""
    auth = get_auth_service()
    current_user = auth.get_authorized_user(authorization, 'customer')
    return await ScenarioTestService(coordinator).run(
        request.case_id, current_user.user_id, auth.get_bearer_token(authorization))
