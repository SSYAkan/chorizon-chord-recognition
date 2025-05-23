#!/bin/bash

# 安装依赖
pip install -r requirements.txt

# 启动 Redis（如果还没有启动）
redis-server --daemonize yes

# 启动应用
gunicorn -c gunicorn.conf.py chord:app 