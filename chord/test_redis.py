import redis

try:
    # 尝试连接 Redis
    redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)
    redis_client.ping()
    print("Redis 连接成功！")
except Exception as e:
    print(f"Redis 连接失败: {str(e)}") 