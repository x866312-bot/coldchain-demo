"""单主控Agent + 白名单工具；离线模式明确标注规则解析。"""
import json
import os
import re
import urllib.request
import urllib.error

SCHEMA = dict(type='object', properties={
    'task': dict(type='string', enum=['optimize_routes', 'replan_routes', 'calculate_kpis', 'clarify']),
    'objective': dict(type='string', enum=['cost', 'carbon', 'distance']),
    'reason': dict(type='string')}, required=['task', 'objective', 'reason'], additionalProperties=False)


def parse_request(message):
    if not isinstance(message, str) or not message.strip() or len(message) > 4000:
        raise ValueError('请输入1至4000字的调度指令')
    key = os.environ.get('COLDCHAIN_API_KEY')
    if key:
        body = dict(model=os.environ.get('COLDCHAIN_MODEL', 'qwen-plus'), temperature=0,
                    messages=[dict(role='system', content='你是冷链调度规划器。只能选择工具，不能编造路径或数值。任务:优化/重规划/指标/澄清。目标cost、carbon、distance。涉及新增订单、故障或改时间窗选replan_routes，参数由用户结构化事件提供；未知约束或多目标权重未指定选clarify。硬容量和时间窗始终启用。'), dict(role='user', content=message)],
                    tools=[dict(type='function', function=dict(name='dispatch', description='选择确定性调度工具', parameters=SCHEMA))],
                    tool_choice=dict(type='function', function=dict(name='dispatch')))
        endpoint = os.environ.get('COLDCHAIN_API_BASE', 'https://dashscope.aliyuncs.com/compatible-mode/v1').rstrip('/')+'/chat/completions'
        req = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers={'Authorization': 'Bearer '+key, 'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.load(response)
            calls = data['choices'][0]['message']['tool_calls']
            if len(calls) != 1 or calls[0]['function']['name'] != 'dispatch':
                raise ValueError('不支持的工具调用')
            parsed = json.loads(calls[0]['function']['arguments'])
            if set(parsed) != {'task', 'objective', 'reason'} or parsed['task'] not in SCHEMA['properties']['task']['enum'] or parsed['objective'] not in ('cost','carbon','distance') or not isinstance(parsed['reason'],str):
                raise ValueError('模型输出不符合工具schema')
            return dict(parsed, mode='llm_function_calling', token_usage=data.get('usage', {}), api_cost_cny=None)
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError('大模型请求失败或输出不合法；未执行任何工具。请检查配置或切换离线模式。') from exc
    # Conservative offline grammar: unknown constraints never silently disappear.
    objective = 'carbon' if re.search('低碳|碳排|减碳', message) else 'distance' if re.search('距离|路程|最短', message) else 'cost'
    if re.search('新增|增加.*订单|故障|不可用|重规划|重调度|修改.*时间窗', message):
        task = 'replan_routes'
    elif re.search('报告|指标|成本构成|分析', message):
        task = 'calculate_kpis'
    elif re.search('优化|配送|调度|规划|路线', message):
        task = 'optimize_routes'
    else:
        task = 'clarify'
    if re.search(r'\d|不超过|最多|至少|软时间窗|忽略|取消|提高|降低容量|温度|优先级', message) or ('低碳' in message and '低成本' in message):
        task = 'clarify'
    return dict(task=task, objective=objective, reason='离线规则解析；数值约束请通过标准数据或事件表单明确提供', mode='offline_rules', token_usage={}, api_cost_cny=None)


def explain(result):
    k = result['kpis']
    if result['status'] != 'feasible':
        return f"未找到完整可行方案；未服务{len(result['unserved_orders'])}单。请检查运力、时间窗和事件时刻。这不是数学上的无解证明，当前部分方案不可作为完整调度计划执行。"
    return (f"独立约束校验通过。待执行订单服务率{result['service_rate']:.0%}，"
            f"距离{k['distance_km']:.2f} km，成本{k['total_cost_cny']:.2f}元，"
            f"按输入系数估算排放{k['emissions_kgco2']:.2f} kg CO₂。"
            f"口径：{result['cost_scope']}。所有数值来自工具计算，排放是模型估算。")
