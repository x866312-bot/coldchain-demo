# 冷链智运免费评审部署

本目录从当前系统按白名单复制，未包含运行日志、历史会话、密钥或旧系统。原本地系统保持不变。

Render Web Service 配置：

- Runtime: Python
- Plan: **Free**
- Region: Singapore
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn -c gunicorn.conf.py cloud_app:app`
- Health check: `/healthz`
- `PYTHON_VERSION=3.13.13`
- `COLDCHAIN_HTTPS=1`
- `COLDCHAIN_OUTPUT_DIR=/tmp/coldchain-outputs`
- 不配置付费数据库、磁盘或模型 API。当前自然语言入口使用界面明示的规则模式。

评审操作：下载页面提供的 25、50、100 客户 Excel，上传后运行优化；在动态事件页面推进配送并插入订单；点击“恢复初始状态”撤销配送与事件。不同浏览器会话彼此隔离。

免费实例闲置 15 分钟后休眠，下一次访问通常等待约一分钟。休眠、重启、重新部署都会丢失当前会话及临时审计文件；需保留结果时先点击“导出完整审计记录”。恢复初始状态只在当前存活会话中有效。并行优化请求会排队，当前系统限制 100 个会话，适合小规模评审演示。

官方限制：https://render.com/docs/free

状态：这是部署源码，不代表服务已经上线。只有创建服务并通过公网验证后才提供评审链接。
