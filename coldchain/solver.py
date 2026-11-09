"""多车经典ALNS：随机/关联破坏、最小增量插入、自适应权重、退火接受。

沿用原项目的随机/空间关联破坏思路。原Q3是单车量子修复，不能直接
声称支持多车硬约束；此处提供独立经典实现，不宣称量子求解或原论文结果。
"""
import copy
import math
import random
import time
import uuid
from .model import context, evaluate_route, validate_solution, calculate_kpis, number


def optimize_routes(p, objective='cost', solver_config=None, states=None, completed=(), initial=None):
    cfg = dict(iterations=120, seed=42, time_limit_seconds=12)
    if solver_config:
        if set(solver_config) - set(cfg):
            raise ValueError('未知求解参数')
        cfg.update(solver_config)
    if objective not in ('cost', 'carbon', 'distance'):
        raise ValueError('目标必须为cost、carbon或distance')
    for k in ('iterations', 'seed'):
        if type(cfg[k]) is not int or cfg[k] < 0:
            raise ValueError(f'{k}必须是非负整数')
    if cfg['iterations'] > 2000 or not 0 < number(cfg['time_limit_seconds'], 'time_limit_seconds') <= 60:
        raise ValueError('迭代上限2000，时间预算须为(0,60]秒')
    started = time.perf_counter()
    deadline = started + cfg['time_limit_seconds']
    rng = random.Random(cfg['seed'])
    states = states or {}
    orders, idx = context(p)
    pending = set(orders) - set(completed)
    vehicles = {v['id']: v for v in p['vehicles']}
    field = {'cost': 'total_cost_cny', 'carbon': 'emissions_kgco2', 'distance': 'distance_km'}[objective]

    def route_score(vid, seq):
        r = evaluate_route(p, vehicles[vid], seq, states.get(vid))
        return r[field] if r['valid'] else math.inf

    def score(routes):
        served = sum(map(len, routes.values()))
        return len(pending)-served, sum(route_score(v, s) for v, s in routes.items())

    def repair(routes, removed):
        # Stable order and local RNG make non-time-limited runs reproducible.
        for node in removed:
            best = (math.inf, None, None)
            for vid, seq in routes.items():
                if not vehicles[vid]['available']:
                    continue
                base = route_score(vid, seq)
                for pos in range(len(seq)+1):
                    if time.perf_counter() >= deadline:
                        return routes
                    delta = route_score(vid, seq[:pos]+[node]+seq[pos:]) - base
                    if delta < best[0]:
                        best = delta, vid, pos
            if best[1] is not None:
                routes[best[1]].insert(best[2], node)
        return routes

    current = {vid: [] for vid in vehicles}
    if initial:
        for r in initial:
            current[r['vehicle_id']] = list(r['customer_sequence'])
    present = {x for seq in current.values() for x in seq}
    current = repair(current, sorted(pending-present, key=lambda x: (orders[x]['due'], x)))
    best, best_score = copy.deepcopy(current), score(current)
    baseline = copy.deepcopy(current)
    weights, curve, iterations = [1., 1.], [], 0
    for iteration in range(cfg['iterations']):
        if time.perf_counter() >= deadline:
            break
        nodes = [x for seq in current.values() for x in seq]
        if not nodes:
            break
        op = rng.choices([0, 1], weights=weights)[0]
        k = max(1, min(len(nodes), math.ceil(len(nodes)*.25)))
        if op == 0:
            removed = rng.sample(nodes, k)
        else:
            center = rng.choice(nodes)
            removed = sorted(nodes, key=lambda x: p['time_matrix'][idx[center]][idx[x]])[:k]
        trial = {v: [x for x in seq if x not in removed] for v, seq in current.items()}
        missing = sorted(pending - {x for seq in trial.values() for x in seq})
        rng.shuffle(missing)
        trial = repair(trial, missing)
        value, old = score(trial), score(current)
        reward = 1.
        if value < best_score:
            best, best_score, reward = copy.deepcopy(trial), value, 8.
        temperature = max(.01, (old[1] or 1)*.03*(1-iteration/max(1,cfg['iterations'])))
        if value < old or (value[0] == old[0] and rng.random() < math.exp(min(0, (old[1]-value[1])/temperature))):
            current = trial
            reward = max(reward, 2.)
        weights[op] = .85*weights[op]+.15*reward
        iterations += 1
        if iteration % 10 == 0:
            curve.append(dict(iteration=iteration, unserved=best_score[0], objective=best_score[1]))

    def assemble(routes):
        raw = [dict(vehicle_id=v, customer_sequence=s) for v, s in routes.items()]
        details, kpis = calculate_kpis(p, raw, states)
        violations = validate_solution(p, raw, states, completed)
        missing = sorted(pending - {x for seq in routes.values() for x in seq})
        return dict(routes=details, kpis=kpis, constraint_violations=violations,
                    status='feasible' if not violations else 'no_feasible_solution_found',
                    unserved_orders=missing, completed_orders=list(completed),
                    service_rate=(len(pending)-len(missing))/len(pending) if pending else 1.)
    result = assemble(best)
    result.update(solution_id=uuid.uuid4().hex, objective=objective, solver_config=cfg,
                  algorithm='classic_multi_vehicle_ALNS', runtime_seconds=time.perf_counter()-started,
                  iterations_completed=iterations, time_limit_reached=time.perf_counter() >= deadline,
                  convergence=curve, operator_weights=weights,
                  baseline=assemble(baseline), cost_scope='从当前执行状态开始的剩余成本（不含已发生费用）' if states else '完整计划成本')
    return result
