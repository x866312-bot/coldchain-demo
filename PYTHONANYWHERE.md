# PythonAnywhere 免费部署说明

该方案不需要绑定银行卡。免费账号提供 1 个 Web App 和 1 个 Worker，Web App 每月需要在控制台续期。

## 1. 上传源码

在 PythonAnywhere 的 **Files** 页面上传 `coldchain-pythonanywhere.zip`。打开 **Consoles > Bash**，运行：

```bash
unzip -o ~/coldchain-pythonanywhere.zip -d ~/coldchain-demo
mkvirtualenv --python=/usr/bin/python3.13 coldchain
pip install -r ~/coldchain-demo/requirements.txt
```

## 2. 创建 Web App

进入 **Web > Add a new web app**：

1. 选择 **Manual configuration**。
2. Python 版本选择 **3.13**。
3. Virtualenv 填入 `/home/你的用户名/.virtualenvs/coldchain`。
4. 打开页面顶部的 WSGI configuration file，用 `~/coldchain-demo/pythonanywhere_wsgi.py` 的内容替换模板。
5. 点击 **Reload**。

公开地址为 `https://你的用户名.pythonanywhere.com/`。

## 3. 验证与维护

- 打开 `/healthz` 应返回 `{"service":"coldchain","status":"ok"}`。
- 免费账号只有一个 Worker，优化请求会串行执行；适合评审演示，不适合并发生产负载。
- Web App 每月到期前需在 Web 页面点击延长运行时间。
- 当前会话保存在单个 Worker 的内存中；Web App 重载后需要重新载入算例。
- 审计文件写入 `~/coldchain-data`，需定期下载或清理，避免占满免费磁盘。
