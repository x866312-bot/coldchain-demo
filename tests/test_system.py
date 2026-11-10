import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coldchain.data import demo_problem, read_excel, coordinate_network
from coldchain.model import normalize, evaluate_route, validate_solution
from coldchain.solver import optimize_routes
from coldchain.replanning import advance_execution, replan_routes
from coldchain.planner import parse_request, explain
from server import app, stores

CFG = dict(iterations=40, seed=42, time_limit_seconds=10)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.p = demo_problem()

    def test_seed_reproducibility(self):
        a = optimize_routes(self.p, solver_config=CFG)
        b = optimize_routes(self.p, solver_config=CFG)
        self.assertEqual(a['routes'], b['routes'])
        self.assertEqual(a['status'], 'feasible')
        self.assertLessEqual(a['kpis']['total_cost_cny'], a['baseline']['kpis']['total_cost_cny'])

    def test_objectives(self):
        for objective, field in [('cost','total_cost_cny'),('carbon','emissions_kgco2'),('distance','distance_km')]:
            s = optimize_routes(self.p, objective, CFG)
            self.assertEqual(s['status'], 'feasible')
            self.assertLessEqual(s['kpis'][field], s['baseline']['kpis'][field]+1e-8)

    def test_capacity_infeasible(self):
        self.p['orders'][0]['demand'] = 1000
        s = optimize_routes(self.p, solver_config=CFG)
        self.assertEqual(s['status'], 'no_feasible_solution_found')
        self.assertIn('C01', s['unserved_orders'])
        self.assertTrue(all(r['load_kg'] <= r['capacity_kg'] for r in s['routes']))

    def test_time_window_infeasible(self):
        self.p['orders'][0]['due'] = 0
        s = optimize_routes(self.p, solver_config=CFG)
        self.assertIn('C01', s['unserved_orders'])

    def test_unavailable_vehicles(self):
        for v in self.p['vehicles']:
            v['available'] = False
        s = optimize_routes(self.p, solver_config=CFG)
        self.assertEqual(len(s['unserved_orders']), 25)

    def test_independent_validator_does_not_trust_solution(self):
        r = dict(vehicle_id='V01', customer_sequence=['C01','C01'], load_kg=0, service_starts=[0,0], valid=True)
        errors = validate_solution(self.p, [r])
        self.assertTrue(any('C01服务次数为2' in x for x in errors))
        self.p['orders'][0]['due'] = 0
        self.assertTrue(any('硬时间窗' in x for x in validate_solution(self.p, [r])))

    def test_unknown_and_duplicate_vehicle(self):
        r = dict(vehicle_id='missing', customer_sequence=['C01'])
        self.assertTrue(validate_solution(self.p, [r]))
        r['vehicle_id'] = 'V01'
        self.assertTrue(any('重复' in x for x in validate_solution(self.p, [r,r])))

    def test_cooling_wait_service_and_load(self):
        p = normalize(dict(orders=[dict(id='C', demand=10,ready=30,due=50,service=10)], vehicles=[dict(id='V',capacity=10)],distance_matrix=[[0,10],[10,0]],time_matrix=[[0,10],[10,0]]))
        r = evaluate_route(p,p['vehicles'][0],['C'])
        self.assertAlmostEqual(r['fuel_l'], 10*.27+10*.18)
        self.assertAlmostEqual(r['cooling_kwh'],50/60*2.5)
        self.assertEqual(r['service_starts'],[30])

    def test_input_rejection(self):
        for mutation in [lambda p:p['orders'].append(p['orders'][0]),lambda p:p['time_matrix'][0].pop(),lambda p:p['orders'][0].update(demand=-1),lambda p:p['orders'][0].update(due=float('nan')),lambda p:p['distance_matrix'][0].__setitem__(1,0),lambda p:p['vehicles'][0].update(capacity=0)]:
            p = copy.deepcopy(self.p)
            mutation(p)
            with self.assertRaises(ValueError):
                normalize(p)

    def test_invalid_solver_config(self):
        for cfg in [dict(iterations=1.5),dict(iterations=3000),dict(time_limit_seconds=0),dict(seed=-1)]:
            with self.assertRaises(ValueError):
                optimize_routes(self.p, solver_config=cfg)

    def test_dynamic_preserves_history_and_return_costs(self):
        s = optimize_routes(self.p, solver_config=CFG)
        ex = advance_execution(self.p,s)
        frozen = copy.deepcopy(ex)
        event = dict(type='new_order',use_demo_coordinates=True,order=dict(id='N',demand=8,ready=0,due=420,service=5,x=27,y=32))
        p, r = replan_routes(self.p,ex,event,solver_config=CFG)
        self.assertEqual(r['status'],'feasible')
        self.assertEqual(ex, frozen)
        self.assertEqual(r['execution_snapshot']['prefixes'],frozen['prefixes'])
        self.assertTrue(set(ex['completed']).isdisjoint(x for route in r['routes'] for x in route['customer_sequence']))
        self.assertTrue(any(x['return_distance_km']>0 for x in r['vehicle_start_states'].values()))
        self.assertTrue(all(x['ready_time'] >= ex['time'] for x in r['vehicle_start_states'].values()))
        self.assertEqual(len({x['ready_time'] for x in r['vehicle_start_states'].values()}),1)
        self.assertEqual(len(p['orders']),26)
        self.assertEqual(len(self.p['orders']),25)
        self.assertTrue(r['comparison']['comparable'])
        ex2 = advance_execution(p,r,ex)
        self.assertTrue(set(ex['completed']) < set(ex2['completed']))

    def test_double_advance_rejected(self):
        s = optimize_routes(self.p,solver_config=CFG)
        ex = advance_execution(self.p,s)
        with self.assertRaises(ValueError):
            advance_execution(self.p,s,ex)

    def test_in_transit_failure_not_teleport(self):
        s = optimize_routes(self.p,solver_config=CFG)
        ex = advance_execution(self.p,s)
        vid = next(v for v,x in ex['vehicles'].items() if x['location']!='DEPOT')
        with self.assertRaises(ValueError):
            replan_routes(self.p,ex,dict(type='vehicle_unavailable',vehicle_id=vid))

    def test_completed_window_immutable(self):
        s = optimize_routes(self.p,solver_config=CFG)
        ex = advance_execution(self.p,s)
        with self.assertRaises(ValueError):
            replan_routes(self.p,ex,dict(type='time_window',order_id=ex['completed'][0],ready=0,due=1))

    def test_asymmetric_event_edges(self):
        ex=dict(completed=[],vehicles={},prefixes={},time=0)
        e=dict(type='new_order',order=dict(id='N',demand=1,ready=0,due=500,service=1),distance_to=[2]*26,distance_from=[3]*26,time_to=[4]*26,time_from=[5]*26)
        p,r=replan_routes(self.p,ex,e,solver_config=CFG)
        self.assertEqual(p['distance_matrix'][0][-1],2)
        self.assertEqual(p['distance_matrix'][-1][0],3)

    def test_infeasible_new_order_reports_capacity_reason(self):
        solution = optimize_routes(self.p, solver_config=CFG)
        execution = advance_execution(self.p, solution)
        event = dict(type='new_order', use_demo_coordinates=True,
                     order=dict(id='N-CAP', demand=1000, ready=0, due=420,
                                service=5, x=27, y=32))
        _, result = replan_routes(self.p, execution, event, solver_config=CFG)
        self.assertEqual(result['status'], 'no_feasible_solution_found')
        diagnostic = next(x for x in result['feasibility_diagnostics'] if x['order_id'] == 'N-CAP')
        self.assertEqual(diagnostic['code'], 'capacity')
        self.assertIn('运力', diagnostic['label'])
        self.assertIn('N-CAP', explain(result))

    def test_infeasible_new_order_reports_event_time_reason(self):
        solution = optimize_routes(self.p, solver_config=CFG)
        execution = advance_execution(self.p, solution)
        event = dict(type='new_order', use_demo_coordinates=True,
                     order=dict(id='N-LATE', demand=1, ready=0,
                                due=max(0, execution['time']-1), service=1, x=27, y=32))
        _, result = replan_routes(self.p, execution, event, solver_config=CFG)
        diagnostic = next(x for x in result['feasibility_diagnostics'] if x['order_id'] == 'N-LATE')
        self.assertEqual(diagnostic['code'], 'event_time')


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'COLDCHAIN_OUTPUT_DIR':self.tmp.name,'COLDCHAIN_API_KEY':''})
        self.env.start()
        stores.clear()
        self.client=app.test_client()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_api_closed_loop_and_isolation(self):
        with self.client.get('/') as response:
            self.assertEqual(response.status_code,200)
        r=self.client.post('/api/agent',json=dict(message='请优化低碳配送',solver_config=CFG)).get_json()
        self.assertEqual(r['solution']['status'],'feasible')
        self.assertEqual(r['trace'][0]['mode'],'offline_rules')
        self.assertEqual(self.client.post('/api/advance',json={}).status_code,200)
        event=dict(type='new_order',use_demo_coordinates=True,order=dict(id='N',demand=2,ready=0,due=420,service=2,x=28,y=30))
        r=self.client.post('/api/replan',json=dict(event=event,solver_config=CFG)).get_json()
        self.assertEqual(r['solution']['status'],'feasible')
        exported=json.loads(self.client.get('/api/export').data)
        self.assertEqual(len(exported['history']),2)
        self.assertIn('problem',exported['history'][0])
        self.assertIsNone(app.test_client().get('/api/state').get_json()['solution'])

    def test_bad_event_rolls_back(self):
        self.client.post('/api/optimize',json={'solver_config':CFG})
        before=self.client.get('/api/state').get_json()
        self.assertEqual(self.client.post('/api/replan',json={'event':{'type':'bad'}}).status_code,400)
        self.assertEqual(self.client.get('/api/state').get_json(),before)

    def test_excel_roundtrip_and_invalid_upload(self):
        data=self.client.get('/api/template').data
        self.assertEqual(len(read_excel(io.BytesIO(data))['orders']),25)
        r=self.client.post('/api/data',data={'file':(io.BytesIO(data),'orders.xlsx')})
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.client.post('/api/data',data={'file':(io.BytesIO(b'bad'),'orders.json')}).status_code,400)

    def test_excel_template_dynamic_roundtrip(self):
        data=self.client.get('/api/template').data
        self.client.post('/api/data',data={'file':(io.BytesIO(data),'template.xlsx')})
        state=self.client.get('/api/state').get_json()
        self.assertEqual(state['problem']['depot'],dict(x=25,y=25))
        self.assertEqual(self.client.post('/api/optimize',json={'solver_config':CFG}).get_json()['solution']['status'],'feasible')
        self.client.post('/api/advance',json={})
        for oid,x in [('N01',27),('N02',28)]:
            event=dict(type='new_order',use_demo_coordinates=True,order=dict(id=oid,demand=2,ready=0,due=420,service=2,x=x,y=32))
            response=self.client.post('/api/replan',json=dict(event=event,solver_config=CFG))
            self.assertEqual(response.status_code,200,response.get_json())
            self.assertEqual(response.get_json()['solution']['status'],'feasible')

    def test_coordinate_capability_checks_matrices_not_source(self):
        p=demo_problem()
        p['source']='用户Excel'
        p.pop('depot')
        self.assertIsNotNone(coordinate_network(p))
        p['source']='合成演示'
        p['distance_matrix'][0][1]+=1
        self.assertIsNone(coordinate_network(p))
        p=demo_problem()
        p['time_matrix'][1][2]+=1
        self.assertIsNone(coordinate_network(p))
        p=demo_problem()
        p['orders'][0].pop('x')
        self.assertIsNone(coordinate_network(p))

    def test_missing_event_clarifies(self):
        self.client.post('/api/optimize',json={'solver_config':CFG})
        r=self.client.post('/api/agent',json={'message':'新增订单重新规划'}).get_json()
        self.assertIn('clarification',r)

    def test_prompt_examples_select_distinct_tools_and_targets(self):
        cost = self.client.post('/api/agent', json=dict(
            message='在满足车辆载重和硬时间窗的前提下，生成总运营成本最低的配送方案，并汇报校验结果。',
            solver_config=CFG)).get_json()
        self.assertEqual(cost['trace'][0]['task'], 'optimize_routes')
        self.assertEqual(cost['trace'][0]['objective'], 'cost')
        carbon = self.client.post('/api/agent', json=dict(
            message='以模型估算碳排放最低为目标重新规划配送路线，同时检查容量、时间窗和客户唯一覆盖约束。',
            solver_config=CFG)).get_json()
        self.assertEqual(carbon['trace'][0]['task'], 'optimize_routes')
        self.assertEqual(carbon['trace'][0]['objective'], 'carbon')
        diagnosis = self.client.post('/api/agent', json=dict(
            message='分析当前配送方案，解释成本、碳排、服务率及约束校验结果，并指出需要关注的风险。')).get_json()
        self.assertEqual(diagnosis['trace'][0]['task'], 'calculate_kpis')
        self.assertEqual(diagnosis['solution']['solution_id'], carbon['solution']['solution_id'])

    def test_diagnosis_before_optimization_gives_actionable_clarification(self):
        response = self.client.post('/api/agent', json=dict(
            message='分析当前配送方案，解释成本、碳排、服务率及约束校验结果。'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('请先执行', response.get_json()['clarification'])

    def test_replan_api_returns_structured_failure_diagnostics(self):
        self.client.post('/api/optimize', json={'solver_config': CFG})
        self.client.post('/api/advance', json={})
        event = dict(type='new_order', use_demo_coordinates=True,
                     order=dict(id='N-CAP', demand=1000, ready=0, due=420,
                                service=5, x=27, y=32))
        response = self.client.post('/api/replan', json=dict(event=event, solver_config=CFG))
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['solution']['status'], 'no_feasible_solution_found')
        self.assertEqual(payload['solution']['feasibility_diagnostics'][0]['code'], 'capacity')
        self.assertIn('原因定位', payload['explanation'])

    def test_bad_json_shape(self):
        self.assertEqual(self.client.post('/api/optimize',json=[]).status_code,400)

    def test_restore_uploaded_dataset_and_initial_plan(self):
        original=demo_problem(50)
        self.client.post('/api/data',json={'problem':original})
        plan=self.client.post('/api/optimize',json={'solver_config':CFG}).get_json()['solution']
        self.client.post('/api/advance',json={})
        event=dict(type='new_order',use_demo_coordinates=True,order=dict(id='N01',demand=2,ready=0,due=420,service=2,x=27,y=32))
        self.client.post('/api/replan',json={'event':event,'solver_config':CFG})
        previous=self.client.get('/api/state').get_json()
        self.assertEqual(len(previous['problem']['orders']),51)
        for _ in range(2):
            response=self.client.post('/api/reset',json={})
            self.assertEqual(response.status_code,200)
            restored=self.client.get('/api/state').get_json()
            self.assertEqual(restored['problem'],original)
            self.assertEqual(restored['solution'],plan)
            self.assertEqual(restored['execution']['completed'],[])
            self.assertEqual(restored['execution']['time'],0)
            self.assertEqual(len(restored['history']),1)
        archives=list((Path(self.tmp.name)/'reset_archives').glob('*.json'))
        self.assertTrue(any(len(json.loads(p.read_text(encoding='utf-8'))['problem']['orders'])==51 for p in archives))
        self.assertEqual(self.client.post('/api/advance',json={}).status_code,200)
        self.assertEqual(self.client.post('/api/replan',json={'event':event,'solver_config':CFG}).status_code,200)

    def test_reset_before_solve_and_reupload_changes_baseline(self):
        for n in (50,100):
            self.client.post('/api/data',json={'problem':demo_problem(n)})
            self.client.post('/api/reset',json={})
            state=self.client.get('/api/state').get_json()
            self.assertEqual(len(state['problem']['orders']),n)
            self.assertIsNone(state['solution'])
        self.client.post('/api/data',json={'demo':True})
        self.client.post('/api/reset',json={})
        self.assertEqual(len(self.client.get('/api/state').get_json()['problem']['orders']),25)

    def test_reset_restores_vehicle_window_and_isolates_sessions(self):
        self.client.post('/api/optimize',json={'solver_config':CFG})
        self.client.post('/api/replan',json={'event':dict(type='vehicle_unavailable',vehicle_id='V04'),'solver_config':CFG})
        self.client.post('/api/replan',json={'event':dict(type='time_window',order_id='C01',ready=0,due=0),'solver_config':CFG})
        other=app.test_client()
        other.post('/api/data',json={'problem':demo_problem(100)})
        self.client.post('/api/reset',json={})
        p=self.client.get('/api/state').get_json()['problem']
        self.assertTrue(p['vehicles'][-1]['available'])
        self.assertEqual(p['orders'][0]['due'],420)
        self.assertEqual(len(other.get('/api/state').get_json()['problem']['orders']),100)

    def test_llm_function_schema_with_mock_transport(self):
        payload=dict(choices=[dict(message=dict(tool_calls=[dict(function=dict(name='dispatch',arguments=json.dumps(dict(task='optimize_routes',objective='carbon',reason='低碳'))))]))],usage={'total_tokens':100})
        with patch.dict(os.environ,{'COLDCHAIN_API_KEY':'test-not-real'}),patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(payload).encode())):
            self.assertEqual(parse_request('低碳优化')['mode'],'llm_function_calling')
        payload['choices'][0]['message']['tool_calls'][0]['function']['name']='delete_all'
        with patch.dict(os.environ,{'COLDCHAIN_API_KEY':'test-not-real'}),patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(payload).encode())):
            with self.assertRaises(ValueError):
                parse_request('低碳优化')


if __name__=='__main__':
    unittest.main(verbosity=2)
