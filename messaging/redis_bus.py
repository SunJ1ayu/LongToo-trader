"""Redis Message Bus - Redis 消息总线 (阶段3)

替代 LocalMessageBus，实现真正的分布式 Agent 通信。
每个 Agent 可以运行在不同进程/机器上，通过 Redis 交换消息。
"""

import json
import redis
import threading
import time
from typing import Dict, List, Callable, Any, Optional
from datetime import datetime

from .local_bus import Channel, Message

# 命名空间常量
NAMESPACE = "instreet:bus"


class RedisMessageBus:
    """Redis 消息总线
    
    使用 Redis Pub/Sub 实现跨进程消息通信。
    接口与 LocalMessageBus 保持一致，便于替换。
    """
    
    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None,
        max_history: int = 1000
    ):
        """
        Args:
            host: Redis 主机地址
            port: Redis 端口
            db: Redis 数据库
            password: Redis 密码
            max_history: 保留的最大消息历史数（本地缓存）
        """
        self._redis_params = {
            "host": host,
            "port": port,
            "db": db,
            "password": password,
            "decode_responses": True
        }
        
        # 连接池
        self._pool = redis.ConnectionPool(**self._redis_params)
        self._redis = redis.Redis(connection_pool=self._pool)
        
        # 订阅管理
        self._subscribers: Dict[Channel, List[Callable[[Message], None]]] = {
            channel: [] for channel in Channel
        }
        self._pubsub = None
        self._listener_thread: Optional[threading.Thread] = None
        
        # 本地缓存
        self._history: List[Message] = []
        self._max_history = max_history
        self._lock = threading.Lock()
        
        self._running = False
        self._node_id = f"node_{threading.current_thread().ident}"
    
    def _get_redis(self) -> redis.Redis:
        """获取 Redis 连接"""
        return redis.Redis(connection_pool=self._pool)
    
    def start(self):
        """启动消息总线"""
        if self._running:
            return
        
        # 检查 Redis 连接
        try:
            self._redis.ping()
        except redis.ConnectionError as e:
            raise RuntimeError(f"无法连接 Redis: {e}")
        
        self._running = True
        
        # 启动订阅监听线程
        self._pubsub = self._redis.pubsub()
        
        # 预先订阅所有频道（解决 subscribe 时序问题）
        for ch in Channel:
            self._pubsub.subscribe(ch.value)
            print(f"📡 预订阅 {ch.value}")
        
        self._listener_thread = threading.Thread(
            target=self._listen,
            daemon=True
        )
        self._listener_thread.start()
        
        print(f"📡 RedisMessageBus 已启动 ({self._node_id})")
        print(f"   连接: {self._redis_params['host']}:{self._redis_params['port']}")
    
    def stop(self):
        """停止消息总线"""
        self._running = False
        
        if self._pubsub:
            self._pubsub.close()
        
        if self._listener_thread:
            self._listener_thread.join(timeout=2.0)
        
        print(f"📡 RedisMessageBus 已停止 ({self._node_id})")
    
    def subscribe(self, channel: Channel, handler: Callable[[Message], None]):
        """订阅频道"""
        with self._lock:
            self._subscribers[channel].append(handler)
        
        # 注意：Redis 频道已在 start() 中预订阅
        print(f"📡 本地订阅 {channel.value}: {handler.__qualname__}")
    
    def unsubscribe(self, channel: Channel, handler: Callable[[Message], None]):
        """取消订阅"""
        with self._lock:
            if handler in self._subscribers[channel]:
                self._subscribers[channel].remove(handler)
        
        # 如果没有订阅者了，取消 Redis 订阅
        if self._pubsub and not self._subscribers[channel]:
            self._pubsub.unsubscribe(channel.value)
    
    def publish(self, message: Message) -> str:
        """发布消息到 Redis"""
        # 序列化消息
        data = json.dumps(message.to_dict())
        
        # 发布到 Redis
        self._redis.publish(message.channel.value, data)
        
        # 本地缓存
        with self._lock:
            self._history.append(message)
            if len(self._history) > self._max_history:
                self._history.pop(0)
        
        return message.msg_id
    
    def publish_sync(self, message: Message) -> int:
        """同步发布（同 publish，保持接口一致）"""
        self.publish(message)
        
        # Redis 不直接返回接收者数量，返回本地订阅者数
        with self._lock:
            return len(self._subscribers[message.channel])
    
    def _listen(self):
        """监听 Redis 消息"""
        print(f"🔌 _listen 开始运行")
        
        if not self._pubsub:
            print(f"❌ _pubsub 为空")
            return
        
        print(f"🔌 进入 listen 循环...")
        
        for item in self._pubsub.listen():
            print(f"📨 收到原始消息: {item}")
            
            if not self._running:
                print(f"🔌 _running=False，退出")
                break
            
            if item["type"] != "message":
                continue
            
            try:
                # 解析消息
                data = json.loads(item["data"])
                message = Message.from_dict(data)
                print(f"📨 解析成功: {message.channel.value}")
                
                # 分发到本地订阅者
                self._dispatch(message)
                
            except Exception as e:
                print(f"⚠️ 消息解析错误: {e}")
    
    def _dispatch(self, message: Message):
        """分发消息到本地订阅者"""
        with self._lock:
            handlers = self._subscribers[message.channel].copy()
        
        for handler in handlers:
            try:
                # 异步处理
                threading.Thread(
                    target=self._safe_handle,
                    args=(handler, message),
                    daemon=True
                ).start()
            except Exception as e:
                print(f"⚠️ 启动消息处理器失败: {e}")
    
    def _safe_handle(self, handler: Callable[[Message], None], message: Message):
        """安全地调用处理器，带重试机制"""
        max_retries = 3
        retry_delay = 1  # 秒
        
        for attempt in range(max_retries):
            try:
                handler(message)
                return  # 成功，直接返回
            except Exception as e:
                print(f"⚠️ 消息处理器错误 [{handler.__qualname__}] 尝试{attempt+1}/{max_retries}: {e}")
                
                if attempt < max_retries - 1:
                    # 指数退避
                    time.sleep(retry_delay * (2 ** attempt))
                else:
                    # 最终失败，记录到死信队列
                    self._log_dead_letter(handler, message, e)
    
    def _log_dead_letter(self, handler: Callable, message: Message, error: Exception):
        """记录死信（处理失败的消息）"""
        dead_letter = {
            "msg_id": message.msg_id,
            "channel": message.channel.value,
            "sender": message.sender,
            "handler": handler.__qualname__,
            "error": str(error),
            "timestamp": datetime.now().isoformat(),
            "payload": message.payload
        }
        # 存储到 Redis 死信队列
        namespace = getattr(self, '_namespace', NAMESPACE)
        self._redis.lpush(f"{namespace}:dead_letters", json.dumps(dead_letter))
        self._redis.ltrim(f"{namespace}:dead_letters", 0, 99)  # 保留最近100条
        print(f"💀 消息已记录到死信队列: {message.msg_id}")
    
    def get_history(self, channel: Optional[Channel] = None, limit: int = 100) -> List[Message]:
        """获取消息历史"""
        with self._lock:
            if channel:
                msgs = [m for m in self._history if m.channel == channel]
            else:
                msgs = self._history.copy()
            return msgs[-limit:]
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        with self._lock:
            stats = {
                "total_messages": len(self._history),
                "subscriber_counts": {
                    ch.value: len(subs) for ch, subs in self._subscribers.items()
                },
                "running": self._running,
                "node_id": self._node_id,
                "redis_connected": self._redis.ping() if self._running else False
            }
            return stats
    
    def clear_history(self):
        """清除历史记录"""
        with self._lock:
            self._history.clear()


# 工厂函数：根据配置创建消息总线
def create_message_bus(
    backend: str = "local",
    **kwargs
):
    """创建消息总线
    
    Args:
        backend: "local" 或 "redis"
        **kwargs: 传递给具体实现的参数
        
    Returns:
        LocalMessageBus 或 RedisMessageBus 实例
    """
    if backend == "redis":
        return RedisMessageBus(**kwargs)
    else:
        from .local_bus import LocalMessageBus
        return LocalMessageBus(**kwargs)
