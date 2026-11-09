"""标准输入与独立验算。单位：km、min、kg、L、kWh、CNY。"""
import copy
import math
from collections import Counter


def number(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f'{label}必须是大于等于{minimum}的有限数值')
    return float(value)


def normalize(raw):
    p = copy.deepcopy(raw)
    for key in ('orders', 'vehicles', 'distance_matrix', 'time_matrix'):
        if key not in p:
            raise ValueError(f'缺少字段 {key}')
    if not isinstance(p['orders'], list) or not 1 <= len(p['orders']) <= 150:
        raise ValueError('orders必须包含1至150个客户（为100客户动态插单预留余量）')
    if not isinstance(p['vehicles'], list) or not 1 <= len(p['vehicles']) <= 30:
        raise ValueError('vehicles必须包含1至30辆车')
    for records, fields in ((p['orders'], ('demand', 'ready', 'due', 'service')), (p['vehicles'], ('capacity',))):
        ids = []
        for r in records:
            if not isinstance(r, dict) or not isinstance(r.get('id'), str) or not r['id'] or r['id'] == 'DEPOT':
                raise ValueError('订单和车辆必须使用非空字符串id（DEPOT保留）')
            ids.append(r['id'])
            for f in fields:
                r[f] = number(r.get(f), f)
        if len(ids) != len(set(ids)):
            raise ValueError('订单或车辆id重复')
    for o in p['orders']:
        if o['ready'] > o['due']:
            raise ValueError(f"{o['id']}时间窗上下界颠倒")
    for v in p['vehicles']:
        if v['capacity'] <= 0:
            raise ValueError('车辆容量必须大于0')
        v.setdefault('available', True)
        if not isinstance(v['available'], bool):
            raise ValueError('available必须是布尔值')
    n = len(p['orders']) + 1
    for key in ('distance_matrix', 'time_matrix'):
        matrix = p[key]
        if not isinstance(matrix, list) or len(matrix) != n or any(not isinstance(row, list) or len(row) != n for row in matrix):
            raise ValueError(f'{key}须为{n}×{n}矩阵；顺序为DEPOT、orders列表')
        for i, row in enumerate(matrix):
            for j, value in enumerate(row):
                number(value, key)
                if (i == j and value != 0) or (i != j and value <= 0):
                    raise ValueError(f'{key}对角线须为0，其他元素须大于0')
    defaults = dict(fixed_cost=300, fuel_empty_l_km=.18, fuel_load_l_km=.09,
                    fuel_price=7.5, cooling_kw=2.5, electricity_price=.8,
                    diesel_kgco2_l=2.68, electricity_kgco2_kwh=.55)
    supplied = p.get('cost_parameters', {})
    if not isinstance(supplied, dict) or set(supplied) - set(defaults):
        raise ValueError('未知成本参数')
    defaults.update(supplied)
    p['cost_parameters'] = {k: number(v, k) for k, v in defaults.items()}
    p.setdefault('source', '用户上传；成本及排放系数须由业务方校准')
    return p


def context(p):
    return {o['id']: o for o in p['orders']}, {'DEPOT': 0, **{o['id']: i+1 for i, o in enumerate(p['orders'])}}


def evaluate_route(p, vehicle, sequence, state=None):
    """候选路径成本模型。动态状态已经包含回仓补货的前置成本。"""
    orders, idx = context(p)
    state = state or {}
    t = state.get('ready_time', 0)
    start = t
    load = sum(orders[x]['demand'] for x in sequence)
    initial_load = load
    c = p['cost_parameters']
    distance = fuel = 0.
    arrivals = []
    valid = load <= vehicle['capacity'] + 1e-8 and (vehicle.get('available', True) or not sequence)
    prev = 'DEPOT'
    for node in list(sequence) + (['DEPOT'] if sequence else []):
        d = p['distance_matrix'][idx[prev]][idx[node]]
        distance += d
        fuel += d * (c['fuel_empty_l_km'] + c['fuel_load_l_km'] * load / vehicle['capacity'])
        t += p['time_matrix'][idx[prev]][idx[node]]
        if node != 'DEPOT':
            o = orders[node]
            t = max(t, o['ready'])
            arrivals.append(t)
            valid = valid and t <= o['due'] + 1e-8
            t += o['service']
            load -= o['demand']
        prev = node
    cooling = (t - start) / 60 * c['cooling_kw']
    used = bool(sequence) or state.get('used', False)
    fixed = c['fixed_cost'] if sequence and not state.get('used', False) else 0
    fuel += state.get('return_fuel_l', 0)
    distance += state.get('return_distance_km', 0)
    cooling += state.get('return_cooling_kwh', 0)
    return dict(vehicle_id=vehicle['id'], customer_sequence=list(sequence), start_time=start,
                service_starts=arrivals, finish_time=t, load_kg=initial_load,
                capacity_kg=vehicle['capacity'], distance_km=distance, fuel_l=fuel,
                cooling_kwh=cooling, fixed_cost_cny=fixed,
                fuel_cost_cny=fuel*c['fuel_price'], cooling_cost_cny=cooling*c['electricity_price'],
                total_cost_cny=fixed+fuel*c['fuel_price']+cooling*c['electricity_price'],
                emissions_kgco2=fuel*c['diesel_kgco2_l']+cooling*c['electricity_kgco2_kwh'], valid=bool(valid), used=used)


def validate_solution(p, routes, states=None, completed=()):
    """从输入和访问序列独立重算约束，不信任求解器的载重、到达时间或valid。"""
    states = states or {}
    orders, idx = context(p)
    vehicles = {v['id']: v for v in p['vehicles']}
    violations, visited, seen_vehicles = [], [], set()
    for r in routes:
        vid = r['vehicle_id']
        if vid not in vehicles or vid in seen_vehicles:
            violations.append(f'车辆未知或重复：{vid}')
            continue
        seen_vehicles.add(vid)
        v, seq = vehicles[vid], r['customer_sequence']
        if any(x not in orders for x in seq):
            violations.append(f'{vid}含未知客户')
            continue
        if seq and not v['available']:
            violations.append(f'{vid}已不可用')
        if sum(orders[x]['demand'] for x in seq) > v['capacity'] + 1e-8:
            violations.append(f'{vid}超载')
        t, prev = states.get(vid, {}).get('ready_time', 0), 'DEPOT'
        for node in seq:
            t = max(t + p['time_matrix'][idx[prev]][idx[node]], orders[node]['ready'])
            if t > orders[node]['due'] + 1e-8:
                violations.append(f'{node}违反硬时间窗')
            t += orders[node]['service']
            prev = node
        visited.extend(seq)
    counts = Counter(visited + list(completed))
    for node in orders:
        if counts[node] != 1:
            violations.append(f'{node}服务次数为{counts[node]}，要求恰好1次')
    return violations


def calculate_kpis(p, routes, states=None):
    states = states or {}
    vehicles = {v['id']: v for v in p['vehicles']}
    details = [evaluate_route(p, vehicles[r['vehicle_id']], r['customer_sequence'], states.get(r['vehicle_id'])) for r in routes]
    fields = ('distance_km', 'fuel_l', 'cooling_kwh', 'fixed_cost_cny', 'fuel_cost_cny', 'cooling_cost_cny', 'total_cost_cny', 'emissions_kgco2')
    return details, {key: sum(r[key] for r in details) for key in fields}
