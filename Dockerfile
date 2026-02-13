# 使用Python官方轻量级镜像
FROM python:3.11-slim

# 环境变量：关闭 .pyc 生成，开启无缓冲输出，便于日志实时输出
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 安装必要的系统依赖
# - tini: 作为init进程，正确处理信号和僵尸进程
# - ca-certificates/tzdata：保证 HTTPS 及时区配置
RUN apt-get update && \
    apt-get install -y --no-install-recommends tini ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

# 工作目录
WORKDIR /app

# 先复制依赖文件再安装，充分利用 Docker 构建缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目源码到容器
COPY . .

# 使用 tini 作为 init 进程，确保信号正确传递
ENTRYPOINT ["/usr/bin/tini", "--"]

# 直接运行 Python 程序（不通过 shell 脚本）
CMD ["python", "main.py"]
