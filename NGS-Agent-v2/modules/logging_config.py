"""
NGS-Agent v2: Logging System
Unified logging for pipeline, agents, and modules.
"""

import logging
import logging.handlers
from pathlib import Path
from typing import Optional


class LoggerFactory:
    """Factory for creating configured loggers."""
    
    _loggers = {}
    _config = None
    
    @staticmethod
    def configure(config):
        """Configure logging from configuration manager."""
        LoggerFactory._config = config
    
    @staticmethod
    def get_logger(name: str) -> logging.Logger:
        """Get or create a logger with given name."""
        if name in LoggerFactory._loggers:
            return LoggerFactory._loggers[name]
        
        logger = logging.getLogger(name)
        
        # Clear any existing handlers
        logger.handlers.clear()
        
        # Get config
        config = LoggerFactory._config
        if config:
            log_level = config.get('logging.level', 'INFO')
            log_file = config.get('logging.log_to_file', True)
            use_colors = config.get('logging.colors', True)
            logs_dir = config.get('paths.logs_dir', './logs')
        else:
            log_level = 'INFO'
            log_file = True
            use_colors = True
            logs_dir = './logs'
        
        # Set level
        logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(getattr(logging, log_level.upper()))
        
        # Formatter
        if use_colors:
            formatter = _ColoredFormatter()
        else:
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
        
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
        # File handler
        if log_file:
            Path(logs_dir).mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                f"{logs_dir}/{name}.log",
                maxBytes=10*1024*1024,  # 10MB
                backupCount=5
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        
        LoggerFactory._loggers[name] = logger
        return logger


class _ColoredFormatter(logging.Formatter):
    """Formatter with color support for terminal output."""
    
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[35m',   # Magenta
    }
    RESET = '\033[0m'
    
    def format(self, record: logging.LogRecord) -> str:
        log_color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{log_color}{record.levelname}{self.RESET}"
        
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        return formatter.format(record)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance."""
    return LoggerFactory.get_logger(name)


# Module logger
logger = get_logger(__name__)
