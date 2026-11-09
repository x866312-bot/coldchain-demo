import copy
import math
from .model import context, normalize, number
from .solver import optimize_routes
from .data import coordinate_network


def advance_execution(p, solution, execution=None):
    """可复现的离散执行仿真：每辆车完成下一客户后停车，统一时刻触发事件。

    不模拟GPS在途位置。新状态来自已验算方案，不能由LLM伪造。
    """
    if solution['status'] != 'feasible':
        raise ValueError('只有通过约束校验的方案才能推进执行')
    old = execution or dict(completed=[], vehicles={}, prefixes={}, time=0)
    if old.get('advanced_solution_id') == solution['solution_id']:
        raise ValueError('该方案已推进；请先重规划再推进')
    result = copy.deepcopy(old)
    orders, _ = context(p)
    for r in solution['routes']:
        vid, seq = r['vehicle_id'], r['customer_sequence']
        if not seq:
            result['vehicles'][vid] = dict(location='DEPOT', time=r['finish_time'], remaining_cargo_kg=0, used=r['used'])
            continue
        node = seq[0]
        if node in result['completed']:
            raise ValueError('执行状态与方案不一致，请先重规划')
        finish = r['service_starts'][0] + orders[node]['service']
        result['completed'].append(node)
        result['prefixes'].setdefault(vid, []).append(dict(order_id=node, service_start=r['service_starts'][0], finish=finish))
        result['vehicles'][vid] = dict(location=node, time=finish,
                                       remaining_cargo_kg=sum(orders[x]['demand'] for x in seq[1:]), used=True)
    result['time'] = max([old['time']] + [s['time'] for s in result['vehicles'].values()])
    result['advanced_solution_id'] = solution['solution_id']
    return result


def replan_routes(problem, execution, event, objective='cost', solver_config=None):
    p, ex = copy.deepcopy(problem), copy.deepcopy(execution)
    if not isinstance(event, dict) or event.get('type') not in ('new_order', 'vehicle_unavailable', 'time_window'):
        raise ValueError('事件类型须为new_order、vehicle_unavailable或time_window')
    kind = event['type']
    orders, idx = context(p)
    if kind == 'new_order':
        order = event.get('order')
        if not isinstance(order, dict) or order.get('id') in orders:
            raise ValueError('新订单缺失或id重复')
        n = len(p['orders'])+1
        # Synthetic coordinate demo only. Real data must supply both directional edges.
        if event.get('use_demo_coordinates'):
            network = coordinate_network(p)
            if network is None:
                raise ValueError('当前数据的坐标或矩阵不符合模板欧氏距离/40 km/h模型，无法推算新增路段。请在自定义事件JSON中提供双向距离和时间。')
            p['depot'] = network['depot']
            x, y = number(order.get('x'), 'x'), number(order.get('y'), 'y')
            pts = [p['depot']] + p['orders']
            distances = [round(math.hypot(x-o['x'], y-o['y']), 4) for o in pts]
            edges = dict(distance_to=distances, distance_from=distances,
                         time_to=[d*1.5 for d in distances], time_from=[d*1.5 for d in distances])
        else:
            edges = event
        for matrix, prefix in (('distance_matrix', 'distance'), ('time_matrix', 'time')):
            to, fr = edges.get(prefix+'_to'), edges.get(prefix+'_from')
            if not isinstance(to, list) or not isinstance(fr, list) or len(to) != n or len(fr) != n:
                raise ValueError(f'新增订单需{n}个{prefix}_to（原节点到新点）和{prefix}_from（新点到原节点）')
            for row, val in zip(p[matrix], to):
                row.append(val)
            p[matrix].append(fr+[0])
        p['orders'].append(order)
    elif kind == 'vehicle_unavailable':
        vid = event.get('vehicle_id')
        matches = [v for v in p['vehicles'] if v['id'] == vid]
        if not matches:
            raise ValueError('车辆不存在')
        if ex['vehicles'].get(vid, {}).get('location', 'DEPOT') != 'DEPOT':
            raise ValueError('该车辆在途：缺少救援/货物转运数据，不能假设货物瞬移。此版本仅支持发车前车辆不可用。')
        matches[0]['available'] = False
    else:
        oid = event.get('order_id')
        if oid not in orders or oid in ex['completed']:
            raise ValueError('客户不存在或已完成，不能改写历史时间窗')
        orders[oid]['ready'], orders[oid]['due'] = event.get('ready'), event.get('due')
    p = normalize(p)
    _, idx = context(p)
    states = {}
    c = p['cost_parameters']
    for v in p['vehicles']:
        state = ex['vehicles'].get(v['id'], {})
        location = state.get('location', 'DEPOT')
        d = p['distance_matrix'][idx[location]][0]
        minutes = p['time_matrix'][idx[location]][0]
        states[v['id']] = dict(ready_time=ex['time']+minutes, used=state.get('used', False),
                                return_distance_km=d, return_cooling_kwh=minutes/60*c['cooling_kw'],
                                return_fuel_l=d*(c['fuel_empty_l_km']+c['fuel_load_l_km']*state.get('remaining_cargo_kg',0)/v['capacity']))
    # 全部携货车辆回仓后才能重新分配货物，防止先回仓车辆使用尚在途的货物。
    reload_time = max(s['ready_time'] for s in states.values())
    for s in states.values():
        if s['used']:
            s['return_cooling_kwh'] += (reload_time-s['ready_time'])/60*c['cooling_kw']
        s['ready_time'] = reload_time
    result = optimize_routes(p, objective, solver_config, states, ex['completed'])
    result['execution_snapshot'] = ex
    result['vehicle_start_states'] = states
    result['event'] = event
    result['dynamic_policy'] = '服务边界停车；所有在途车辆从当前位置回仓，统一到齐后卸货重配；含回仓等待制冷，装卸时间暂设0，须业务校准'
    result['comparison'] = dict(baseline='同一事件、同一执行状态下最小增量插入',
                                comparable=result['status']=='feasible' and result['baseline']['status']=='feasible',
                                delta_cost_cny=result['kpis']['total_cost_cny']-result['baseline']['kpis']['total_cost_cny'] if result['status']=='feasible' and result['baseline']['status']=='feasible' else None)
    return p, result
