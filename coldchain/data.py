import math
import random
import pandas as pd
from .model import normalize


def coordinate_network(problem):
    """核验内置模板的欧氏距离/40km/h模型，不用来源文字授予坐标插单能力。

    旧四表模板不含仓库坐标；只在全部矩阵与(25,25)仓库吻合时恢复它。
    真实路网或缺少坐标时返回None，保留显式双向边输入要求。
    """
    depot = problem.get('depot', dict(x=25., y=25.))
    try:
        points = [depot] + problem['orders']
        for point in points:
            for key in ('x', 'y'):
                value = point[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    return None
        for i, a in enumerate(points):
            for j, b in enumerate(points):
                d = problem['distance_matrix'][i][j]
                t = problem['time_matrix'][i][j]
                if not math.isclose(d, math.hypot(a['x']-b['x'], a['y']-b['y']), rel_tol=0, abs_tol=0.000051):
                    return None
                if not math.isclose(t, d*1.5, rel_tol=0, abs_tol=1e-7):
                    return None
        return dict(depot=dict(x=depot['x'], y=depot['y']), minutes_per_km=1.5)
    except (KeyError, TypeError, IndexError):
        return None


def demo_problem(n=25, seed=42):
    """明确标注的合成算例；不冒充Solomon或企业真实订单。"""
    rng = random.Random(seed)
    coords = [(25., 25.)] + [(rng.uniform(2,48), rng.uniform(2,48)) for _ in range(n)]
    distance = [[round(math.dist(a,b),4) for b in coords] for a in coords]
    return normalize(dict(orders=[dict(id=f'C{i:02d}', demand=rng.randint(4,12), ready=0,
                                      due=420, service=5, x=coords[i][0], y=coords[i][1]) for i in range(1,n+1)],
                          vehicles=[dict(id=f'V{i:02d}', capacity=80) for i in range(1,max(4, math.ceil(n/8))+1)],
                          distance_matrix=distance, time_matrix=[[d*1.5 for d in row] for row in distance],
                          depot=dict(x=25, y=25), source='合成演示算例；40 km/h恒速、欧氏距离，非真实道路；成本/排放系数为可配置示例'))


def read_excel(stream):
    """显式表结构，不猜测编码损坏的旧表头，也不将分钟隐式换成km。"""
    book = pd.ExcelFile(stream)
    required = {'orders', 'vehicles', 'distance_km', 'time_min'}
    if not required <= set(book.sheet_names):
        raise ValueError('Excel需包含orders、vehicles、distance_km、time_min四个sheet；请下载模板。旧参考算例缺少公里矩阵，不能直接导入。')
    problem = normalize(dict(orders=pd.read_excel(book, 'orders').to_dict('records'),
                          vehicles=pd.read_excel(book, 'vehicles').to_dict('records'),
                          distance_matrix=pd.read_excel(book, 'distance_km', header=None).values.tolist(),
                          time_matrix=pd.read_excel(book, 'time_min', header=None).values.tolist(),
                          source='用户Excel；距离km、时间min、需求kg；系数采用示例默认值，正式实验前须校准'))
    network = coordinate_network(problem)
    if network:
        problem['depot'] = network['depot']
        problem['source'] = '导入Excel；已核验坐标与欧氏距离、40 km/h时间矩阵一致；坐标插单沿用该模型，非真实道路导航；成本/排放系数为示例'
    return problem
