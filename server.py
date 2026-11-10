"""冷链智运：单进程演示服务，会话隔离、原子提交和可下载审计记录。"""
import copy
import io
import json
import os
import secrets
import threading
import time
import uuid
import zipfile
from pathlib import Path
import pandas as pd
from flask import Flask, jsonify, request, session, send_file
from werkzeug.exceptions import HTTPException
from coldchain.data import demo_problem, read_excel
from coldchain.model import normalize
from coldchain.solver import optimize_routes
from coldchain.replanning import advance_execution, replan_routes
from coldchain.planner import parse_request, explain

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(ROOT/'web'), static_url_path='/web')
app.secret_key = os.environ.get('COLDCHAIN_SESSION_SECRET') or secrets.token_hex(32)
app.config.update(MAX_CONTENT_LENGTH=5*1024*1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict')
stores = {}
lock = threading.RLock()


def empty_execution():
    return dict(completed=[], vehicles={}, prefixes={}, time=0)


def fresh_state(problem):
    state = dict(problem=copy.deepcopy(problem), solution=None, execution=empty_execution(), history=[], trace=[])
    state['initial_state'] = copy.deepcopy(state)
    return state


def store():
    if 'id' not in session:
        session['id'] = uuid.uuid4().hex
    sid = session['id']
    if sid not in stores:
        if len(stores) >= 100:
            raise ValueError('演示会话已达100个，请重启服务释放会话')
        stores[sid] = fresh_state(demo_problem())
    return stores[sid]


def persist(state):
    directory = Path(os.environ.get('COLDCHAIN_OUTPUT_DIR', str(ROOT/'outputs')))
    directory.mkdir(exist_ok=True, parents=True)
    target = directory / (session['id']+'.json')
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(target)


def commit(state, result, trace):
    state['solution'] = result
    state['trace'] = trace
    state['history'].append(dict(problem=copy.deepcopy(state['problem']), execution=copy.deepcopy(state['execution']), solution=copy.deepcopy(result), timestamp=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), trace=trace))
    # 保留最近一次基于原始数据的可行方案，不让事件或不可行尝试覆盖恢复点。
    if (result['status'] == 'feasible' and not result.get('event')
            and not state['execution']['completed']
            and state['problem'] == state['initial_state']['problem']):
        state['initial_state'] = copy.deepcopy({k: state[k] for k in ('problem', 'solution', 'execution', 'history', 'trace')})
    persist(state)
    stores[session['id']] = state


@app.errorhandler(Exception)
def error(exc):
    if isinstance(exc, (ValueError, KeyError, TypeError, zipfile.BadZipFile)):
        return jsonify(success=False, error=str(exc)), 400
    if isinstance(exc, HTTPException):
        return jsonify(success=False, error=exc.description), exc.code
    app.logger.exception('Request failed')
    return jsonify(success=False, error='服务器处理失败；请查看本地日志。当前状态未提交。'), 500


@app.before_request
def same_origin():
    if request.method == 'POST' and request.headers.get('Sec-Fetch-Site') == 'cross-site':
        return jsonify(success=False, error='不接受跨站写入'), 403
    if request.method == 'POST' and request.is_json and not isinstance(request.get_json(), dict):
        return jsonify(success=False, error='请求JSON必须是对象'), 400


@app.get('/')
def index():
    return app.send_static_file('index.html')


@app.get('/api/state')
def get_state():
    with lock:
        return jsonify(success=True, **store(), mode='llm_function_calling' if os.environ.get('COLDCHAIN_API_KEY') else 'offline_rules')


@app.post('/api/data')
def upload():
    with lock:
        state = copy.deepcopy(store())
        if request.files:
            file = request.files.get('file')
            if file is None:
                raise ValueError('请选择file字段上传')
            if file.filename.lower().endswith('.xlsx'):
                problem = read_excel(file)
            elif file.filename.lower().endswith('.json'):
                problem = normalize(json.load(file))
            else:
                raise ValueError('支持.xlsx或.json；CSV请先按模板组装矩阵')
        else:
            body = request.get_json()
            problem = demo_problem() if body.get('demo') else normalize(body['problem'])
        state = fresh_state(problem)
        persist(state)
        stores[session['id']] = state
        return jsonify(success=True, problem=problem)


@app.post('/api/reset')
def reset():
    with lock:
        previous = copy.deepcopy(store())
        initial = copy.deepcopy(previous['initial_state'])
        restored = copy.deepcopy(initial)
        restored['initial_state'] = initial
        directory = Path(os.environ.get('COLDCHAIN_OUTPUT_DIR', str(ROOT/'outputs'))) / 'reset_archives'
        directory.mkdir(exist_ok=True, parents=True)
        archive = directory / (session['id'] + '_' + uuid.uuid4().hex + '.json')
        temporary = archive.with_suffix('.tmp')
        temporary.write_text(json.dumps(previous, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        temporary.replace(archive)
        persist(restored)
        stores[session['id']] = restored
        return jsonify(success=True, has_solution=restored['solution'] is not None,
                       customers=len(restored['problem']['orders']), archive_id=archive.name)


@app.get('/api/template')
def template():
    p = demo_problem()
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine='openpyxl') as writer:
        pd.DataFrame(p['orders']).to_excel(writer, sheet_name='orders', index=False)
        pd.DataFrame(p['vehicles']).to_excel(writer, sheet_name='vehicles', index=False)
        pd.DataFrame(p['distance_matrix']).to_excel(writer, sheet_name='distance_km', index=False, header=False)
        pd.DataFrame(p['time_matrix']).to_excel(writer, sheet_name='time_min', index=False, header=False)
    out.seek(0)
    return send_file(out, as_attachment=True, download_name='coldchain_template.xlsx')


@app.post('/api/advance')
def advance():
    with lock:
        state = copy.deepcopy(store())
        if not state['solution']:
            raise ValueError('请先生成可行方案')
        state['execution'] = advance_execution(state['problem'], state['solution'], state['execution'])
        persist(state)
        stores[session['id']] = state
        return jsonify(success=True, execution=state['execution'])


@app.post('/api/agent')
@app.post('/api/optimize')
@app.post('/api/replan')
def dispatch():
    started = time.perf_counter()
    with lock:
        state = copy.deepcopy(store())
        body = request.get_json() or {}
        if request.path == '/api/agent':
            plan = parse_request(body.get('message'))
        else:
            plan = dict(task='replan_routes' if request.path.endswith('replan') else 'optimize_routes', objective=body.get('objective','cost'), mode='structured_form')
        trace = [dict(step='parse', **plan)]
        task = plan['task']
        if task == 'clarify':
            return jsonify(success=True, clarification='请明确单一目标（成本/碳排/距离）。容量和时间窗请通过数据文件修改；动态事件请填写事件表单，系统不会忽略未识别约束。', trace=trace)
        if task == 'calculate_kpis':
            if not state['solution']:
                return jsonify(success=True, clarification='当前没有可诊断的配送方案。请先执行“成本优先”或“低碳优先”生成方案，再使用方案诊断。', trace=trace)
            return jsonify(success=True, solution=state['solution'],
                           explanation='已读取当前方案并完成指标诊断；本次未改变路线。'+explain(state['solution']),
                           trace=trace)
        if task == 'replan_routes':
            if not state['solution']:
                raise ValueError('请先生成初始方案')
            if not body.get('event'):
                return jsonify(success=True, clarification='请在动态事件中心填写新增订单、发车前车辆不可用或时间窗变更；缺少参数时不会自动修改订单。', trace=trace)
            state['problem'], result = replan_routes(state['problem'], state['execution'], body['event'], plan['objective'], body.get('solver_config'))
        else:
            if state['execution']['completed']:
                raise ValueError('配送已推进，请使用动态重规划；重新导入数据才能重置执行历史')
            result = optimize_routes(state['problem'], plan['objective'], body.get('solver_config'))
        trace.extend([dict(step='tool', name=task, solution_id=result['solution_id']),
                      dict(step='validate', status=result['status'], violations=result['constraint_violations']),
                      dict(step='explain', message=explain(result))])
        result['end_to_end_seconds'] = time.perf_counter()-started
        commit(state, result, trace)
        return jsonify(success=True, solution=result, explanation=explain(result), trace=trace)


@app.get('/api/export')
def export():
    with lock:
        content = json.dumps(store(), ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    return send_file(io.BytesIO(content), as_attachment=True, download_name='coldchain_audit.json', mimetype='application/json')
