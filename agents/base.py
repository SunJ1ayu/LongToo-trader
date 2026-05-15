"""Base Agent 抽象基类 - 阶段2重构（支持消息总线）"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional, Callable


@dataclass
class AgentResult:
    """Agent 执行结果"""
    success: bool
    data: Dict
    error: Optional[str] = None


class BaseAgent(ABC):
    """Agent 抽象基类
    
    所有具体 Agent 类都必须继承此基类，并实现 process 方法。
    阶段2新增：支持消息总线订阅/发布
    """
    
    def __init__(self):
        self._message_bus = None
        self._subscribed_channels = []
    
    @property
    def name(self) -> str:
        """返回 Agent 类名"""
        return self.__class__.__name__
    
    def set_message_bus(self, message_bus):
        """设置消息总线（阶段2新增）
        
        Args:
            message_bus: LocalMessageBus 实例
        """
        self._message_bus = message_bus
    
    def publish(self, message):
        """发布消息（阶段2新增）
        
        Args:
            message: Message 实例
        """
        if self._message_bus:
            self._message_bus.publish(message)
    
    def subscribe(self, channel, handler: Callable):
        """订阅频道（阶段2新增）
        
        Args:
            channel: Channel 枚举
            handler: 消息处理函数
        """
        if self._message_bus:
            self._message_bus.subscribe(channel, handler)
            self._subscribed_channels.append(channel)
    
    @abstractmethod
    def process(self, input_data: Dict) -> AgentResult:
        """处理输入数据并返回结果
        
        Args:
            input_data: 输入数据字典
            
        Returns:
            AgentResult: 包含处理结果的对象
        """
        pass
    
    @abstractmethod
    def handle_message(self, message):
        """处理收到的消息（阶段2新增）
        
        Args:
            message: Message 实例
        """
        pass
    
    def health_check(self) -> bool:
        """健康检查
        
        默认返回 True，子类可覆盖实现自定义健康检查逻辑。
        
        Returns:
            bool: Agent 是否健康
        """
        return True
