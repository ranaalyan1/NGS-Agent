"""
NGS-Agent v2: Base Agent Framework
Abstract base class for all analysis agents.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional
from enum import Enum
import time

from modules.logging_config import get_logger


class AgentStatus(Enum):
    """Agent execution status."""
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    HALTED = "halted"


@dataclass
class AgentResult:
    """Standard result format for all agents."""
    
    status: AgentStatus
    payload: Dict[str, Any]
    reasoning: str
    halt: bool = False
    halt_reason: str = ""
    execution_time: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'status': self.status.value,
            'payload': self.payload,
            'reasoning': self.reasoning,
            'halt': self.halt,
            'halt_reason': self.halt_reason,
            'execution_time': self.execution_time,
        }
    
    def __repr__(self) -> str:
        halt_info = f" (HALT: {self.halt_reason})" if self.halt else ""
        return (f"AgentResult(status={self.status.value}, "
                f"reasoning={self.reasoning[:50]}...{halt_info})")


class BaseAgent(ABC):
    """
    Abstract base class for all NGS agents.
    
    Each agent performs a specific task in the pipeline:
    - QC agent checks quality
    - Trim agent removes low-quality reads
    - Alignment agent maps reads to genome
    - Quantification agent counts features
    - DE agent performs differential expression analysis
    """
    
    def __init__(self, name: str):
        """Initialize agent with name."""
        self.name = name
        self.logger = get_logger(f"agent.{name}")
        self.logger.info(f"Initialized {name} agent")
    
    @abstractmethod
    def execute(self, inputs: Dict[str, Any], config: Any) -> AgentResult:
        """
        Execute agent logic. Must be implemented by subclasses.
        
        Args:
            inputs: Input data dictionary
            config: Configuration object
        
        Returns:
            AgentResult with status, payload, reasoning, and halt flags
        """
        pass
    
    def run(self, inputs: Dict[str, Any], config: Any) -> AgentResult:
        """
        Run agent with timing and error handling.
        
        Args:
            inputs: Input data
            config: Configuration object
        
        Returns:
            AgentResult
        """
        start_time = time.time()
        self.logger.info(f"Running {self.name} agent")
        
        try:
            result = self.execute(inputs, config)
            result.execution_time = time.time() - start_time
            
            self.logger.info(
                f"{self.name} completed in {result.execution_time:.2f}s: "
                f"{result.reasoning}"
            )
            
            return result
        
        except Exception as e:
            execution_time = time.time() - start_time
            error_msg = str(e)
            self.logger.error(f"{self.name} failed: {error_msg}")
            
            return AgentResult(
                status=AgentStatus.ERROR,
                payload={},
                reasoning=f"Agent failed: {error_msg}",
                halt=True,
                halt_reason=f"{self.name} execution error",
                execution_time=execution_time,
            )
    
    def log_info(self, message: str):
        """Log info message."""
        self.logger.info(message)
    
    def log_warning(self, message: str):
        """Log warning message."""
        self.logger.warning(message)
    
    def log_error(self, message: str):
        """Log error message."""
        self.logger.error(message)
    
    def log_debug(self, message: str):
        """Log debug message."""
        self.logger.debug(message)
