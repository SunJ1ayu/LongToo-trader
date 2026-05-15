"""Local Message Bus - 本地消息总线 (阶段2)

模拟 C1/C2/C3 消息频道，在同进程内实现 Agent 间异步通信。
为阶段3的 Redis/RabbitMQ 替换做准备。
"""

from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Dict, List, Callable, Any, Optional
from datetime import datetime
import threading
import queue
import uuid
from concurrent.futures import ThreadPoolExecutor


class Channel(Enum):
    """消息频道定义 (C1/C2/C3 规范)"""
    
    # C1: 原始数据输入 / 分析指令
    C1_RAW_DATA = "c1_raw_data"
    C1_ANALYSIS_CMD = "c1_analysis_cmd"
    
    # C2: 分析结果 / 风控指令
    C2_ANALYSIS_RESULT = "c2_analysis_result"
    C2_RISK_CMD = "c2_risk_cmd"
    
    # C3: 风控结果 / 执行指令
    C3_RISK_RESULT = "c3_risk_result"
    C3_EXECUTE_CMD = "c3_execute_cmd"
    
    # 系统频道
    SYS_HEARTBEAT = "sys_heartbeat"
    SYS_LOG = "sys_log"

    # 交易事件频道 (v2.5.0)
    TRADE_ORDER_FILLED = "trade_order_filled"      # 订单成交（买入/卖出成功）
    TRADE_STOP_TRIGGERED = "trade_stop_triggered"  # 止损触发
    TRADE_SIGNAL_GENERATED = "trade_signal_generated"  # 信号生成


@dataclass
class Message:
    """消息格式 (C1/C2/C3 规范)"""
    
    # 消息标识
    msg_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    channel: Channel = Channel.C1_RAW_DATA
    
    # 发送信息
    sender: str = "unknown"
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    # 消息内容
    msg_type: str = "data"  # data, cmd, result, error
    payload: Dict[str, Any] = field(default_factory=dict)
    
    # 追踪信息
    correlation_id: Optional[str] = None  # 关联消息ID（用于请求-响应）
    
    # 元数据
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        """转换为字典（用于序列化）"""
        return {
            "msg_id": self.msg_id,
            "channel": self.channel.value,
            "sender": self.sender,
            "timestamp": self.timestamp,
            "msg_type": self.msg_type,
            "payload": self.payload,
            "correlation_id": self.correlation_id,
            "metadata": self.metadata
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> "Message":
        """从字典创建消息"""
        return cls(
            msg_id=data.get("msg_id", str(uuid.uuid4())[:8]),
            channel=Channel(data.get("channel", "c1_raw_data")),
            sender=data.get("sender", "unknown"),
            timestamp=data.get("timestamp", datetime.now().isoformat()),
            msg_type=data.get("msg_type", "data"),
            payload=data.get("payload", {}),
            correlation_id=data.get("correlation_id"),
            metadata=data.get("metadata", {})
        )


class LocalMessageBus:
    """本地消息总线
    
    在同进程内模拟消息队列，支持：
    - 订阅/发布模式
    - 异步消息处理
    - 消息历史记录（调试）
    
    阶段3可替换为 Redis/RabbitMQ 实现，接口保持不变。
    """
    
    def __init__(self, max_history: int = 1000, max_workers: int = 10):
        """
        Args:
            max_history: 保留的最大消息历史数
            max_workers: 最大线程池工作线程数
        """
        self._subscribers: Dict[Channel, List[Callable[[Message], None]]] = {
            channel: [] for channel in Channel
        }
        self._history: List[Message] = []
        self._max_history = max_history
        self._lock = threading.Lock()
        self._running = False
        self._message_queue: queue.Queue = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._max_workers = max_workers
    
    def start(self):
        """启动消息总线（启动后台处理线程和线程池）"""
        if self._running:
            return
        
        self._running = True
        self._executor = ThreadPoolExecutor(max_workers=self._max_workers)
        self._worker_thread = threading.Thread(target=self._process_messages, daemon=True)
        self._worker_thread.start()
        print(f"📡 LocalMessageBus 已启动 (线程池: {self._max_workers} workers)")
    
    def stop(self):
        """停止消息总线"""
        self._running = False
        if self._worker_thread:
            self._worker_thread.join(timeout=1.0)
        if self._executor:
            self._executor.shutdown(wait=False)
        print("📡 LocalMessageBus 已停止")
    
    def subscribe(self, channel: Channel, handler: Callable[[Message], None]):
        """订阅频道
        
        Args:
            channel: 要订阅的频道
            handler: 消息处理函数，接收 Message 参数
        """
        with self._lock:
            self._subscribers[channel].append(handler)
        print(f"📡 订阅 {channel.value}: {handler.__qualname__}")
    
    def unsubscribe(self, channel: Channel, handler: Callable[[Message], None]):
        """取消订阅"""
        with self._lock:
            if handler in self._subscribers[channel]:
                self._subscribers[channel].remove(handler)
    
    def publish(self, message: Message) -> str:
        """发布消息
        
        Args:
            message: 要发布的消息
            
        Returns:
            str: 消息ID
        """
        # 加入队列异步处理
        self._message_queue.put(message)
        
        # 记录历史
        with self._lock:
            self._history.append(message)
            if len(self._history) > self._max_history:
                self._history.pop(0)
        
        return message.msg_id
    
    def publish_sync(self, message: Message) -> int:
        """同步发布消息（直接调用订阅者，用于调试）
        
        Returns:
            int: 通知的订阅者数量
        """
        with self._lock:
            handlers = self._subscribers[message.channel].copy()
        
        count = 0
        for handler in handlers:
            try:
                handler(message)
                count += 1
            except Exception as e:
                print(f"⚠️ 消息处理错误 [{message.channel.value}]: {e}")
        
        # 记录历史
        with self._lock:
            self._history.append(message)
            if len(self._history) > self._max_history:
                self._history.pop(0)
        
        return count
    
    def _process_messages(self):
        """后台线程：处理消息队列"""
        while self._running:
            try:
                message = self._message_queue.get(timeout=0.1)
                self._dispatch(message)
            except queue.Empty:
                continue
            except Exception as e:
                print(f"⚠️ 消息处理异常: {e}")
    
    def _dispatch(self, message: Message):
        """分发消息到订阅者（使用线程池限制并发）"""
        with self._lock:
            handlers = self._subscribers[message.channel].copy()
        
        for handler in handlers:
            try:
                # 使用线程池提交任务，限制并发数
                if self._executor:
                    self._executor.submit(self._safe_handle, handler, message)
                else:
                    # 回退到同步处理（线程池未启动时）
                    self._safe_handle(handler, message)
            except Exception as e:
                print(f"⚠️ 提交消息处理器失败: {e}")
    
    def _safe_handle(self, handler: Callable[[Message], None], message: Message):
        """安全地调用处理器"""
        try:
            handler(message)
        except Exception as e:
            print(f"⚠️ 消息处理器错误 [{handler.__qualname__}]: {e}")
    
    def get_history(self, channel: Optional[Channel] = None, limit: int = 100) -> List[Message]:
        """获取消息历史
        
        Args:
            channel: 筛选特定频道（None表示全部）
            limit: 返回的最大数量
            
        Returns:
            List[Message]: 消息列表
        """
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
                "queue_size": self._message_queue.qsize(),
                "running": self._running
            }
            return stats


# 全局消息总线实例（单例模式）
_message_bus: Optional[LocalMessageBus] = None


def get_message_bus() -> LocalMessageBus:
    """获取全局消息总线实例"""
    global _message_bus
    if _message_bus is None:
        _message_bus = LocalMessageBus()
    return _message_bus


def reset_message_bus():
    """重置全局消息总线（测试用）"""
    global _message_bus
    if _message_bus:
        _message_bus.stop()
    _message_bus = LocalMessageBus()
